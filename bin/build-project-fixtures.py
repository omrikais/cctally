#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally project`.

Writes one pair of (stats.db, cache.db) per scenario under
tests/fixtures/project/<scenario>/.local/share/cctally/.
Schema mirrors the production DB. Idempotent — overwrites existing DBs.
"""

from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_stats_db,
    fixture_timestamp_utc,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/project"


# Fixed timestamp for session_files.last_ingested_at so cache.db rebuilds
# are byte-deterministic. Arbitrary UTC instant — value doesn't matter;
# only stability does. When new fixture timestamps are needed for a scenario,
# keep this constant fixed and change the scenario-specific `as_of` instead.
_FIXED_LAST_INGESTED_AT = "2026-04-15T15:00:00Z"


# -- Helpers -----------------------------------------------------------------

def _iso(ts: dt.datetime) -> str:
    return fixture_timestamp_utc(ts)

def _insert_entry(conn, *, source_path, ts, model, input_t, output_t,
                  cache_create=0, cache_read=0, session_id="s0", project_path):
    """Insert one entry + upsert its session_files row."""
    conn.execute(
        "INSERT OR IGNORE INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, session_id, project_path) "
        "VALUES (?, 0, 0, 0, ?, ?, ?)",
        (source_path, _FIXED_LAST_INGESTED_AT, session_id, project_path),
    )
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens) "
        "VALUES (?, 0, ?, ?, ?, ?, ?, ?)",
        (source_path, _iso(ts), model, input_t, output_t, cache_create, cache_read),
    )

def _week_bounds_for(ts: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Anchor a 7-day window starting Monday 00:00 UTC of ts's ISO week."""
    monday = (ts - dt.timedelta(days=ts.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=dt.timezone.utc
    )
    return monday, monday + dt.timedelta(days=7)

# -- Scenario builders -------------------------------------------------------

def build_two_projects_current_week():
    scenario_dir = FIXTURES_DIR / "two-projects-current-week"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    # Fixed AS_OF: Wednesday 2026-04-15 15:00 UTC
    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    # stats.db
    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 50.0, "{}"),
        )

    # cache.db — token counts chosen per the plan. Actual dollar amounts under
    # current CLAUDE_MODEL_PRICING (tiered above 200k) are documented in the
    # commit message; the attribution formula (spent_i / spent_total × week_pct)
    # is ratio-based, so exact round-dollar targets are not required for the
    # downstream Used%/$-per-1% math to be deterministic.
    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # Alpha: 5 entries Opus across 3 sessions
        for i, (sid, input_t, output_t) in enumerate([
            ("alpha-s1", 1_000_000, 100_000),
            ("alpha-s1", 1_000_000, 100_000),
            ("alpha-s2", 500_000, 50_000),
            ("alpha-s3", 300_000, 30_000),
            ("alpha-s3", 200_000, 20_000),
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/alpha-{i}.jsonl",
                ts=as_of - dt.timedelta(hours=24 - i),
                model="claude-opus-4-7",
                input_t=input_t, output_t=output_t,
                session_id=sid, project_path="/fake/repos/alpha",
            )
        # Beta: 3 entries Sonnet across 2 sessions
        for i, (sid, input_t, output_t) in enumerate([
            ("beta-s1", 500_000, 50_000),
            ("beta-s1", 300_000, 30_000),
            ("beta-s2", 200_000, 20_000),
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/beta-{i}.jsonl",
                ts=as_of - dt.timedelta(hours=12 - i),
                model="claude-sonnet-4-6",
                input_t=input_t, output_t=output_t,
                session_id=sid, project_path="/fake/repos/beta",
            )

    # input.env
    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\n'
    )

def build_breakdown_two_models():
    scenario_dir = FIXTURES_DIR / "breakdown-two-models"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 60.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # One project, two models. Token counts chosen so costs differ between
        # models (Opus > Sonnet), producing meaningful per-model breakdown rows.
        for i, (sid, input_t, output_t, model) in enumerate([
            ("s1", 800_000, 80_000, "claude-opus-4-7"),
            ("s1", 200_000, 20_000, "claude-sonnet-4-6"),
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/gamma-{i}.jsonl",
                ts=as_of - dt.timedelta(hours=6 - i),
                model=model,
                input_t=input_t, output_t=output_t,
                session_id=sid, project_path="/fake/repos/gamma",
            )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--breakdown"\n'
    )

def build_filters_project_only():
    scenario_dir = FIXTURES_DIR / "filters-project-only"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 60.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # Three synthetic projects, each one Opus entry at distinct hours
        for i, (proj, sid, input_t, output_t) in enumerate([
            ("/fake/repos/alpha", "alpha-s1", 500_000, 50_000),
            ("/fake/repos/beta",  "beta-s1",  300_000, 30_000),
            ("/fake/repos/gamma", "gamma-s1", 400_000, 40_000),
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/{sid}.jsonl",
                ts=as_of - dt.timedelta(hours=6 - i),
                model="claude-opus-4-7",
                input_t=input_t, output_t=output_t,
                session_id=sid, project_path=proj,
            )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--project alpha --project gamma"\n'
    )

def build_filters_model_only():
    scenario_dir = FIXTURES_DIR / "filters-model-only"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 40.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # Project delta: pure Opus. Project epsilon: pure Sonnet. Filter --model opus
        # should keep only delta; epsilon filtered out of visible rows, but its cost
        # STILL contributes to the per-week denominator (filter-invariant).
        for i, (proj, sid, model, input_t, output_t) in enumerate([
            ("/fake/repos/delta",   "delta-s1",   "claude-opus-4-7",   400_000, 40_000),
            ("/fake/repos/epsilon", "epsilon-s1", "claude-sonnet-4-6", 300_000, 30_000),
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/{sid}.jsonl",
                ts=as_of - dt.timedelta(hours=3 - i),
                model=model,
                input_t=input_t, output_t=output_t,
                session_id=sid, project_path=proj,
            )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--model opus"\n'
    )

def build_sort_by_used_asc():
    scenario_dir = FIXTURES_DIR / "sort-by-used-asc"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 50.0, "{}"),
        )

    # Two projects, different costs so attributedUsedPercent differs.
    # Under --sort used --order asc, beta (lower pct) should come before alpha.
    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        for i, (proj, sid, input_t, output_t) in enumerate([
            ("/fake/repos/alpha-big",    "a-s1", 1_000_000, 100_000),   # larger cost
            ("/fake/repos/beta-small",   "b-s1",   200_000,  20_000),   # smaller cost
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/{sid}.jsonl",
                ts=as_of - dt.timedelta(hours=3 - i),
                model="claude-opus-4-7",
                input_t=input_t, output_t=output_t,
                session_id=sid, project_path=proj,
            )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--sort used --order asc"\n'
    )

