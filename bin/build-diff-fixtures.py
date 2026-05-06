#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally diff`.

Writes one pair of (stats.db, cache.db) per scenario under
tests/fixtures/diff/<scenario>/.local/share/cctally/.
Schema mirrors the production DB. Idempotent — overwrites existing DBs.

Each scenario picks its own `AS_OF` (pinned via CCTALLY_AS_OF in
input.env). `_diff_resolve_anchor` reads the most-recent
weekly_usage_snapshots row to get the (anchor_week_start,
anchor_resets_at) pair used by week-token resolution. We seed an
"anchor" snapshot whose week_start_at and week_end_at form the
subscription-week boundaries.
"""

from __future__ import annotations
import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    FIXED_LAST_INGESTED_AT,
    create_cache_db,
    create_stats_db,
    seed_session_entry,
    seed_session_file,
    seed_weekly_usage_snapshot,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/diff"


# -- Helpers -----------------------------------------------------------------

def _iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_anchor(
    conn: sqlite3.Connection,
    *,
    captured_at: dt.datetime,
    week_start: dt.datetime,
    week_end: dt.datetime,
    weekly_percent: float = 50.0,
) -> None:
    """Seed one weekly_usage_snapshots row that pins the subscription-week
    anchor (week_start_at, week_end_at) used by `_diff_resolve_anchor`."""
    seed_weekly_usage_snapshot(
        conn,
        captured_at_utc=_iso(captured_at),
        week_start_date=week_start.date().isoformat(),
        week_end_date=week_end.date().isoformat(),
        week_start_at=_iso(week_start),
        week_end_at=_iso(week_end),
        weekly_percent=weekly_percent,
    )


def _seed_entry(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    line_offset: int,
    ts: dt.datetime,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_create: int = 0,
    cache_read: int = 0,
    session_id: str = "s0",
    project_path: str | None,
    msg_id: str | None = None,
    req_id: str | None = None,
) -> None:
    """Insert one entry; upsert its session_files row idempotently.

    A scenario commonly emits multiple entries per source_path (one row
    per (source_path, line_offset)); the corresponding session_files row
    must be inserted only once. We pre-check via SELECT to keep the call
    site idempotent without relying on a raw INSERT OR IGNORE."""
    existing = conn.execute(
        "SELECT 1 FROM session_files WHERE path = ?",
        (source_path,),
    ).fetchone()
    if existing is None:
        seed_session_file(
            conn,
            path=source_path,
            session_id=session_id,
            project_path=project_path,
            last_ingested_at=FIXED_LAST_INGESTED_AT,
        )
    seed_session_entry(
        conn,
        source_path=source_path,
        line_offset=line_offset,
        timestamp_utc=_iso(ts),
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_create=cache_create,
        cache_read=cache_read,
        msg_id=msg_id,
        req_id=req_id,
    )


def _ensure_dir(scenario: str) -> tuple[Path, Path, Path]:
    scenario_dir = FIXTURES_DIR / scenario
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)
    return scenario_dir, db_dir / "stats.db", db_dir / "cache.db"


def _ensure_week_reset_events_table(conn: sqlite3.Connection) -> None:
    """Materialize the production `week_reset_events` table inside a
    fixture stats.db. The shared `_fixture_builders.create_stats_db`
    intentionally omits this table because most scenarios don't seed
    reset events; the diff fixtures need it for scenarios that
    exercise the mid-week reset override path."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS week_reset_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at_utc        TEXT NOT NULL,
            old_week_end_at        TEXT NOT NULL,
            new_week_end_at        TEXT NOT NULL,
            effective_reset_at_utc TEXT NOT NULL,
            UNIQUE(old_week_end_at, new_week_end_at)
        )
        """
    )


def _canonical_iso(ts: dt.datetime) -> str:
    """Canonicalize a UTC datetime to the same form
    `_canonicalize_optional_iso` produces in production: hour-floored
    boundary, ISO 8601 with `+00:00` suffix (NOT `Z`). Required for
    `week_reset_events` text comparisons to match what
    `_diff_resolve_anchor` looks up."""
    utc = ts.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    return utc.isoformat(timespec="seconds")


def _seed_reset_event(
    conn: sqlite3.Connection,
    *,
    detected_at: dt.datetime,
    old_week_end: dt.datetime,
    new_week_end: dt.datetime,
    effective_reset_at: dt.datetime,
) -> None:
    """Seed one week_reset_events row. Endpoints are stored in the
    `+00:00` canonical form (mirrors production `cmd_record_usage`
    output via `_canonicalize_optional_iso`)."""
    conn.execute(
        "INSERT OR IGNORE INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
        (
            detected_at.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
            _canonical_iso(old_week_end),
            _canonical_iso(new_week_end),
            _canonical_iso(effective_reset_at),
        ),
    )


# -- Scenario 1: same-length-week -------------------------------------------

def build_same_length_week():
    """`--a this-week --b last-week` with AS_OF == anchor_resets_at so both
    windows are exactly 7 days and week-aligned (no mismatch)."""
    # NOTE: D1 reconcile invariant (diff_overall_eq_daily_total) depends on
    # entries falling on 2026-04-{20,21,23,24} only — no entries on the
    # partial-boundary days 2026-04-{18,19,25}. Adding entries on those days
    # will break the invariant because daily covers full UTC dates while
    # window A ends at 2026-04-25T19:30Z.
    scenario_dir, stats_path, cache_path = _ensure_dir("same-length-week")

    anchor_week_start = dt.datetime(2026, 4, 18, 19, 30, 0, tzinfo=dt.timezone.utc)
    anchor_resets_at = dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc)
    last_week_start = anchor_week_start - dt.timedelta(days=7)
    as_of = anchor_resets_at

    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        # Anchor (current week) snapshot — pins week_start_at / week_end_at
        _seed_anchor(conn,
                     captured_at=as_of - dt.timedelta(hours=1),
                     week_start=anchor_week_start,
                     week_end=anchor_resets_at,
                     weekly_percent=60.0)
        # Last-week snapshot for Used % lookup on window B
        _seed_anchor(conn,
                     captured_at=anchor_week_start - dt.timedelta(hours=1),
                     week_start=last_week_start,
                     week_end=anchor_week_start,
                     weekly_percent=40.0)

    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # This week: 3 entries on opus, 1 on sonnet — moderate cost
        for i, (ts, model, in_t, out_t) in enumerate([
            (anchor_week_start + dt.timedelta(days=1, hours=10),
             "claude-opus-4-7", 600_000, 60_000),
            (anchor_week_start + dt.timedelta(days=2, hours=12),
             "claude-opus-4-7", 400_000, 40_000),
            (anchor_week_start + dt.timedelta(days=4, hours=9),
             "claude-sonnet-4-6", 200_000, 20_000),
            (anchor_week_start + dt.timedelta(days=5, hours=14),
             "claude-opus-4-7", 300_000, 30_000),
        ]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/this-{i}.jsonl",
                        line_offset=0,
                        ts=ts, model=model, input_tokens=in_t, output_tokens=out_t,
                        cache_read=in_t // 4,
                        session_id=f"this-s{i}",
                        project_path="/fake/repos/alpha")
        # Last week: smaller — gives a clear cost-up delta
        for i, (ts, model, in_t, out_t) in enumerate([
            (last_week_start + dt.timedelta(days=1, hours=10),
             "claude-opus-4-7", 300_000, 30_000),
            (last_week_start + dt.timedelta(days=3, hours=11),
             "claude-sonnet-4-6", 200_000, 20_000),
            (last_week_start + dt.timedelta(days=5, hours=13),
             "claude-opus-4-7", 200_000, 20_000),
        ]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/last-{i}.jsonl",
                        line_offset=0,
                        ts=ts, model=model, input_tokens=in_t, output_tokens=out_t,
                        cache_read=in_t // 5,
                        session_id=f"last-s{i}",
                        project_path="/fake/repos/alpha")

    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a this-week --b last-week --width 144"\n'
    )


# -- Scenario 1b: auto-week-headline ----------------------------------------

def build_auto_week_headline():
    """`--a this-week --b last-week` mid-flight (AS_OF in the middle of
    this-week's window). Exercises the spec §2 rule 3 auto-normalization
    branch: same-kind week pair, lengths differ, auto-fires per-day
    normalization with the softer info banner — NO `--allow-mismatch`
    flag in input.env. Companion to `same-length-week` (which pins
    AS_OF == anchor_resets_at to avoid mismatch entirely)."""
    scenario_dir, stats_path, cache_path = _ensure_dir("auto-week-headline")

    # Anchor: subscription week [2026-04-18 19:30, 2026-04-25 19:30].
    # AS_OF mid-flight (3.5 days in).
    anchor_week_start = dt.datetime(2026, 4, 18, 19, 30, 0, tzinfo=dt.timezone.utc)
    anchor_resets_at = dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc)
    last_week_start = anchor_week_start - dt.timedelta(days=7)
    as_of = anchor_week_start + dt.timedelta(days=3, hours=12)  # 2026-04-22 07:30

    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        # Anchor (current week) snapshot — captured shortly before AS_OF.
        _seed_anchor(conn,
                     captured_at=as_of - dt.timedelta(hours=2),
                     week_start=anchor_week_start,
                     week_end=anchor_resets_at,
                     weekly_percent=42.0)
        # Last-week snapshot (full week) for window B Used % lookup.
        _seed_anchor(conn,
                     captured_at=anchor_week_start - dt.timedelta(hours=1),
                     week_start=last_week_start,
                     week_end=anchor_week_start,
                     weekly_percent=58.0)

    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # This-week (partial — entries up to AS_OF, 3 entries on opus + 1 on sonnet)
        for i, (off_d, off_h, model, in_t, out_t) in enumerate([
            (1, 6, "claude-opus-4-7", 500_000, 50_000),
            (2, 4, "claude-opus-4-7", 350_000, 35_000),
            (2, 18, "claude-sonnet-4-6", 180_000, 18_000),
            (3, 8, "claude-opus-4-7", 280_000, 28_000),
        ]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/this-{i}.jsonl",
                        line_offset=0,
                        ts=anchor_week_start + dt.timedelta(days=off_d, hours=off_h),
                        model=model, input_tokens=in_t, output_tokens=out_t,
                        cache_read=in_t // 4,
                        session_id=f"this-s{i}",
                        project_path="/fake/repos/alpha")
        # Last-week (full 7d) — slightly higher cost overall to make the
        # per-day delta visible (last-week's per-day will be lower than
        # this-week's per-day after normalization).
        for i, (off_d, off_h, model, in_t, out_t) in enumerate([
            (1, 10, "claude-opus-4-7", 600_000, 60_000),
            (2, 12, "claude-opus-4-7", 500_000, 50_000),
            (3, 8, "claude-opus-4-7", 450_000, 45_000),
            (4, 14, "claude-sonnet-4-6", 220_000, 22_000),
            (5, 9, "claude-opus-4-7", 380_000, 38_000),
            (6, 11, "claude-opus-4-7", 320_000, 32_000),
        ]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/last-{i}.jsonl",
                        line_offset=0,
                        ts=last_week_start + dt.timedelta(days=off_d, hours=off_h),
                        model=model, input_tokens=in_t, output_tokens=out_t,
                        cache_read=in_t // 4,
                        session_id=f"last-s{i}",
                        project_path="/fake/repos/alpha")

    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a this-week --b last-week --width 144"\n'
    )


# -- Scenario 2: with-new-and-dropped ---------------------------------------

def build_with_new_and_dropped():
    """`--a this-week --b last-week`. last-week has claude-haiku-4-5 entries
    that don't appear in this-week (dropped); this-week has claude-sonnet-4-5
    entries that didn't exist in last-week (new). Exercises the `new` /
    `dropped` row statuses in the Models section.

    Both models are real entries in CLAUDE_MODEL_PRICING so neither row
    triggers the `[cost] unknown model` warning (which would leak into
    the goldens and break the JSON parse)."""
    scenario_dir, stats_path, cache_path = _ensure_dir("with-new-and-dropped")

    anchor_week_start = dt.datetime(2026, 4, 18, 19, 30, 0, tzinfo=dt.timezone.utc)
    anchor_resets_at = dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc)
    last_week_start = anchor_week_start - dt.timedelta(days=7)
    as_of = anchor_resets_at

    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        _seed_anchor(conn, captured_at=as_of - dt.timedelta(hours=1),
                     week_start=anchor_week_start, week_end=anchor_resets_at,
                     weekly_percent=55.0)
        _seed_anchor(conn, captured_at=anchor_week_start - dt.timedelta(hours=1),
                     week_start=last_week_start, week_end=anchor_week_start,
                     weekly_percent=35.0)

    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # This week: opus (shared) + sonnet-4-7 (NEW model)
        for i, (ts, model, in_t, out_t) in enumerate([
            (anchor_week_start + dt.timedelta(days=1, hours=10),
             "claude-opus-4-7", 500_000, 50_000),
            (anchor_week_start + dt.timedelta(days=2, hours=11),
             "claude-sonnet-4-5", 600_000, 60_000),  # NEW: only in A
            (anchor_week_start + dt.timedelta(days=3, hours=12),
             "claude-sonnet-4-5", 300_000, 30_000),  # NEW
        ]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/this-{i}.jsonl",
                        line_offset=0, ts=ts, model=model,
                        input_tokens=in_t, output_tokens=out_t,
                        cache_read=in_t // 5,
                        session_id=f"this-s{i}",
                        project_path="/fake/repos/alpha")
        # Last week: opus (shared) + haiku-4-5 (DROPPED model)
        for i, (ts, model, in_t, out_t) in enumerate([
            (last_week_start + dt.timedelta(days=1, hours=10),
             "claude-opus-4-7", 400_000, 40_000),
            (last_week_start + dt.timedelta(days=2, hours=11),
             "claude-haiku-4-5", 800_000, 80_000),  # DROPPED: only in B
            (last_week_start + dt.timedelta(days=4, hours=14),
             "claude-haiku-4-5", 400_000, 40_000),  # DROPPED
        ]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/last-{i}.jsonl",
                        line_offset=0, ts=ts, model=model,
                        input_tokens=in_t, output_tokens=out_t,
                        cache_read=in_t // 5,
                        session_id=f"last-s{i}",
                        project_path="/fake/repos/alpha")

    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a this-week --b last-week --width 144"\n'
    )


# -- Scenario 3: mismatched-default ------------------------------------------

def build_mismatched_default():
    """`--a last-7d --b prev-14d` (no --allow-mismatch). Lengths differ
    (7 vs 14 days) so _build_diff_result raises WindowMismatchError →
    cmd_diff prints to stderr and exits 2. Goldens capture the
    stderr-only message."""
    scenario_dir, stats_path, cache_path = _ensure_dir("mismatched-default")

    as_of = dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc)

    # Day-range tokens don't need an anchor, but cmd_diff still calls
    # _diff_resolve_anchor — empty stats.db is fine (returns None,None).
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    # No entries needed: the error fires before any aggregation.

    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a last-7d --b prev-14d --width 144"\n'
    )


# -- Scenario 4: mismatched-allowed ------------------------------------------

def build_mismatched_allowed():
    """`--a last-7d --b prev-14d --allow-mismatch`. Per-day normalization
    is applied so deltas are scaled to per-day rates. Both windows have
    entries (otherwise the comparison is empty)."""
    scenario_dir, stats_path, cache_path = _ensure_dir("mismatched-allowed")

    as_of = dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc)

    create_stats_db(stats_path)
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # Window A = last 7d  → [as_of - 7d, as_of] = [04-18, 04-25]
        # Window B = prev 14d → [as_of - 28d, as_of - 14d] = [03-28, 04-11]
        # Sprinkle a few entries in each window.
        a_start = as_of - dt.timedelta(days=7)
        for i, (off_h, model, in_t, out_t) in enumerate([
            (24, "claude-opus-4-7", 400_000, 40_000),
            (60, "claude-opus-4-7", 300_000, 30_000),
            (120, "claude-sonnet-4-6", 200_000, 20_000),
        ]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/win-a-{i}.jsonl",
                        line_offset=0,
                        ts=a_start + dt.timedelta(hours=off_h),
                        model=model, input_tokens=in_t, output_tokens=out_t,
                        cache_read=in_t // 5,
                        session_id=f"a-s{i}",
                        project_path="/fake/repos/alpha")
        b_start = as_of - dt.timedelta(days=28)
        for i, (off_h, model, in_t, out_t) in enumerate([
            (24, "claude-opus-4-7", 600_000, 60_000),
            (96, "claude-opus-4-7", 500_000, 50_000),
            (192, "claude-sonnet-4-6", 400_000, 40_000),
            (264, "claude-opus-4-7", 200_000, 20_000),
        ]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/win-b-{i}.jsonl",
                        line_offset=0,
                        ts=b_start + dt.timedelta(hours=off_h),
                        model=model, input_tokens=in_t, output_tokens=out_t,
                        cache_read=in_t // 5,
                        session_id=f"b-s{i}",
                        project_path="/fake/repos/alpha")

    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a last-7d --b prev-14d --allow-mismatch --width 144"\n'
    )


# -- Scenario 5: month-vs-month ---------------------------------------------

def build_month_vs_month():
    """`--a this-month --b last-month`. Multi-week windows → Used % uses
    the `avg` mode (averaged across the snapshots that fall inside each
    window)."""
    scenario_dir, stats_path, cache_path = _ensure_dir("month-vs-month")

    # AS_OF inside April 2026 so this-month = [2026-04-01, 2026-05-01)
    # and last-month = [2026-03-01, 2026-04-01).
    as_of = dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc)

    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        # Two snapshots inside April (current month, varying pct)
        for ts, ws, we, pct in [
            (dt.datetime(2026, 4, 7, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 4, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 11, 19, 30, 0, tzinfo=dt.timezone.utc), 30.0),
            (dt.datetime(2026, 4, 14, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 11, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 18, 19, 30, 0, tzinfo=dt.timezone.utc), 50.0),
            (dt.datetime(2026, 4, 21, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 18, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc), 70.0),
        ]:
            _seed_anchor(conn, captured_at=ts, week_start=ws, week_end=we,
                         weekly_percent=pct)
        # Two snapshots inside March (prior month)
        for ts, ws, we, pct in [
            (dt.datetime(2026, 3, 10, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 7, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 14, 19, 30, 0, tzinfo=dt.timezone.utc), 25.0),
            (dt.datetime(2026, 3, 17, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 14, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 21, 19, 30, 0, tzinfo=dt.timezone.utc), 45.0),
        ]:
            _seed_anchor(conn, captured_at=ts, week_start=ws, week_end=we,
                         weekly_percent=pct)

    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # April entries
        for i, day in enumerate([5, 9, 14, 18, 22]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/apr-{i}.jsonl",
                        line_offset=0,
                        ts=dt.datetime(2026, 4, day, 14, 0, 0, tzinfo=dt.timezone.utc),
                        model="claude-opus-4-7",
                        input_tokens=400_000, output_tokens=40_000,
                        cache_read=80_000,
                        session_id=f"apr-s{i}",
                        project_path="/fake/repos/alpha")
        # March entries (smaller cost than April → clear delta direction)
        for i, day in enumerate([4, 10, 16, 22]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/mar-{i}.jsonl",
                        line_offset=0,
                        ts=dt.datetime(2026, 3, day, 14, 0, 0, tzinfo=dt.timezone.utc),
                        model="claude-opus-4-7",
                        input_tokens=200_000, output_tokens=20_000,
                        cache_read=40_000,
                        session_id=f"mar-s{i}",
                        project_path="/fake/repos/alpha")

    # Calendar months differ in length (April=30d vs March=31d). Without
    # --allow-mismatch the comparison refuses; this scenario exercises the
    # multi-week "avg" Used % mode under per-day normalization.
    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a this-month --b last-month --allow-mismatch --width 144"\n'
    )


# -- Scenario 5b: month-vs-month-full-coverage ------------------------------

def build_month_vs_month_full_coverage():
    """`--a this-month --b last-month` with full per-week snapshot coverage
    so `_diff_resolve_used_pct` resolves to mode="avg" for BOTH windows.

    Background: the previous month-vs-month fixture seeds only 3 of 4 weeks
    for April and 2 of 4 weeks for March, which trips the spec §9.3
    coverage check (`len(vals) < window.full_weeks_count` → "n/a"). That's
    the correct behavior, but it means no fixture exercises the successful
    `avg` rendering path. This scenario fills that gap by seeding one
    snapshot per subscription week intersecting each calendar month so
    coverage is satisfied and Used % renders as the per-window average.
    """
    scenario_dir, stats_path, cache_path = _ensure_dir(
        "month-vs-month-full-coverage"
    )

    # AS_OF inside April 2026 so this-month = [2026-04-01, 2026-05-01)
    # and last-month = [2026-03-01, 2026-04-01). full_weeks_count for
    # both is round(length/7) = 4 (April 30d, March 31d).
    as_of = dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc)

    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        # April: 4 distinct subscription weeks whose captured_at falls
        # inside [2026-04-01, 2026-05-01). Cadence = Wed 19:30 UTC ending,
        # matching the rest of the diff fixture set. Percents ascend so
        # avg = (25+40+55+70)/4 = 47.5.
        for ts, ws, we, pct in [
            (dt.datetime(2026, 4, 2, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 28, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 4, 19, 30, 0, tzinfo=dt.timezone.utc), 25.0),
            (dt.datetime(2026, 4, 8, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 4, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 11, 19, 30, 0, tzinfo=dt.timezone.utc), 40.0),
            (dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 11, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 18, 19, 30, 0, tzinfo=dt.timezone.utc), 55.0),
            (dt.datetime(2026, 4, 22, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 18, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc), 70.0),
        ]:
            _seed_anchor(conn, captured_at=ts, week_start=ws, week_end=we,
                         weekly_percent=pct)
        # March: 4 distinct subscription weeks inside
        # [2026-03-01, 2026-04-01). avg = (15+25+35+50)/4 = 31.25.
        for ts, ws, we, pct in [
            (dt.datetime(2026, 3, 4, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 2, 28, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 7, 19, 30, 0, tzinfo=dt.timezone.utc), 15.0),
            (dt.datetime(2026, 3, 11, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 7, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 14, 19, 30, 0, tzinfo=dt.timezone.utc), 25.0),
            (dt.datetime(2026, 3, 18, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 14, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 21, 19, 30, 0, tzinfo=dt.timezone.utc), 35.0),
            (dt.datetime(2026, 3, 25, 12, 0, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 21, 19, 30, 0, tzinfo=dt.timezone.utc),
             dt.datetime(2026, 3, 28, 19, 30, 0, tzinfo=dt.timezone.utc), 50.0),
        ]:
            _seed_anchor(conn, captured_at=ts, week_start=ws, week_end=we,
                         weekly_percent=pct)

    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # April entries — same model/project shape as month-vs-month so
        # the rest of the diff stays comparable. Cost per entry is larger
        # than March to make the per-day-normalized delta clearly visible.
        for i, day in enumerate([5, 9, 14, 18, 22]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/apr-{i}.jsonl",
                        line_offset=0,
                        ts=dt.datetime(2026, 4, day, 14, 0, 0, tzinfo=dt.timezone.utc),
                        model="claude-opus-4-7",
                        input_tokens=400_000, output_tokens=40_000,
                        cache_read=80_000,
                        session_id=f"apr-s{i}",
                        project_path="/fake/repos/alpha")
        # March entries — smaller cost per entry → clear delta direction.
        for i, day in enumerate([4, 10, 16, 22]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/mar-{i}.jsonl",
                        line_offset=0,
                        ts=dt.datetime(2026, 3, day, 14, 0, 0, tzinfo=dt.timezone.utc),
                        model="claude-opus-4-7",
                        input_tokens=200_000, output_tokens=20_000,
                        cache_read=40_000,
                        session_id=f"mar-s{i}",
                        project_path="/fake/repos/alpha")

    # April=30d vs March=31d — same-kind month pair auto-normalizes per
    # spec §2 rule 3 with NO `--allow-mismatch` flag (companion to
    # `month-vs-month` which keeps the now-redundant flag to test the
    # no-op-flag path). --width 144 matches the rest of the diff
    # fixture set.
    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a this-month --b last-month --width 144"\n'
    )


# -- Scenario 6: arbitrary-range --------------------------------------------

def build_arbitrary_range():
    """`--a 2026-04-01..2026-04-15 --b 2026-03-01..2026-03-15`. Explicit
    ranges → Used % is `n/a` (date-range tokens don't map to subscription
    weeks)."""
    scenario_dir, stats_path, cache_path = _ensure_dir("arbitrary-range")

    as_of = dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc)

    create_stats_db(stats_path)
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # Window A: 2026-04-01..2026-04-15 (15 days inclusive — but the
        # parser uses [start, end+1day) so 16 days). Two entries.
        for i, day in enumerate([5, 12]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/apr-{i}.jsonl",
                        line_offset=0,
                        ts=dt.datetime(2026, 4, day, 14, 0, 0, tzinfo=dt.timezone.utc),
                        model="claude-opus-4-7",
                        input_tokens=500_000, output_tokens=50_000,
                        cache_read=100_000,
                        session_id=f"apr-s{i}",
                        project_path="/fake/repos/alpha")
        # Window B: 2026-03-01..2026-03-15
        for i, day in enumerate([4, 11]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/mar-{i}.jsonl",
                        line_offset=0,
                        ts=dt.datetime(2026, 3, day, 14, 0, 0, tzinfo=dt.timezone.utc),
                        model="claude-opus-4-7",
                        input_tokens=300_000, output_tokens=30_000,
                        cache_read=60_000,
                        session_id=f"mar-s{i}",
                        project_path="/fake/repos/alpha")

    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a 2026-04-01..2026-04-15 --b 2026-03-01..2026-03-15 --width 144"\n'
    )


# -- Scenario 7: noise-filter -----------------------------------------------

def build_noise_filter():
    """Many tiny-changed projects (below |Δ$| / |Δ%| thresholds) plus one
    or two clearly above. Default golden hides the small ones; the
    section header reports the hidden count."""
    scenario_dir, stats_path, cache_path = _ensure_dir("noise-filter")

    anchor_week_start = dt.datetime(2026, 4, 18, 19, 30, 0, tzinfo=dt.timezone.utc)
    anchor_resets_at = dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc)
    last_week_start = anchor_week_start - dt.timedelta(days=7)
    as_of = anchor_resets_at

    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        _seed_anchor(conn, captured_at=as_of - dt.timedelta(hours=1),
                     week_start=anchor_week_start, week_end=anchor_resets_at,
                     weekly_percent=55.0)
        _seed_anchor(conn, captured_at=anchor_week_start - dt.timedelta(hours=1),
                     week_start=last_week_start, week_end=anchor_week_start,
                     weekly_percent=50.0)

    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # 4 noise projects with nearly-identical token counts in both
        # weeks. Both |Δ$| < $0.10 AND |Δ%| < 1.0 so they're hidden under
        # the default noise filter (the section header reports the count).
        for n, name in enumerate(["alpha", "bravo", "charlie", "delta"]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/this-{name}.jsonl",
                        line_offset=0,
                        ts=anchor_week_start + dt.timedelta(days=1 + n, hours=10),
                        model="claude-sonnet-4-6",
                        input_tokens=200_000 + n * 50, output_tokens=20_000,
                        cache_read=40_000,
                        session_id=f"this-{name}",
                        project_path=f"/fake/repos/{name}")
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/last-{name}.jsonl",
                        line_offset=0,
                        ts=last_week_start + dt.timedelta(days=1 + n, hours=10),
                        model="claude-sonnet-4-6",
                        input_tokens=200_000 - n * 50, output_tokens=20_000,
                        cache_read=40_000,
                        session_id=f"last-{name}",
                        project_path=f"/fake/repos/{name}")
        # One BIG project (well above noise) — much higher in this-week
        for i in range(3):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/this-loud-{i}.jsonl",
                        line_offset=0,
                        ts=anchor_week_start + dt.timedelta(days=2, hours=8 + i),
                        model="claude-opus-4-7",
                        input_tokens=800_000, output_tokens=80_000,
                        cache_read=160_000,
                        session_id=f"this-loud-s{i}",
                        project_path="/fake/repos/loud")
        for i in range(2):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/last-loud-{i}.jsonl",
                        line_offset=0,
                        ts=last_week_start + dt.timedelta(days=2, hours=8 + i),
                        model="claude-opus-4-7",
                        input_tokens=400_000, output_tokens=40_000,
                        cache_read=80_000,
                        session_id=f"last-loud-s{i}",
                        project_path="/fake/repos/loud")

    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a this-week --b last-week --width 144"\n'
    )


# -- Scenario 8: mid-week-reset ---------------------------------------------

def build_mid_week_reset():
    """Mid-week subscription reset shifts the anchor forward inside the
    current week. Used % must come from the LATEST snapshot row, not from
    a stale `week_start_at` lookup. Exercises the
    `_apply_midweek_reset_override`-related lookup path."""
    scenario_dir, stats_path, cache_path = _ensure_dir("mid-week-reset")

    # Original week was supposed to run [04-15 19:30, 04-22 19:30].
    # On 04-19 12:00 the user got a billing reset — the new anchor week
    # runs [04-19 12:00, 04-26 12:00]. AS_OF is inside the new window.
    orig_week_start = dt.datetime(2026, 4, 15, 19, 30, 0, tzinfo=dt.timezone.utc)
    orig_week_end = dt.datetime(2026, 4, 22, 19, 30, 0, tzinfo=dt.timezone.utc)
    new_week_start = dt.datetime(2026, 4, 19, 12, 0, 0, tzinfo=dt.timezone.utc)
    new_week_end = dt.datetime(2026, 4, 26, 12, 0, 0, tzinfo=dt.timezone.utc)
    last_week_start = new_week_start - dt.timedelta(days=7)
    as_of = new_week_end  # week-aligned end so this-week is exactly 7d.

    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        # Pre-reset snapshot (older capture, original boundaries, lower pct).
        _seed_anchor(conn,
                     captured_at=dt.datetime(2026, 4, 17, 10, 0, 0, tzinfo=dt.timezone.utc),
                     week_start=orig_week_start, week_end=orig_week_end,
                     weekly_percent=20.0)
        # Post-reset snapshot — newest; defines the "current" anchor used
        # by _diff_resolve_anchor (via ORDER BY captured_at_utc DESC).
        _seed_anchor(conn,
                     captured_at=as_of - dt.timedelta(hours=2),
                     week_start=new_week_start, week_end=new_week_end,
                     weekly_percent=45.0)
        # Last-week snapshot (relative to NEW anchor) for window B Used %.
        _seed_anchor(conn,
                     captured_at=new_week_start - dt.timedelta(hours=1),
                     week_start=last_week_start, week_end=new_week_start,
                     weekly_percent=30.0)

    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # Entries inside the new (post-reset) week
        for i, off_h in enumerate([6, 30, 72, 120]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/this-{i}.jsonl",
                        line_offset=0,
                        ts=new_week_start + dt.timedelta(hours=off_h),
                        model="claude-opus-4-7",
                        input_tokens=400_000, output_tokens=40_000,
                        cache_read=80_000,
                        session_id=f"this-s{i}",
                        project_path="/fake/repos/alpha")
        # Entries inside the prior 7d window (relative to new anchor)
        for i, off_h in enumerate([12, 48, 96]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/last-{i}.jsonl",
                        line_offset=0,
                        ts=last_week_start + dt.timedelta(hours=off_h),
                        model="claude-opus-4-7",
                        input_tokens=300_000, output_tokens=30_000,
                        cache_read=60_000,
                        session_id=f"last-s{i}",
                        project_path="/fake/repos/alpha")

    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a this-week --b last-week --width 144"\n'
    )


# -- Scenario 8b: mid-week-reset-with-event ---------------------------------

def build_mid_week_reset_with_event():
    """Mid-week subscription reset where the post-reset snapshot's
    `week_start_at` is cadence-aligned (= new_week_end - 7d), NOT
    the effective reset moment, AND a `week_reset_events` row carries
    the actual reset instant. Exercises the new reset-event override
    inside `_diff_resolve_anchor`.

    Without the override, `this-week` would span [04-19 12:00,
    04-26 12:00) (cadence boundary). With the override applied,
    `this-week` correctly spans [04-21 09:00, 04-26 12:00) (the real
    reset moment), so entries dated 04-19/04-20 land OUTSIDE Window A
    instead of inside it. Used % falls through to `n/a` for both
    windows because the lookup key (`window.start_utc.date()`) no
    longer matches the snapshot's `week_start_date` after override —
    matches the spec self-consistency note in `_diff_resolve_anchor`.
    """
    scenario_dir, stats_path, cache_path = _ensure_dir("mid-week-reset-with-event")

    orig_week_start = dt.datetime(2026, 4, 15, 19, 30, 0, tzinfo=dt.timezone.utc)
    orig_week_end = dt.datetime(2026, 4, 22, 19, 30, 0, tzinfo=dt.timezone.utc)
    new_week_end = dt.datetime(2026, 4, 26, 12, 0, 0, tzinfo=dt.timezone.utc)
    new_week_start_cadence = new_week_end - dt.timedelta(days=7)  # 04-19 12:00
    effective_reset_at = dt.datetime(2026, 4, 21, 9, 0, 0, tzinfo=dt.timezone.utc)
    last_week_start = effective_reset_at - dt.timedelta(days=7)  # 04-14 09:00
    as_of = new_week_end

    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        _ensure_week_reset_events_table(conn)
        # Pre-reset snapshot — captured before the reset, original boundaries.
        # Required so cumulative pct on prior week is 50% (drop to 5% in
        # post-reset snapshot triggers backfill detection consistently with
        # production but UNIQUE(old, new) ensures our explicit seed wins).
        _seed_anchor(conn,
                     captured_at=dt.datetime(2026, 4, 17, 10, 0, 0, tzinfo=dt.timezone.utc),
                     week_start=orig_week_start, week_end=orig_week_end,
                     weekly_percent=50.0)
        # Post-reset snapshot — newest. Stores the CADENCE-aligned
        # week_start_at (= new_week_end - 7d), NOT the actual reset
        # instant. The override below is what produces the real start.
        _seed_anchor(conn,
                     captured_at=effective_reset_at + dt.timedelta(hours=1),
                     week_start=new_week_start_cadence,
                     week_end=new_week_end,
                     weekly_percent=5.0)
        # Explicit reset event row carrying the real reset instant.
        # Backfill at open_db() time will INSERT OR IGNORE the same
        # (old, new) pair — our row wins and pins effective_reset_at.
        _seed_reset_event(
            conn,
            detected_at=effective_reset_at + dt.timedelta(hours=1),
            old_week_end=orig_week_end,
            new_week_end=new_week_end,
            effective_reset_at=effective_reset_at,
        )

    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # Entries in [04-19 12:00, 04-21 09:00) — would be in Window A
        # WITHOUT the override; with the override applied they fall in
        # the gap between Window A and Window B and are excluded from
        # both.
        for i, off_h in enumerate([2, 30]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/gap-{i}.jsonl",
                        line_offset=0,
                        ts=new_week_start_cadence + dt.timedelta(hours=off_h),
                        model="claude-opus-4-7",
                        input_tokens=200_000, output_tokens=20_000,
                        cache_read=40_000,
                        session_id=f"gap-s{i}",
                        project_path="/fake/repos/alpha")
        # Entries in Window A (post-override): [04-21 09:00, 04-26 12:00)
        for i, off_h in enumerate([5, 30, 70, 100]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/this-{i}.jsonl",
                        line_offset=0,
                        ts=effective_reset_at + dt.timedelta(hours=off_h),
                        model="claude-opus-4-7",
                        input_tokens=400_000, output_tokens=40_000,
                        cache_read=80_000,
                        session_id=f"this-s{i}",
                        project_path="/fake/repos/alpha")
        # Entries in Window B (last-week relative to overridden anchor):
        # [04-14 09:00, 04-21 09:00)
        for i, off_h in enumerate([12, 60, 110]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/last-{i}.jsonl",
                        line_offset=0,
                        ts=last_week_start + dt.timedelta(hours=off_h),
                        model="claude-opus-4-7",
                        input_tokens=300_000, output_tokens=30_000,
                        cache_read=60_000,
                        session_id=f"last-s{i}",
                        project_path="/fake/repos/alpha")

    # `--allow-mismatch` is required because the override compresses
    # Window A to ~5.1 days while Window B remains a full 7 days (the
    # bare fact that the windows differ in length is itself proof the
    # override path executed; without it both windows would be 7d).
    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a this-week --b last-week --allow-mismatch --width 144"\n'
    )


# -- Scenario 9: no-anchor ---------------------------------------------------

def build_no_anchor():
    """Empty `weekly_usage_snapshots`. `--a this-week --b last-week`
    raises NoAnchorError → cmd_diff prints the error to stderr and
    exits 1. Goldens capture the stderr message."""
    scenario_dir, stats_path, cache_path = _ensure_dir("no-anchor")

    as_of = dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc)

    # Empty stats.db (no snapshots). Empty cache.db. The cmd_diff path
    # raises NoAnchorError before any aggregation runs.
    create_stats_db(stats_path)
    create_cache_db(cache_path)

    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a this-week --b last-week --width 144"\n'
    )


# -- Scenario 10: cache-section-only ----------------------------------------

def build_cache_section_only():
    """`--a this-week --b last-week --only cache`. The terminal output
    contains only the Cache section (banner + window header + Cache table).
    Same anchor + entries as scenario 1 (so cache deltas are non-trivial)."""
    scenario_dir, stats_path, cache_path = _ensure_dir("cache-section-only")

    anchor_week_start = dt.datetime(2026, 4, 18, 19, 30, 0, tzinfo=dt.timezone.utc)
    anchor_resets_at = dt.datetime(2026, 4, 25, 19, 30, 0, tzinfo=dt.timezone.utc)
    last_week_start = anchor_week_start - dt.timedelta(days=7)
    as_of = anchor_resets_at

    create_stats_db(stats_path)
    with sqlite3.connect(stats_path) as conn:
        _seed_anchor(conn, captured_at=as_of - dt.timedelta(hours=1),
                     week_start=anchor_week_start, week_end=anchor_resets_at,
                     weekly_percent=60.0)
        _seed_anchor(conn, captured_at=anchor_week_start - dt.timedelta(hours=1),
                     week_start=last_week_start, week_end=anchor_week_start,
                     weekly_percent=40.0)

    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # This week: heavy cache reads
        for i, (ts, in_t, out_t, cw, cr) in enumerate([
            (anchor_week_start + dt.timedelta(days=1, hours=10),
             500_000, 50_000, 100_000, 300_000),
            (anchor_week_start + dt.timedelta(days=3, hours=11),
             300_000, 30_000, 50_000, 200_000),
        ]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/this-{i}.jsonl",
                        line_offset=0, ts=ts,
                        model="claude-opus-4-7",
                        input_tokens=in_t, output_tokens=out_t,
                        cache_create=cw, cache_read=cr,
                        session_id=f"this-s{i}",
                        project_path="/fake/repos/alpha")
        # Last week: less cache use
        for i, (ts, in_t, out_t, cw, cr) in enumerate([
            (last_week_start + dt.timedelta(days=1, hours=10),
             400_000, 40_000, 30_000, 80_000),
            (last_week_start + dt.timedelta(days=3, hours=11),
             200_000, 20_000, 20_000, 50_000),
        ]):
            _seed_entry(conn,
                        source_path=f"/fake/jsonl/last-{i}.jsonl",
                        line_offset=0, ts=ts,
                        model="claude-opus-4-7",
                        input_tokens=in_t, output_tokens=out_t,
                        cache_create=cw, cache_read=cr,
                        session_id=f"last-s{i}",
                        project_path="/fake/repos/alpha")

    (scenario_dir / "input.env").write_text(
        f'AS_OF={_iso(as_of)}\n'
        f'FLAGS="--a this-week --b last-week --only cache --width 144"\n'
    )


# -- Entry point -------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Override output directory. Defaults to the in-tree path "
            "tests/fixtures/diff/. Used by cctally-diff-test "
            "to write into a per-run scratch dir so the in-tree fixtures "
            "stay byte-stable across harness runs."
        ),
    )
    args = parser.parse_args()
    if args.out is not None:
        FIXTURES_DIR = args.out
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    build_same_length_week()
    build_auto_week_headline()
    build_with_new_and_dropped()
    build_mismatched_default()
    build_mismatched_allowed()
    build_month_vs_month()
    build_month_vs_month_full_coverage()
    build_arbitrary_range()
    build_noise_filter()
    build_mid_week_reset()
    build_mid_week_reset_with_event()
    build_no_anchor()
    build_cache_section_only()
    print(f"Built fixtures under {FIXTURES_DIR}")
