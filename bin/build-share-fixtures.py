#!/usr/bin/env python3
"""Build deterministic SQLite fixtures + CHANGELOG fixture for cctally-share-test.

Output layout (12 scenarios):
  tests/fixtures/share/<scenario>/
    cache.db
    stats.db
    CHANGELOG.md
    .gitignore (covers *.db-wal, *.db-shm)

Scenarios cover the share-enabled subcommands across formats:
  report-md, report-svg-light, report-svg-dark,
  daily-md, monthly-md, weekly-md, weekly-html,
  forecast-md, project-md-anon, project-md-reveal,
  five-hour-blocks-md, session-md.

The fixtures share one synthetic dataset:
  - 4 weeks of weekly_usage_snapshots / weekly_cost_snapshots ending 2026-05-04.
  - 4 session_entries spanning 2026-05-04..2026-05-07 across 3 projects (one
    NULL project_path → "(unknown)" bucket).
  - 1 five_hour_blocks row anchored at 2026-05-07T13:00:00Z, populated so
    `cmd_five_hour_blocks --format md` has a row to render.
  - CHANGELOG with version 9.9.9 so `_share_resolve_version()` returns a
    stable string regardless of the in-tree CHANGELOG.

Stats / cache schemas come from `_fixture_builders.create_stats_db` /
`create_cache_db` so all post-migration columns + indexes are baked in
(no first-open mutation, no dirty in-tree DBs).
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_stats_db,
    seed_session_entry,
    seed_session_file,
    seed_weekly_usage_snapshot,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "share"

SCENARIOS: tuple[str, ...] = (
    "report-md",
    "report-svg-light",
    "report-svg-dark",
    "daily-md",
    "monthly-md",
    "weekly-md",
    "weekly-html",
    "forecast-md",
    "project-md-anon",
    "project-md-reveal",
    "five-hour-blocks-md",
    "session-md",
)

# Synthetic 4-week trend ending 2026-05-04 (Monday). Tuples are
# (week_start_date, weekly_percent, cost_usd).
WEEKS: tuple[tuple[str, float, float], ...] = (
    ("2026-04-13", 65.2, 35.40),
    ("2026-04-20", 58.7, 28.90),
    ("2026-04-27", 71.0, 42.10),
    ("2026-05-04", 45.5, 19.80),
)

# Session entries spanning the most recent week. Tuples are
# (source_path, line_offset, ts_iso, model, in, out, cc, cr, project_path).
ENTRIES: tuple[tuple[str, int, str, str, int, int, int, int, str | None], ...] = (
    ("/fake/proj/alpha-internal/sess-a.jsonl", 0, "2026-05-04T10:00:00Z",
     "claude-sonnet-4-5", 1000, 2000, 0, 500, "/fake/proj/alpha-internal"),
    ("/fake/proj/alpha-internal/sess-a.jsonl", 1, "2026-05-05T11:00:00Z",
     "claude-sonnet-4-5", 800, 1500, 0, 400, "/fake/proj/alpha-internal"),
    ("/fake/proj/beta-public/sess-b.jsonl", 0, "2026-05-06T12:00:00Z",
     "claude-opus-4-5", 500, 800, 0, 200, "/fake/proj/beta-public"),
    ("/fake/proj/orphan/sess-c.jsonl", 0, "2026-05-07T13:00:00Z",
     "claude-sonnet-4-5", 300, 600, 0, 100, None),
)

# Files (one per source_path). Project paths align with ENTRIES.
SESSION_FILES: tuple[tuple[str, str | None, str | None], ...] = (
    ("/fake/proj/alpha-internal/sess-a.jsonl", "sess-aaaa",
     "/fake/proj/alpha-internal"),
    ("/fake/proj/beta-public/sess-b.jsonl", "sess-bbbb",
     "/fake/proj/beta-public"),
    ("/fake/proj/orphan/sess-c.jsonl", "sess-cccc", None),
)


def _seed_stats_db(path: pathlib.Path) -> None:
    """Stats.db: weekly_usage_snapshots + weekly_cost_snapshots + one
    five_hour_blocks row.

    `weekly_cost_snapshots` is fed for completeness — note `weekly` re-
    aggregates from session_entries at query time, so most renderers
    don't read this table. `report` does.

    `five_hour_blocks` carries the API-anchored block that
    `cmd_five_hour_blocks --format md` renders; without it, the share
    snapshot would be empty. Plan didn't cover this — added here per
    Implementor 10's findings on the actual data shape.
    """
    create_stats_db(path)
    with sqlite3.connect(path) as conn:
        for ws, used_pct, cost in WEEKS:
            # Compute week_end_date 7 days later — required NOT NULL on both tables.
            from datetime import date, timedelta
            ws_d = date.fromisoformat(ws)
            we_d = ws_d + timedelta(days=7)
            week_end_date = we_d.isoformat()
            # captured_at midweek for stable sort; no time-of-day jitter.
            captured = f"{ws}T12:00:00Z"
            seed_weekly_usage_snapshot(
                conn,
                captured_at_utc=captured,
                week_start_date=ws,
                week_end_date=week_end_date,
                week_start_at=f"{ws}T15:00:00Z",
                week_end_at=f"{week_end_date}T15:00:00Z",
                weekly_percent=used_pct,
            )
            conn.execute(
                "INSERT INTO weekly_cost_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, cost_usd, source, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, 'fixture', 'auto')",
                (f"{ws}T23:59:59Z", ws, week_end_date,
                 f"{ws}T15:00:00Z", f"{week_end_date}T15:00:00Z", cost),
            )

        # One 5h block — anchored 2026-05-07T13:00:00Z, the same instant as
        # the orphan session entry. Window key floors the epoch to 600s.
        # `5h reset` is +5h after block_start. crossed_seven_day_reset=0 to
        # keep the row visually quiet in the md golden.
        from datetime import datetime, timezone
        block_start = datetime(2026, 5, 7, 13, 0, 0, tzinfo=timezone.utc)
        epoch = int(block_start.timestamp())
        window_key = (epoch // 600) * 600
        block_start_iso = "2026-05-07T13:00:00Z"
        block_end_iso = "2026-05-07T18:00:00Z"
        conn.execute(
            """
            INSERT INTO five_hour_blocks (
                five_hour_window_key, five_hour_resets_at, block_start_at,
                first_observed_at_utc, last_observed_at_utc,
                final_five_hour_percent, seven_day_pct_at_block_start,
                seven_day_pct_at_block_end, crossed_seven_day_reset,
                total_input_tokens, total_output_tokens,
                total_cache_create_tokens, total_cache_read_tokens,
                total_cost_usd, is_closed,
                created_at_utc, last_updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (window_key, block_end_iso, block_start_iso,
             block_start_iso, block_start_iso,
             32.5, 44.0, 45.5,
             300, 600, 0, 100, 1.85,
             block_start_iso, block_start_iso),
        )
        conn.commit()