def build_basename_collision_default():
    scenario_dir = FIXTURES_DIR / "basename-collision-default"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 40.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # Two no-git paths sharing basename `delta`. Under default --group
        # git-root, both buckets survive (distinct bucket_path) but the
        # display collides, so the renderer augments with parent segments.
        for i, (proj, sid) in enumerate([
            ("/fake/repos/delta", "delta-repos-s1"),
            ("/fake/forks/delta", "delta-forks-s1"),
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/{sid}.jsonl",
                ts=as_of - dt.timedelta(hours=3 - i),
                model="claude-opus-4-7",
                input_t=400_000, output_t=40_000,
                session_id=sid, project_path=proj,
            )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS=""\n'
    )

def build_basename_collision_full_path():
    scenario_dir = FIXTURES_DIR / "basename-collision-full-path"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 40.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # Same seeding as basename-collision-default; the only difference
        # is FLAGS="--group full-path" so display uses the raw project_path
        # and there is no basename collision to disambiguate.
        for i, (proj, sid) in enumerate([
            ("/fake/repos/delta", "delta-repos-s1"),
            ("/fake/forks/delta", "delta-forks-s1"),
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/{sid}.jsonl",
                ts=as_of - dt.timedelta(hours=3 - i),
                model="claude-opus-4-7",
                input_t=400_000, output_t=40_000,
                session_id=sid, project_path=proj,
            )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--group full-path"\n'
    )

