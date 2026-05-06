"""Tests for diff section aggregators + Used% resolver."""
import datetime as dt
import pathlib
import sqlite3
import sys

import pytest

from conftest import load_script


def _ns():
    return load_script()


def _utc(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


@pytest.fixture
def seeded_cache_db(tmp_path, monkeypatch):
    """Create a minimal cache.db + stats.db with three Claude session_entries.

    Layout:
      * Two entries within window A (this-week ~ 2026-04-19..2026-04-26):
          - opus-4-7  on 2026-04-22, project-a, no cache, $0.50 raw
          - sonnet-4-6 on 2026-04-23, project-b, with cache, $0.80 raw
      * One entry within window B (last-week ~ 2026-04-12..2026-04-19):
          - sonnet-4-6 on 2026-04-15, project-b, no cache, $0.60 raw

    Uses the production fixture builders so the schema is bit-identical to
    open_cache_db()/open_db() and inline migrations don't fire on first open.
    """
    home = tmp_path
    share = home / ".local" / "share" / "cctally"
    share.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    # Empty ~/.claude/projects so sync_cache walks an empty tree and
    # leaves our seeded rows untouched.
    (home / ".claude" / "projects").mkdir(parents=True)

    # Reuse the production fixture builders — schema match guaranteed.
    bin_dir = pathlib.Path(__file__).resolve().parent.parent / "bin"
    sys.path.insert(0, str(bin_dir))
    try:
        from _fixture_builders import (
            create_cache_db, create_stats_db,
            seed_session_file, seed_session_entry,
        )
    finally:
        sys.path.pop(0)

    cache_db = share / "cache.db"
    create_cache_db(cache_db)
    create_stats_db(share / "stats.db")

    with sqlite3.connect(cache_db) as conn:
        seed_session_file(
            conn, path="/c/projects/aa/sess1.jsonl",
            session_id="s1", project_path="/path/to/project-a",
        )
        seed_session_file(
            conn, path="/c/projects/bb/sess2.jsonl",
            session_id="s2", project_path="/path/to/project-b",
        )
        seed_session_file(
            conn, path="/c/projects/bb/sess3.jsonl",
            session_id="s3", project_path="/path/to/project-b",
        )
        seed_session_entry(
            conn, source_path="/c/projects/aa/sess1.jsonl", line_offset=0,
            timestamp_utc="2026-04-22T10:00:00Z", model="claude-opus-4-7",
            input_tokens=100, output_tokens=1000,
            cache_create=0, cache_read=50000, cost_usd_raw=0.50,
        )
        seed_session_entry(
            conn, source_path="/c/projects/bb/sess2.jsonl", line_offset=0,
            timestamp_utc="2026-04-23T10:00:00Z", model="claude-sonnet-4-6",
            input_tokens=200, output_tokens=2000,
            cache_create=5000, cache_read=80000, cost_usd_raw=0.80,
        )
        seed_session_entry(
            conn, source_path="/c/projects/bb/sess3.jsonl", line_offset=0,
            timestamp_utc="2026-04-15T10:00:00Z", model="claude-sonnet-4-6",
            input_tokens=150, output_tokens=1500,
            cache_create=0, cache_read=60000, cost_usd_raw=0.60,
        )
        conn.commit()

    return cache_db


def _wide_window(ns):
    """Window covering all three seeded entries."""
    ParsedWindow = ns["ParsedWindow"]
    return ParsedWindow(
        label="all", start_utc=_utc("2026-04-01T00:00:00Z"),
        end_utc=_utc("2026-04-30T00:00:00Z"),
        length_days=29.0, kind="explicit-range",
        week_aligned=False, full_weeks_count=0,
    )


def test_overall_aggregator_sums_cost_and_tokens(seeded_cache_db):
    ns = _ns()
    agg = ns["_diff_aggregate_overall"]
    pw = _wide_window(ns)
    mb = agg(pw, skip_sync=True)
    # cost_usd_raw is honored by mode="auto" — sum of 0.50 + 0.80 + 0.60.
    assert abs(mb.cost_usd - 1.90) < 1e-9
    assert mb.tokens_input == 100 + 200 + 150
    assert mb.tokens_output == 1000 + 2000 + 1500
    # cache_hit = cache_read / (cache_read + input).
    expected_hit = 190_000 / (190_000 + 450) * 100.0
    assert abs(mb.cache_hit_pct - expected_hit) < 1e-6


def test_models_aggregator_groups_by_model(seeded_cache_db):
    ns = _ns()
    agg = ns["_diff_aggregate_models"]
    pw = _wide_window(ns)
    by_model = agg(pw, skip_sync=True)
    assert "claude-opus-4-7" in by_model
    assert "claude-sonnet-4-6" in by_model
    assert abs(by_model["claude-opus-4-7"].cost_usd - 0.50) < 1e-9
    assert abs(by_model["claude-sonnet-4-6"].cost_usd - (0.80 + 0.60)) < 1e-9


def test_projects_aggregator_groups_by_project(seeded_cache_db):
    ns = _ns()
    agg = ns["_diff_aggregate_projects"]
    pw = _wide_window(ns)
    by_project = agg(pw, skip_sync=True)
    assert len(by_project) >= 2
    total = sum(mb.cost_usd for mb in by_project.values())
    assert abs(total - 1.90) < 1e-9


def test_cache_aggregator_returns_cache_active_scope(seeded_cache_db):
    ns = _ns()
    agg = ns["_diff_aggregate_cache"]
    pw = _wide_window(ns)
    by_scope = agg(pw, skip_sync=True)
    assert "cache:overall" in by_scope
    overall = by_scope["cache:overall"]
    # All three seeded entries have cache_read > 0, so overall cost matches.
    assert abs(overall.cost_usd - 1.90) < 1e-9
    if "cache:claude" in by_scope:
        assert by_scope["cache:claude"].cost_usd <= overall.cost_usd + 1e-9


@pytest.fixture
def seeded_stats_db(tmp_path, monkeypatch):
    """Create a stats.db with weekly_usage_snapshots rows for Used% lookup tests."""
    home = tmp_path
    share = home / ".local" / "share" / "cctally"
    share.mkdir(parents=True)

    bin_dir = pathlib.Path(__file__).resolve().parent.parent / "bin"
    sys.path.insert(0, str(bin_dir))
    try:
        from _fixture_builders import create_stats_db, seed_weekly_usage_snapshot
    finally:
        sys.path.pop(0)

    db = share / "stats.db"
    create_stats_db(db)
    with sqlite3.connect(db) as conn:
        # Two weeks of snapshots, one per week.
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc="2026-04-18T23:00:00Z",
            week_start_date="2026-04-12",
            week_end_date="2026-04-19",
            weekly_percent=42.0,
            week_start_at="2026-04-12T07:00:00Z",
            week_end_at="2026-04-19T07:00:00Z",
        )
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc="2026-04-25T19:00:00Z",
            week_start_date="2026-04-19",
            week_end_date="2026-04-26",
            weekly_percent=57.0,
            week_start_at="2026-04-19T07:00:00Z",
            week_end_at="2026-04-26T07:00:00Z",
        )
        conn.commit()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return db