def _seed_cache_db(path: pathlib.Path) -> None:
    """Cache.db: session_files + session_entries.

    Powers `daily`, `monthly`, `weekly`, `project`, `session`,
    `five-hour-blocks` (rollup totals are recomputed from session_entries
    on every render, so we deliberately leave them aligned with the
    block-row we wrote in stats.db).
    """
    create_cache_db(path)
    with sqlite3.connect(path) as conn:
        for src, sess_id, proj in SESSION_FILES:
            seed_session_file(
                conn,
                path=src,
                session_id=sess_id,
                project_path=proj,
            )
        for src, line_offset, ts, model, ti, to_, cc, cr, _proj in ENTRIES:
            seed_session_entry(
                conn,
                source_path=src,
                line_offset=line_offset,
                timestamp_utc=ts,
                model=model,
                input_tokens=ti,
                output_tokens=to_,
                cache_create=cc,
                cache_read=cr,
                msg_id=f"msg-{src}-{line_offset}",
                req_id=f"req-{src}-{line_offset}",
            )
        conn.commit()


def _write_changelog(path: pathlib.Path) -> None:
    """Fixture CHANGELOG with a stable version stamp.

    `_release_read_latest_release_version()` reads the first
    `## [X.Y.Z] - YYYY-MM-DD` header, so 9.9.9 lands in every snapshot's
    `version` field regardless of what the in-tree CHANGELOG happens to
    look like during this run.
    """
    path.write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "## [9.9.9] - 2026-01-01\n\n"
        "- Fixture-stable version stamp.\n",
        encoding="utf-8",
    )


def _write_gitignore(scenario_dir: pathlib.Path) -> None:
    gitignore = scenario_dir / ".gitignore"
    gitignore.write_text("*.db-wal\n*.db-shm\n", encoding="utf-8")


def main() -> int:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    # Top-level .gitignore covers any per-scenario WAL/SHM files in case a
    # future scenario forgets its own. Per-scenario .gitignore is the
    # primary surface (each fixture dir is independently navigable).
    (FIXTURE_ROOT / ".gitignore").write_text(
        "*.db-wal\n*.db-shm\n",
        encoding="utf-8",
    )
    for scenario in SCENARIOS:
        scen_dir = FIXTURE_ROOT / scenario
        scen_dir.mkdir(parents=True, exist_ok=True)
        _seed_stats_db(scen_dir / "stats.db")
        _seed_cache_db(scen_dir / "cache.db")
        _write_changelog(scen_dir / "CHANGELOG.md")
        _write_gitignore(scen_dir)
        print(f"Built fixture: {scenario}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