def build_unknown_entries():
    scenario_dir = FIXTURES_DIR / "unknown-entries"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 30.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # 3 real-project entries + 2 orphan (NULL project_path)
        for i, (proj, sid) in enumerate([
            ("/fake/repos/real",  "real-s1"),
            ("/fake/repos/real",  "real-s2"),
            ("/fake/repos/real",  "real-s3"),
            (None,                "orphan-s1"),   # NULL project_path
            (None,                "orphan-s2"),
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/entry-{i}.jsonl",
                ts=as_of - dt.timedelta(hours=6 - i),
                model="claude-opus-4-7",
                input_t=300_000, output_t=30_000,
                session_id=sid, project_path=proj,
            )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS=""\n'
    )

def build_empty_range():
    scenario_dir = FIXTURES_DIR / "empty-range"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    # Entries 4 weeks before as_of; current week has none.
    old_ts = as_of - dt.timedelta(days=28)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        # Snapshot for the OLD week (not the current week)
        old_week_start, old_week_end = _week_bounds_for(old_ts)
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(old_ts), old_week_start.date().isoformat(), old_week_end.date().isoformat(),
             _iso(old_week_start), _iso(old_week_end), 50.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        _insert_entry(
            conn,
            source_path="/fake/jsonl/old-s1.jsonl",
            ts=old_ts,
            model="claude-opus-4-7",
            input_t=200_000, output_t=20_000,
            session_id="old-s1", project_path="/fake/repos/old",
        )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS=""\n'
    )

def build_two_weeks_span():
    scenario_dir = FIXTURES_DIR / "two-weeks-span"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    current_week_start, current_week_end = _week_bounds_for(as_of)
    prior_week_start = current_week_start - dt.timedelta(days=7)
    prior_week_end = current_week_start

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        # Two snapshots: prior week (50%), current week (30%)
        for ts, wstart, wend, pct in [
            (prior_week_start + dt.timedelta(days=3),
             prior_week_start, prior_week_end, 50.0),
            (as_of, current_week_start, current_week_end, 30.0),
        ]:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
                " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_iso(ts), wstart.date().isoformat(), wend.date().isoformat(),
                 _iso(wstart), _iso(wend), pct, "{}"),
            )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # One project, entries in both weeks
        for i, ts in enumerate([
            prior_week_start + dt.timedelta(days=2, hours=10),
            prior_week_start + dt.timedelta(days=4, hours=12),
            current_week_start + dt.timedelta(days=1, hours=9),
            current_week_start + dt.timedelta(days=2, hours=14),
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/span-{i}.jsonl",
                ts=ts,
                model="claude-opus-4-7",
                input_t=200_000, output_t=20_000,
                session_id=f"span-s{i}", project_path="/fake/repos/span",
            )

    (scenario_dir / "input.env").write_text(
        # COLUMNS_OVERRIDE=200 so the `(2cy)` suffix on the Used % column is
        # not truncated by the default 120-col render scale-down. Without it
        # the cell collapses to `80.0% (…` and the (Ncy) marker disappears.
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--weeks 2"\nCOLUMNS_OVERRIDE=200\n'
    )

def build_partial_week_denominator():
    """Fixture A (covers Fix 1: whole-week denominator).

    User slice filters down to a single day mid-week, but another project's
    entries later the same week still contribute to the attribution
    denominator — so the visible row's Used % reflects its share of the
    FULL week, not the sliced day.
    """
    scenario_dir = FIXTURES_DIR / "partial-week-denominator"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 50.0, "{}"),
        )

    # Alpha entry inside the user slice (2026-04-14).
    # Beta entry same subscription week but outside the slice (2026-04-16).
    # Both same token counts so beta's contribution to the denominator is
    # symmetric and the visible Used % is a clean 25% (half of the week's 50%).
    alpha_ts = dt.datetime(2026, 4, 14, 10, 0, 0, tzinfo=dt.timezone.utc)
    beta_ts = dt.datetime(2026, 4, 16, 10, 0, 0, tzinfo=dt.timezone.utc)

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        _insert_entry(
            conn,
            source_path="/fake/jsonl/alpha.jsonl",
            ts=alpha_ts,
            model="claude-opus-4-7",
            input_t=1_000_000, output_t=100_000,
            session_id="alpha-s1", project_path="/fake/repos/alpha",
        )
        _insert_entry(
            conn,
            source_path="/fake/jsonl/beta.jsonl",
            ts=beta_ts,
            model="claude-opus-4-7",
            input_t=1_000_000, output_t=100_000,
            session_id="beta-s1", project_path="/fake/repos/beta",
        )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--since 2026-04-14 --until 2026-04-14"\n'
    )