def test_used_pct_exact_for_aligned_single_week(seeded_stats_db):
    ns = _ns()
    ParsedWindow = ns["ParsedWindow"]
    resolve = ns["_diff_resolve_used_pct"]
    # Aligned single-week window covering 2026-04-19 .. 2026-04-26
    pw = ParsedWindow(label="this-week", start_utc=_utc("2026-04-19T07:00:00Z"),
                      end_utc=_utc("2026-04-26T07:00:00Z"),
                      length_days=7.0, kind="week",
                      week_aligned=True, full_weeks_count=1)
    val, mode = resolve(pw)
    assert mode == "exact"
    assert val == 57.0


def test_used_pct_avg_for_multi_week_aligned(seeded_stats_db):
    ns = _ns()
    ParsedWindow = ns["ParsedWindow"]
    resolve = ns["_diff_resolve_used_pct"]
    # Two-week window covering both seeded snapshots
    pw = ParsedWindow(label="2w-ago",
                      start_utc=_utc("2026-04-12T00:00:00Z"),
                      end_utc=_utc("2026-04-26T00:00:00Z"),
                      length_days=14.0, kind="week",
                      week_aligned=True, full_weeks_count=2)
    val, mode = resolve(pw)
    assert mode == "avg"
    assert abs(val - 49.5) < 1e-9   # (42 + 57) / 2


def test_used_pct_n_a_for_partial_window(seeded_stats_db):
    ns = _ns()
    ParsedWindow = ns["ParsedWindow"]
    resolve = ns["_diff_resolve_used_pct"]
    # Mid-week partial — week_aligned=False, full_weeks_count=0
    pw = ParsedWindow(label="this-week", start_utc=_utc("2026-04-19T07:00:00Z"),
                      end_utc=_utc("2026-04-25T19:30:00Z"),
                      length_days=6.5, kind="week",
                      week_aligned=False, full_weeks_count=0)
    val, mode = resolve(pw)
    assert mode == "n/a"
    assert val is None


def test_used_pct_mid_week_reset_uses_latest_captured(seeded_stats_db):
    """Regression test for the _apply_midweek_reset_override gotcha:
    the lookup must order by captured_at_utc DESC, not by week_start_date."""
    ns = _ns()
    import os
    home = pathlib.Path(os.environ["HOME"])
    db = home / ".local" / "share" / "cctally" / "stats.db"
    with sqlite3.connect(db) as conn:
        # Append a NEWER snapshot for the SAME calendar week with a different %
        conn.execute(
            "INSERT INTO weekly_usage_snapshots(week_start_date, week_end_date, "
            "week_start_at, week_end_at, weekly_percent, captured_at_utc, "
            "payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-04-19", "2026-04-26",
             "2026-04-19T07:00:00Z", "2026-04-26T07:00:00Z",
             73.0, "2026-04-25T20:00:00Z", "{}"),
        )
        conn.commit()

    ParsedWindow = ns["ParsedWindow"]
    resolve = ns["_diff_resolve_used_pct"]
    pw = ParsedWindow(label="this-week", start_utc=_utc("2026-04-19T07:00:00Z"),
                      end_utc=_utc("2026-04-26T07:00:00Z"),
                      length_days=7.0, kind="week",
                      week_aligned=True, full_weeks_count=1)
    val, mode = resolve(pw)
    assert mode == "exact"
    assert val == 73.0   # the LATEST captured wins, not the oldest
