#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally budget`.

Writes one fixture tree per scenario under
`tests/fixtures/budget/<scenario>/` (or a `--out` scratch dir for the
golden harness). Each tree has:
  - `.local/share/cctally/stats.db`   (weekly_usage_snapshots — the window anchor)
  - `.local/share/cctally/cache.db`   (session_entries — drives live spend)
  - `.local/share/cctally/config.json`(the `budget` block under test)
  - `input.env`                       (AS_OF for the deterministic clock)

Spend is driven entirely by `session_entries`, NOT by snapshot percent —
`cctally budget` recomputes live cost via `_sum_cost_for_range`. Each entry
is 100k input + 100k output tokens on `claude-sonnet-4-6`, which costs
exactly $1.80 (100k * $3/M + 100k * $15/M = $0.30 + $1.50). So a scenario's
target dollar spend is `n_entries * $1.80`, with `n_recent` of those placed
inside the trailing 24h so the recent-rate projection band is deterministic.

Verdict shapes (target $300, default thresholds 90/100) confirmed against
the kernel before committing:
  under   : 96h elapsed, 70 entries → $126 (42%), projection ≤ 270 → ok
  warn    : 120h elapsed, 112 entries → $201.60, projection in [270,300) → warn
  over    : 120h elapsed, 185 entries → $333 (>target) → over
  low-conf: 12h elapsed, 10 entries → $18, elapsed_fraction < 0.15 → low_confidence
  no-budget: config carries no weekly_usd → friendly no-budget status

Run: `bin/build-budget-fixtures.py` (idempotent — overwrites existing DBs).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_stats_db,
    seed_session_entry,
    seed_session_file,
    seed_weekly_usage_snapshot,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/budget"