def build_weeks_zero_rejected():
    """Fixture B (covers Fix 2: reject non-positive --weeks).

    Minimal one-project seed; fixture's goal is to verify `--weeks 0`
    produces the validation error before any expensive work.
    """
    scenario_dir = FIXTURES_DIR / "weeks-zero-rejected"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 50.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        _insert_entry(
            conn,
            source_path="/fake/jsonl/only.jsonl",
            ts=as_of - dt.timedelta(hours=3),
            model="claude-opus-4-7",
            input_t=200_000, output_t=20_000,
            session_id="only-s1", project_path="/fake/repos/only",
        )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--weeks 0"\n'
    )

def build_project_filter_disambig():
    """Fixture C (covers Fix 3: --project matches underlying path too).

    Two projects with colliding basenames; `--project repos` should match
    the path segment and keep only the `/fake/repos/delta` row. Matching
    only display_key would fail because the disambiguated suffix doesn't
    contain the substring `repos` the user typed.
    """
    scenario_dir = FIXTURES_DIR / "project-filter-disambig"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 40.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        for i, (proj, sid) in enumerate([
            ("/fake/repos/delta", "delta-repos-s1"),
            ("/fake/forks/delta", "delta-forks-s1"),
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/{sid}.jsonl",
                ts=as_of - dt.timedelta(hours=3 - i),
                model="claude-opus-4-7",
                input_t=400_000, output_t=40_000,
                session_id=sid, project_path=proj,
            )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--project repos"\n'
    )

def build_no_color_flag():
    """Fixture D (covers Fix 4: --no-color suppresses ANSI despite FORCE_COLOR).

    Minimal one-project seed. Harness forwards FORCE_COLOR=1 and omits
    NO_COLOR so ANSI would normally emit; the --no-color flag must win.
    """
    scenario_dir = FIXTURES_DIR / "no-color-flag"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 50.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        _insert_entry(
            conn,
            source_path="/fake/jsonl/solo.jsonl",
            ts=as_of - dt.timedelta(hours=3),
            model="claude-opus-4-7",
            input_t=400_000, output_t=40_000,
            session_id="solo-s1", project_path="/fake/repos/solo",
        )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--no-color"\nFORCE_COLOR=1\n'
    )

def build_equal_cost_tie_break_asc():
    """Fixture E (covers Fix 5: stable alphabetical tie-break across --order).

    Two projects with identical token counts → bitwise-equal cost_usd.
    Under default --sort cost --order asc, the old `list(reversed)` logic
    inverted the tie-break and banana came before apple. With the
    sign-flip fix, the dname secondary stays ascending regardless of
    primary direction, so apple < banana.
    """
    scenario_dir = FIXTURES_DIR / "equal-cost-tie-break-asc"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 50.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        for i, (proj, sid) in enumerate([
            ("/fake/repos/apple",  "apple-s1"),
            ("/fake/repos/banana", "banana-s1"),
        ]):
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/{sid}.jsonl",
                ts=as_of - dt.timedelta(hours=3 - i),
                model="claude-opus-4-7",
                input_t=500_000, output_t=50_000,
                session_id=sid, project_path=proj,
            )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="--order asc"\n'
    )

def build_missing_session_id_fallback():
    """Fixture F (covers Fix 1: NULL session_id falls back to filename stem).

    Two entries with NULL session_id across distinct source files. Without the
    fix, both collapse into the empty-string sentinel and Sessions reports 1.
    With the fix, each filename stem becomes its own surrogate and Sessions
    reports 2 — plus a one-shot stderr warning.
    """
    scenario_dir = FIXTURES_DIR / "missing-session-id-fallback"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of), week_start.date().isoformat(), week_end.date().isoformat(),
             _iso(week_start), _iso(week_end), 30.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # Insert via helper, then NULL-out the session_id on each session_files
        # row so the runtime takes the filename-stem fallback path.
        for i, src in enumerate([
            "/fake/jsonl/zeta-a.jsonl",
            "/fake/jsonl/zeta-b.jsonl",
        ]):
            _insert_entry(
                conn,
                source_path=src,
                ts=as_of - dt.timedelta(hours=3 - i),
                model="claude-opus-4-7",
                input_t=300_000, output_t=30_000,
                session_id=f"placeholder-{i}", project_path="/fake/repos/zeta",
            )
            conn.execute(
                "UPDATE session_files SET session_id = NULL WHERE path = ?",
                (src,),
            )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\n'
    )

def build_reset_boundary_default_range():
    """Fixture G (covers Fix 2: probe widened past zero-width boundary).

    AS_OF lands exactly on a Thursday-anchored subscription-week reset. The
    snapshot for the new (just-started) week is present, so the bisect should
    pick it as the anchor and the default range should start at the Thursday
    noon timestamp. Pre-fix, the zero-width [now, now] probe returned empty
    and the code fell into the Monday-00:00 fallback.
    """
    scenario_dir = FIXTURES_DIR / "reset-boundary-default-range"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    # Thursday noon UTC — non-Monday so the Monday fallback would visibly
    # disagree with the snapshot's reset boundary.
    new_week_start = dt.datetime(2026, 4, 16, 12, 0, 0, tzinfo=dt.timezone.utc)
    new_week_end = new_week_start + dt.timedelta(days=7)
    prior_week_start = new_week_start - dt.timedelta(days=7)
    prior_week_end = new_week_start
    as_of = new_week_start  # exact reset instant

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        # Both prior and new-week snapshots so _compute_subscription_weeks can
        # anchor on the new week (which starts at AS_OF).
        for ts, wstart, wend, pct in [
            (prior_week_start + dt.timedelta(days=2), prior_week_start, prior_week_end, 25.0),
            (new_week_start, new_week_start, new_week_end, 0.0),
        ]:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
                " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_iso(ts), wstart.date().isoformat(), wend.date().isoformat(),
                 _iso(wstart), _iso(wend), pct, "{}"),
            )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # One entry inside the prior week so default-range output isn't empty
        # — but the visible window must still anchor on the Thursday reset
        # (so this entry falls outside, which is the whole point: the
        # "no rows" message would print and prove the anchor is correct).
        # To get a non-empty visible row, place an entry RIGHT AT the new
        # week start. Until is `now`, since is `now`, so we need a sliver.
        # Easiest: place an entry at `as_of` exactly (within [since, until]).
        _insert_entry(
            conn,
            source_path="/fake/jsonl/iota.jsonl",
            ts=as_of,
            model="claude-opus-4-7",
            input_t=400_000, output_t=40_000,
            session_id="iota-s1", project_path="/fake/repos/iota",
        )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\n'
    )