# Subscription-week window shared by every scenario. 7-day window anchored
# on a Tuesday 14:00 UTC (matches the forecast fixtures' anchor style).
WEEK_START = dt.datetime(2026, 5, 26, 14, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END = WEEK_START + dt.timedelta(days=7)

# Per-entry cost: 100k input + 100k output on claude-sonnet-4-6 == $1.80.
_ENTRY_INPUT = 100_000
_ENTRY_OUTPUT = 100_000
ENTRY_USD = 1.80


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_window_snapshot(stats_conn, *, weekly_percent: float, as_of: dt.datetime) -> None:
    """Seed one boundary-aware weekly_usage_snapshots row so
    `_fetch_current_week_snapshots` resolves the window. The percent value is
    irrelevant to budget spend (spend comes from session_entries) but must be
    present for the window to anchor."""
    captured = min(as_of, WEEK_START + dt.timedelta(hours=6))
    seed_weekly_usage_snapshot(
        stats_conn,
        captured_at_utc=_iso(captured),
        week_start_date=WEEK_START.date().isoformat(),
        week_end_date=(WEEK_END - dt.timedelta(seconds=1)).date().isoformat(),
        week_start_at=_iso(WEEK_START),
        week_end_at=_iso(WEEK_END),
        weekly_percent=weekly_percent,
        source="fixture",
        payload_json=json.dumps({"fixture": True}),
    )


def _seed_entries(cache_conn, *, n_total: int, n_recent: int, as_of: dt.datetime) -> None:
    """Seed `n_total` session_entries summing to `n_total * $1.80`.

    `n_recent` of them land inside the trailing 24h before `as_of` (driving
    `recent_24h_usd`); the remainder are spread across the earlier part of the
    window. Each is 100k input + 100k output on claude-sonnet-4-6 ($1.80)."""
    recent_anchor = as_of - dt.timedelta(hours=12)  # mid trailing-24h
    early_anchor = WEEK_START + dt.timedelta(hours=3)
    idx = 0
    for _ in range(n_recent):
        seed_session_entry(
            cache_conn,
            source_path=f"/fx/budget-session-{idx}.jsonl",
            line_offset=idx,
            timestamp_utc=_iso(recent_anchor),
            model="claude-sonnet-4-6",
            input_tokens=_ENTRY_INPUT,
            output_tokens=_ENTRY_OUTPUT,
        )
        idx += 1
    for _ in range(max(0, n_total - n_recent)):
        seed_session_entry(
            cache_conn,
            source_path=f"/fx/budget-session-{idx}.jsonl",
            line_offset=idx,
            timestamp_utc=_iso(early_anchor),
            model="claude-sonnet-4-6",
            input_tokens=_ENTRY_INPUT,
            output_tokens=_ENTRY_OUTPUT,
        )
        idx += 1


def _seed_project_entries(cache_conn, *, projects: dict, as_of: dt.datetime) -> None:
    """Seed per-project session entries for the per-project budget scenario.

    ``projects`` maps a canonical git-root path → entry count. Each project's
    entries share one ``session_files`` row carrying ``project_path`` (the
    git-root), so ``get_claude_session_entries``' LEFT JOIN surfaces it and
    ``_resolve_project_key`` buckets the entry under that root. Each entry is
    100k input + 100k output on claude-sonnet-4-6 ($1.80) placed early in the
    week — the per-project verdict is projection-based but with all spend early
    the recent-rate band stays modest, keeping verdicts deterministic."""
    early_anchor = WEEK_START + dt.timedelta(hours=3)
    idx = 0
    for i, (root, n) in enumerate(projects.items()):
        src = f"/fx/budget-project-{i}.jsonl"
        seed_session_file(
            cache_conn, path=src, session_id=f"proj-s{i}", project_path=root,
        )
        for _ in range(n):
            seed_session_entry(
                cache_conn,
                source_path=src,
                line_offset=idx,
                timestamp_utc=_iso(early_anchor),
                model="claude-sonnet-4-6",
                input_tokens=_ENTRY_INPUT,
                output_tokens=_ENTRY_OUTPUT,
            )
            idx += 1


def _build(name: str, *, as_of: dt.datetime, budget_block: dict,
           n_total: int, n_recent: int, weekly_percent: float,
           seed_data: bool = True, project_entries: dict | None = None) -> None:
    out_dir = FIXTURES_DIR / name
    app_dir = out_dir / ".local" / "share" / "cctally"
    app_dir.mkdir(parents=True, exist_ok=True)

    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    # Full production schema + WAL via the shared builders (they also call
    # register_fixture_db() so the atexit writer-version zero-out runs).
    create_stats_db(stats_path)
    create_cache_db(cache_path)

    stats_conn = sqlite3.connect(stats_path)
    cache_conn = sqlite3.connect(cache_path)
    if seed_data:
        _seed_window_snapshot(stats_conn, weekly_percent=weekly_percent, as_of=as_of)
        if project_entries is not None:
            _seed_project_entries(cache_conn, projects=project_entries, as_of=as_of)
        else:
            _seed_entries(cache_conn, n_total=n_total, n_recent=n_recent, as_of=as_of)
    stats_conn.commit(); stats_conn.close()
    cache_conn.commit(); cache_conn.close()

    # config.json carries the budget block under test + display.tz=utc so
    # goldens render UTC suffixes regardless of host TZ.
    cfg = {"display": {"tz": "utc"}}
    if budget_block is not None:
        cfg["budget"] = budget_block
    (app_dir / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")

    # input.env carries only AS_OF; FAKE_HOME is derived from the fixture dir
    # at harness time, so absolute paths stay out of the committed file.
    (out_dir / "input.env").write_text(f"AS_OF={_iso(as_of)}\n")


SCENARIOS = {
    # 96h elapsed (~57% of week), $126 spent (42% of $300) → ok.
    "under": dict(
        as_of=WEEK_START + dt.timedelta(hours=96),
        budget_block={"weekly_usd": 300.0, "alerts_enabled": True,
                      "alert_thresholds": [90, 100]},
        n_total=70, n_recent=10, weekly_percent=40.0,
    ),
    # 120h elapsed (~71% of week), $201.60 spent (67%) → projection [270,300) → warn.
    "warn": dict(
        as_of=WEEK_START + dt.timedelta(hours=120),
        budget_block={"weekly_usd": 300.0, "alerts_enabled": True,
                      "alert_thresholds": [90, 100]},
        n_total=112, n_recent=12, weekly_percent=68.0,
    ),
    # 120h elapsed, $333 spent (>target) → over; both thresholds crossed.
    "over": dict(
        as_of=WEEK_START + dt.timedelta(hours=120),
        budget_block={"weekly_usd": 300.0, "alerts_enabled": True,
                      "alert_thresholds": [90, 100]},
        n_total=185, n_recent=20, weekly_percent=95.0,
    ),
    # 120h elapsed (~71% of week, remaining 48h), $180 spent (60% of $300),
    # but 40 of the 100 entries land in the trailing 24h so the RECENT rate
    # ($3.00/h) runs hotter than the WEEK-AVERAGE rate ($1.50/h). This makes
    # the displayed verdict band high end (projected_eow_high == spent +
    # rate_recent*remaining = $324) diverge from the week-average projection
    # (week_avg_projection_usd == spent + rate_avg*remaining = $252). It is the
    # non-vacuity fixture for the reconcile invariant
    # `projected_alert_eq_displayed_week_avg`: the projected-pace alert fires on
    # $252, NOT the displayed $324, so a regression that bound the alert/output
    # field to the verdict high end would fail the invariant here. Not
    # low-confidence (elapsed_fraction ~0.71 ≥ 0.15, spent > 0).
    "recent-hot": dict(
        as_of=WEEK_START + dt.timedelta(hours=120),
        budget_block={"weekly_usd": 300.0, "alerts_enabled": True,
                      "alert_thresholds": [90, 100]},
        n_total=100, n_recent=40, weekly_percent=60.0,
    ),
    # 12h elapsed (<15% → low_confidence), $18 spent.
    "low-conf": dict(
        as_of=WEEK_START + dt.timedelta(hours=12),
        budget_block={"weekly_usd": 300.0, "alerts_enabled": True,
                      "alert_thresholds": [90, 100]},
        n_total=10, n_recent=10, weekly_percent=5.0,
    ),
    # No weekly_usd → friendly "no budget set" status. Window/entries still
    # seeded so the no-budget path is exercised independent of data presence.
    "no-budget": dict(
        as_of=WEEK_START + dt.timedelta(hours=96),
        budget_block={"alerts_enabled": True, "alert_thresholds": [90, 100]},
        n_total=20, n_recent=5, weekly_percent=20.0,
    ),
    # Per-project budgets (#19/#121, spec §7). Three git-roots:
    #   alpha — 10 entries → $18.00 on a $15 budget → 120% → over
    #   beta  —  5 entries → $9.00  on a $20 budget → 45%  → ok
    #   gamma —  0 entries → $0.00  on a $50 budget → 0%   → ok (LOW CONF)
    # gamma exercises the deleted/moved/never-matched no-spend row (spec §7.2).
    # A global weekly_usd is set too so the per-project section renders BELOW
    # the global status block. 96h elapsed so the section is well past LOW CONF
    # for the projects that have spend. Sorted by Used % desc → alpha, beta, gamma.
    "per-project": dict(
        as_of=WEEK_START + dt.timedelta(hours=96),
        budget_block={
            "weekly_usd": 300.0, "alerts_enabled": True,
            "alert_thresholds": [90, 100],
            "projects": {
                "/fake/repos/alpha": 15.0,
                "/fake/repos/beta": 20.0,
                "/fake/repos/gamma": 50.0,
            },
        },
        n_total=0, n_recent=0, weekly_percent=40.0,
        project_entries={
            "/fake/repos/alpha": 10,
            "/fake/repos/beta": 5,
            "/fake/repos/gamma": 0,
        },
    ),
    # Same-basename collision (IMPORTANT-1 regression). Two DISTINCT git-roots
    # share the basename `app`:
    #   /fake/work/app     —  9 entries → $16.20 on a $15 budget → 108% → over
    #   /fake/personal/app —  4 entries → $7.20  on a $20 budget → 36%  → ok
    # A bare `display_key` renders BOTH as `app` (terminal/JSON indistinct) and
    # collapses BOTH to a single `project-1` in anonymized share. Routing the
    # labels through `_project_disambiguate_labels` suffixes the parent-dir
    # segment → `app (work)` / `app (personal)`, so terminal/JSON rows are
    # distinguishable AND anon share gives each its own project-N number,
    # spend-ranked (work $16.20 → project-1, personal $7.20 → project-2).
    "per-project-collision": dict(
        as_of=WEEK_START + dt.timedelta(hours=96),
        budget_block={
            "weekly_usd": 300.0, "alerts_enabled": True,
            "alert_thresholds": [90, 100],
            "projects": {
                "/fake/work/app": 15.0,
                "/fake/personal/app": 20.0,
            },
        },
        n_total=0, n_recent=0, weekly_percent=40.0,
        project_entries={
            "/fake/work/app": 9,
            "/fake/personal/app": 4,
        },
    ),
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Override output directory (defaults to tests/fixtures/budget/). "
             "cctally-budget-test writes into a per-run scratch dir so the "
             "in-tree fixtures stay byte-stable.",
    )
    args = parser.parse_args()
    if args.out is not None:
        FIXTURES_DIR = args.out
    for sc_name, sc_kwargs in SCENARIOS.items():
        _build(sc_name, **sc_kwargs)
        print(f"built: {sc_name}")