def _build_project_credited(name, *, cuts, ladder, flags, as_of=None,
                            week_of=None, successor=None):
    """One in-place-credited week for `cctally project` (#750 S4 §6.2 / #734).

    `bin/build-project-fixtures.py` wrote no `old_week_end_at` at all, so no
    project fixture exercised a credited week. Each event here carries the
    shape both appliers require — `old_week_end_at == effective_reset_at_utc`,
    with `new_week_end_at` holding the week's unchanged end — and N cuts
    produce N+1 billing cycles, each of which must resolve its own percentage
    inside its own half-open interval.

    Every drop across a cut stays under the 25pp threshold
    `_backfill_week_reset_events` uses. Above it the backfill mints a second,
    capture-anchored event beside the seeded one and the fixture would hold
    more cycles than it seeds. The split under test comes from the seeded
    event, not from the size of the drop.

    ``week_of`` anchors the credited week somewhere other than the one
    containing ``as_of``, and ``successor`` seeds the following week. Together
    they put the credited week somewhere other than LAST, which is what lets
    `_apply_overlap_clamp_to_subweeks` reach its tail: that clamp runs after
    the reset applier and pulls a tail's end back to the next week's start
    when the next anchor drifted earlier, so the tail's end stops being the
    `new_week_end_at` the event recorded.
    """
    as_of = as_of or dt.datetime(2026, 4, 17, 18, 0, 0, tzinfo=dt.timezone.utc)
    scenario_dir = FIXTURES_DIR / name
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)
    week_start, week_end = _week_bounds_for(week_of or as_of)

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        for hours_in, pct in ladder:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_iso(week_start + dt.timedelta(hours=hours_in)),
                 week_start.date().isoformat(), week_end.date().isoformat(),
                 _iso(week_start), _iso(week_end), pct, "{}"),
            )
        for hours_in in cuts:
            cut = week_start + dt.timedelta(hours=hours_in)
            cut_iso = cut.astimezone(dt.timezone.utc).isoformat(
                timespec="seconds")
            conn.execute(
                "INSERT INTO week_reset_events "
                "(detected_at_utc, old_week_end_at, new_week_end_at, "
                " effective_reset_at_utc, observed_pre_credit_pct, "
                " account_key) VALUES (?,?,?,?,?,?)",
                (cut_iso, cut_iso,
                 week_end.astimezone(dt.timezone.utc).isoformat(
                     timespec="seconds"),
                 cut_iso, 0.0, "unattributed"),
            )
        if successor is not None:
            succ_start = week_end + dt.timedelta(
                hours=successor["drift_hours"])
            succ_end = succ_start + dt.timedelta(days=7)
            # A successor is either a single uncredited reading
            # (`captured_hours` + `pct`) or a credited week of its own
            # (`ladder` + `cuts`). The second form is what puts TWO credited
            # weeks inside one rendered window.
            succ_ladder = successor.get("ladder")
            if succ_ladder is None:
                succ_ladder = [(successor["captured_hours"],
                                successor["pct"])]
            for hours_in, pct in succ_ladder:
                conn.execute(
                    "INSERT INTO weekly_usage_snapshots "
                    "(captured_at_utc, week_start_date, week_end_date, "
                    " week_start_at, week_end_at, weekly_percent, "
                    " payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_iso(succ_start + dt.timedelta(hours=hours_in)),
                     succ_start.date().isoformat(),
                     succ_end.date().isoformat(),
                     _iso(succ_start), _iso(succ_end), pct, "{}"),
                )
            for hours_in in successor.get("cuts", ()):
                cut = succ_start + dt.timedelta(hours=hours_in)
                cut_iso = cut.astimezone(dt.timezone.utc).isoformat(
                    timespec="seconds")
                conn.execute(
                    "INSERT INTO week_reset_events "
                    "(detected_at_utc, old_week_end_at, new_week_end_at, "
                    " effective_reset_at_utc, observed_pre_credit_pct, "
                    " account_key) VALUES (?,?,?,?,?,?)",
                    (cut_iso, cut_iso,
                     succ_end.astimezone(dt.timezone.utc).isoformat(
                         timespec="seconds"),
                     cut_iso, 0.0, "unattributed"),
                )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # One entry per cycle, in alternating projects, so a cycle reading
        # another cycle's percentage shows up in a rendered Used %.
        bounds = [0] + list(cuts) + [24 * 7]
        for i in range(len(bounds) - 1):
            ts = week_start + dt.timedelta(hours=bounds[i] + 1)
            project = "/fake/repos/alpha" if i % 2 == 0 else "/fake/repos/beta"
            _insert_entry(
                conn,
                source_path=f"/fake/jsonl/{name}-{i}.jsonl",
                ts=ts,
                model="claude-opus-4-7",
                input_t=150_000 + 20_000 * i, output_t=20_000,
                session_id=f"{name}-s{i}", project_path=project,
            )
        if successor is not None:
            succ_start = week_end + dt.timedelta(
                hours=successor["drift_hours"])
            # One entry per successor cycle, for the same reason the credited
            # week gets one per cycle: a cycle borrowing a neighbour's
            # percentage has to show up in a rendered Used %.
            succ_bounds = [0] + list(successor.get("cuts", ())) + [24 * 7]
            for i in range(len(succ_bounds) - 1):
                _insert_entry(
                    conn,
                    source_path=f"/fake/jsonl/{name}-succ-{i}.jsonl",
                    ts=succ_start + dt.timedelta(hours=succ_bounds[i] + 12),
                    model="claude-opus-4-7",
                    input_t=90_000 + 15_000 * i, output_t=15_000,
                    session_id=f"{name}-succ{i}",
                    project_path=("/fake/repos/alpha" if i % 2 == 0
                                  else "/fake/repos/beta"),
                )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\nFLAGS="{flags}"\nCOLUMNS_OVERRIDE=200\n'
    )


def build_credited_week():
    """An ordinary single credit whose two cycles fall on DIFFERENT dates.

    `project` must report the pre-credit cycle's own 41.0 and the post-credit
    cycle's 30.0 — the two figures `weekly` reports for the same store — and
    `totals.usedPercent` must SUM them, because a billing cycle is the unit
    that owns a 100% quota (D7).
    """
    _build_project_credited(
        "credited-week",
        cuts=[72],
        ladder=[(20, 24.0), (50, 41.0), (72, 19.0), (92, 30.0)],
        flags="--weeks 1",
    )


def build_credited_week_same_day():
    """TWO cuts on one calendar day: three cycles."""
    _build_project_credited(
        "credited-week-same-day",
        cuts=[75, 87],
        ladder=[(30, 26.0), (74, 44.0), (77, 22.0), (89, 5.0), (100, 13.0)],
        flags="--weeks 1",
    )


def build_credited_week_not_last():
    """A credited week followed by one whose anchor drifted EARLIER.

    The overlap clamp then pulls the credited week's tail end back to that
    drifted anchor, so the tail no longer ends at the `new_week_end_at` the
    reset event recorded. A cut binding keyed on the week end finds nothing
    here, reports the week as uncredited, and hands BOTH cycles the week's
    latest reading — #731 in full, and silently. Every credited project
    fixture before this one ran `--weeks 1`, which always makes the credited
    week the last one and leaves its tail unclamped.

    It carries TWO cuts rather than one, so the moved tail and the N-ary split
    are exercised together. Before that they were covered only apart: every
    three-segment case ran `--weeks 1`, where the tail is never clamped, and
    this scenario was the only clamped tail and had a single cut. Both
    behaviours are correct by construction, which is exactly why a regression
    in their interaction would be silent.
    """
    _build_project_credited(
        "credited-week-not-last",
        cuts=[72, 120],
        ladder=[(20, 24.0), (50, 41.0), (72, 19.0), (92, 30.0),
                (120, 8.0), (140, 17.0)],
        # `--weeks 4`, not 3: the second cut adds a third cycle, and at 3 the
        # window starts inside the credited week, dropping the head cycle's 41.0 and
        # leaving a cycle in range with no snapshot, so
        # `weeklyAttributionAvailable` went false. At 4 all three cycles and
        # the successor are in range with four distinct readings — 41.0, 30.0,
        # 17.0, 15.0 — one per cycle.
        flags="--weeks 4",
        as_of=dt.datetime(2026, 4, 22, 12, 0, 0, tzinfo=dt.timezone.utc),
        week_of=dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc),
        successor={"drift_hours": -6, "captured_hours": 42, "pct": 15.0},
    )


def build_two_credited_weeks():
    """TWO credited weeks inside ONE rendered window.

    Every other credited fixture holds exactly one, so nothing pinned what
    happens when a window contains several: the cut binding, the segment
    capture bounds and the per-segment percentage each key on one week at a
    time, and a defect that mixed one week's cuts into another's segments
    would leave every single-week fixture green. The successor's anchor does
    NOT drift here, so this scenario isolates the multiplicity from the
    overlap clamp `credited-week-not-last` covers.

    Four cycles in total at four different percentages, so a cycle reading a
    neighbour's figure — inside a week or across the two — changes a rendered
    Used %.
    """
    _build_project_credited(
        "two-credited-weeks",
        cuts=[72],
        ladder=[(20, 24.0), (50, 41.0), (72, 19.0), (92, 30.0)],
        # `--weeks 4` is the width that admits all four cycles. At 3 the
        # window opens on the first week's cut and the head cycle's 41.0 never
        # renders, which halves what the goldens discriminate.
        flags="--weeks 4",
        as_of=dt.datetime(2026, 4, 24, 12, 0, 0, tzinfo=dt.timezone.utc),
        week_of=dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc),
        successor={
            "drift_hours": 0,
            "cuts": [96],
            # 33.0 -> 10.0 is 23pp, under the 25pp threshold
            # `_backfill_week_reset_events` uses, for the reason
            # `_build_project_credited`'s docstring gives: above it the
            # backfill mints its own capture-anchored event beside the seeded
            # one.
            "ladder": [(24, 12.0), (90, 33.0), (96, 10.0), (120, 21.0)],
        },
    )


def build_tz_variant_snapshots():
    """Two snapshots of ONE cycle whose offset spellings disagree with their
    instant order.

    Fixture H originally covered a `GROUP BY week_start_at` defect: two rows
    for one logical week written `+00:00` and `+03:00` split into two groups
    and `_load_week_snapshots` overwrote one pct on the dict-key collision.
    #750 S4 §2.1 replaced that reducer, and `latest_usage_by_segment` selects
    `week_start_date, captured_at_utc, id, weekly_percent` — it never reads
    `week_start_at`, so through THIS command that question is now unreachable.
    It is still live on the dashboard projects query, which anchors each row
    by `week_start_at` (bin/_cctally_dashboard.py), and it is asserted there.

    So the discriminating construction moved to the column this path does
    read. The reducer ranks candidates by the PARSED capture instant, never
    by the stored text, because one instant is written in more than one
    offset across the tree and `'…+00:00' < '…Z'` holds for two spellings of
    the same moment. The two rows below are built so a textual ranking and an
    instant ranking disagree outright: the 70% row is captured EARLIER
    (13:00Z) but spelled `+03:00`, so its text sorts AFTER the 50% row's
    (14:00Z, `+00:00`). A lexical `ORDER BY captured_at_utc DESC` therefore
    reports 70 and the golden's 50 fails.

    The value also pins LATEST over MAX. MAX rested on "weekly_percent is
    monotonic within a week", which is true of a billing CYCLE and false of a
    credited week; segment bounds express the cycle directly now, and LATEST
    is what `weekly` and the dashboard projects panel already read, so all
    three surfaces answer #731's question the same way. The two rows sit in
    one uncredited cycle, so the later capture (50) is the answer where the
    maximum (70) used to be.
    """
    scenario_dir = FIXTURES_DIR / "tz-variant-snapshots"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    as_of = dt.datetime(2026, 4, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
    week_start, week_end = _week_bounds_for(as_of)

    # Two tz spellings of the same instant: 2026-04-13T00:00:00Z.
    ws_utc = "2026-04-13T00:00:00+00:00"
    ws_plus3 = "2026-04-13T03:00:00+03:00"
    we_utc = "2026-04-20T00:00:00+00:00"
    we_plus3 = "2026-04-20T03:00:00+03:00"

    stats_path = db_dir / "stats.db"
    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        # The 70% row is captured at 13:00Z — EARLIER than the 50% row — but
        # written `+03:00`, so its text sorts after the other row's. A ranking
        # by stored text picks it; a ranking by parsed instant does not. The
        # literal is written out rather than passed through `_iso`, because
        # `_iso` canonicalizes to `+00:00` and that is exactly the spelling
        # this row must NOT carry.
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-04-15T16:00:00+03:00",
             week_start.date().isoformat(), week_end.date().isoformat(),
             ws_utc, we_utc, 70.0, "{}"),
        )
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(as_of - dt.timedelta(hours=1)),
             week_start.date().isoformat(), week_end.date().isoformat(),
             ws_plus3, we_plus3, 50.0, "{}"),
        )

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        _insert_entry(
            conn,
            source_path="/fake/jsonl/tzv.jsonl",
            ts=as_of - dt.timedelta(hours=3),
            model="claude-opus-4-7",
            input_t=400_000, output_t=40_000,
            session_id="tzv-s1", project_path="/fake/repos/tzv",
        )

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\n'
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Override output directory. Defaults to the in-tree path "
            "tests/fixtures/project/. Used by cctally-project-test "
            "to write into a per-run scratch dir so the in-tree fixtures "
            "stay byte-stable across harness runs."
        ),
    )
    args = parser.parse_args()
    if args.out is not None:
        FIXTURES_DIR = args.out
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    build_two_projects_current_week()
    build_breakdown_two_models()
    build_filters_project_only()
    build_filters_model_only()
    build_sort_by_used_asc()
    build_basename_collision_default()
    build_basename_collision_full_path()
    build_unknown_entries()
    build_empty_range()
    build_two_weeks_span()
    build_partial_week_denominator()
    build_weeks_zero_rejected()
    build_project_filter_disambig()
    build_no_color_flag()
    build_equal_cost_tie_break_asc()
    build_missing_session_id_fallback()
    build_reset_boundary_default_range()
    build_credited_week()
    build_credited_week_same_day()
    build_credited_week_not_last()
    build_two_credited_weeks()
    build_tz_variant_snapshots()
    print(f"Built fixtures under {FIXTURES_DIR}")
