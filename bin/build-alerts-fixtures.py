#!/usr/bin/env python3
"""Build seeded SQLite fixtures for ``bin/cctally-alerts-test``.

Writes one ``cache.db`` + ``stats.db`` (+ optional ``config.json``) per
scenario under ``<out>/<scenario>/.local/share/cctally/``. All schema
goes through ``bin/_fixture_builders.py``; the alerted_at columns are
ALTER-added on top because the shared baseline pre-dates the
threshold-actions migration.

Idempotent — every scenario is rebuilt from scratch each run. Goldens
are NOT pre-baked: the alerts harness uses programmatic structural
assertions (``alerted_at IS NULL`` / NOT NULL, milestone-row counts,
alerts.log line counts, stderr warnings) rather than stdout text-diff
because the assertions are timestamp-bearing and structural rather
than render-shaped.

Each scenario also gets:
  * ``input.env`` — ``AS_OF`` (deterministic clock pin) plus per-scenario
    env vars (``CCTALLY_TEST_POPEN_FACTORY`` for osascript-missing, etc.)
    that the harness sources before invoking ``cctally record-usage``.
  * ``.gitignore`` — covers spawned config / lock / log / WAL files
    per CLAUDE.md fixture-scoped convention.

Run:
    python3 bin/build-alerts-fixtures.py [--out <dir>]

When ``--out`` is omitted, writes to the in-tree
``tests/fixtures/alerts/`` directory. ``cctally-alerts-test``
overrides ``--out`` with a per-run scratch dir to keep the in-tree
fixtures byte-stable.
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
    seed_week_reset_event,
    seed_weekly_usage_snapshot,
)

DEFAULT_FIXTURES_DIR = (
    Path(__file__).resolve().parent.parent / "tests/fixtures/alerts"
)

# Deterministic clock pins. AS_OF is what the harness exports as
# CCTALLY_AS_OF (does NOT thread into now_utc_iso() inside cmd_record_usage,
# but it's the canonical fixture-time anchor — matches the forecast/diff
# convention). Wall-clock fields written by the live record-usage call are
# normalized to sentinels at assertion time.
DEFAULT_AS_OF = "2026-04-29T14:30:00Z"

# Active subscription week (Mon-Mon). resets_at = the Monday-end UTC epoch.
WEEK_START = dt.datetime(2026, 4, 27, 14, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END = WEEK_START + dt.timedelta(days=7)
WEEK_START_ISO = WEEK_START.strftime("%Y-%m-%dT%H:%M:%SZ")
WEEK_END_ISO = WEEK_END.strftime("%Y-%m-%dT%H:%M:%SZ")
WEEK_START_DATE = WEEK_START.date().isoformat()
WEEK_END_DATE = WEEK_END.date().isoformat()
RESETS_AT_EPOCH = int(WEEK_END.timestamp())  # what record-usage --resets-at expects

# 5h block anchored partway through the week. resets_at_5h is the canonical
# epoch (rounded to 10-min floor by _canonical_5h_window_key).
FIVE_H_BLOCK_START = dt.datetime(2026, 4, 29, 10, 0, 0, tzinfo=dt.timezone.utc)
FIVE_H_RESETS_AT = FIVE_H_BLOCK_START + dt.timedelta(hours=5)
FIVE_H_RESETS_AT_EPOCH = int(FIVE_H_RESETS_AT.timestamp())


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_alerted_at_columns(stats_db: Path) -> None:
    """ALTER-add ``alerted_at`` to percent_milestones and five_hour_milestones.

    The shared schema in ``_fixture_builders.create_stats_db`` pre-dates
    the threshold-actions migration; production's ``open_db()`` adds
    these columns inline. We pre-apply them here so the fixture DB
    matches the live schema exactly and ``cctally record-usage`` against
    the fixture doesn't trigger a write-back-on-first-open that might
    flip header bytes.
    """
    with sqlite3.connect(stats_db) as conn:
        # idempotent: PRAGMA table_info → check before ALTER
        pm_cols = {r[1] for r in conn.execute("PRAGMA table_info(percent_milestones)")}
        if "alerted_at" not in pm_cols:
            conn.execute("ALTER TABLE percent_milestones ADD COLUMN alerted_at TEXT")
        fhm_cols = {r[1] for r in conn.execute("PRAGMA table_info(five_hour_milestones)")}
        if "alerted_at" not in fhm_cols:
            conn.execute("ALTER TABLE five_hour_milestones ADD COLUMN alerted_at TEXT")
        conn.commit()


def _seed_week_costs(stats_conn: sqlite3.Connection, *, captured_at: dt.datetime,
                     cost_usd: float) -> int:
    """Insert one weekly_cost_snapshots row; return its id."""
    cur = stats_conn.execute(
        "INSERT INTO weekly_cost_snapshots(captured_at_utc, week_start_date, "
        "week_end_date, week_start_at, week_end_at, cost_usd, source, mode) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            _iso(captured_at), WEEK_START_DATE, WEEK_END_DATE,
            WEEK_START_ISO, WEEK_END_ISO, cost_usd,
            "fixture", "auto",
        ),
    )
    return int(cur.lastrowid)


def _seed_session_entries_for_cost(cache_conn: sqlite3.Connection,
                                   *, total_cost_anchor_usd: float) -> None:
    """Seed a single session_entries row whose computed cost roughly anchors
    the fixture's $/% expectations.

    Cost computation reads CLAUDE_MODEL_PRICING at query time. For
    claude-sonnet-4-6: input=$3/M, output=$15/M. We pick token counts
    that yield a clean, recognizable USD figure. Exact pricing-day
    drift is fine — the alerts harness checks structural state
    (alerted_at presence, log lines), not USD totals.
    """
    seed_session_file(
        cache_conn,
        path="/fx/alerts/session-1.jsonl",
        session_id="alerts-fx-1",
        project_path="/fx/alerts",
        size_bytes=0,
        mtime_ns=0,
        last_byte_offset=0,
    )
    # 1M input + 800K output @ sonnet-4-6 ≈ $3 + $12 = $15 (recognizable).
    seed_session_entry(
        cache_conn,
        source_path="/fx/alerts/session-1.jsonl",
        line_offset=0,
        timestamp_utc=_iso(WEEK_START + dt.timedelta(hours=24)),
        model="claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=800_000,
        cache_create=0,
        cache_read=0,
    )


def _seed_percent_milestone(
    stats_conn: sqlite3.Connection,
    *,
    captured_at: dt.datetime,
    threshold: int,
    cumulative_cost_usd: float,
    usage_id: int,
    cost_id: int,
    alerted_at: "str | None" = None,
) -> int:
    """INSERT one percent_milestones row with optional alerted_at preset.

    Used by:
      * disabled-then-enabled (pre-seed crossing without alerted_at)
      * concurrent-record-usage (pre-seed alerted_at to verify the
        IS NULL guard prevents a second write)
      * mid-week-reset (pre-seed pre-reset crossings)
    """
    cur = stats_conn.execute(
        "INSERT INTO percent_milestones("
        "captured_at_utc, week_start_date, week_end_date, "
        "week_start_at, week_end_at, percent_threshold, "
        "cumulative_cost_usd, marginal_cost_usd, "
        "usage_snapshot_id, cost_snapshot_id, alerted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            _iso(captured_at), WEEK_START_DATE, WEEK_END_DATE,
            WEEK_START_ISO, WEEK_END_ISO, int(threshold),
            float(cumulative_cost_usd), None,
            int(usage_id), int(cost_id), alerted_at,
        ),
    )
    return int(cur.lastrowid)


# ─── Per-scenario builders ───────────────────────────────────────────


def _scenario_paths(out: Path, name: str) -> tuple[Path, Path, Path]:
    """Return (scenario_dir, app_dir, app_dir_logs).
    Creates directory structure idempotently."""
    scenario_dir = out / name
    app_dir = scenario_dir / ".local" / "share" / "cctally"
    logs_dir = app_dir / "logs"
    app_dir.mkdir(parents=True, exist_ok=True)
    return scenario_dir, app_dir, logs_dir


def _write_input_env(scenario_dir: Path, *, as_of: str = DEFAULT_AS_OF,
                     extra: "dict[str, str] | None" = None,
                     percent: float = 91.0,
                     five_hour_percent: "float | None" = None) -> None:
    """Write the per-scenario input.env file.

    Fields consumed by the harness:
      AS_OF                — exported as CCTALLY_AS_OF (analytic-time pin)
      PERCENT              — --percent passed to cctally record-usage
      RESETS_AT            — --resets-at (the weekly window epoch)
      FIVE_HOUR_PERCENT    — --five-hour-percent (optional)
      FIVE_HOUR_RESETS_AT  — --five-hour-resets-at (optional)
      ALERTED_AT_FIRST_RUN — fixture-pinned timestamp for pre-seeded
                              alerted_at rows (concurrent / disabled-
                              then-enabled scenarios). Sentinel-style
                              so the harness can verify the value is
                              preserved across the second invocation.
      EXTRA_ENV_<NAME>=<VAL> — any extra env var to export to the
                              record-usage subprocess (e.g.
                              CCTALLY_TEST_POPEN_FACTORY=raise_filenotfound).
    """
    lines = [
        f"AS_OF={as_of}",
        f"PERCENT={percent}",
        f"RESETS_AT={RESETS_AT_EPOCH}",
    ]
    if five_hour_percent is not None:
        lines.append(f"FIVE_HOUR_PERCENT={five_hour_percent}")
        lines.append(f"FIVE_HOUR_RESETS_AT={FIVE_H_RESETS_AT_EPOCH}")
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}={v}")
    (scenario_dir / "input.env").write_text("\n".join(lines) + "\n")


def _write_gitignore(scenario_dir: Path) -> None:
    """Per-fixture .gitignore covering spawned WAL/lock/log/config files
    (CLAUDE.md fixture-scoped convention)."""
    (scenario_dir / ".gitignore").write_text(
        "# Spawned by HOME=<scenario> at harness invocation time.\n"
        "*.db-wal\n"
        "*.db-shm\n"
        "*.lock\n"
        ".local/share/cctally/logs/\n"
        ".local/share/cctally/cache.db*.lock\n"
        ".local/share/cctally/stats.db*.lock\n"
    )


def _write_config(app_dir: Path, *, alerts_block: "dict | None" = None,
                  display_tz: str = "utc") -> None:
    """Write a deterministic config.json. ``alerts_block`` if provided
    becomes the top-level ``alerts`` key (deep-merged with display.tz)."""
    cfg: dict = {"display": {"tz": display_tz}}
    if alerts_block is not None:
        cfg["alerts"] = alerts_block
    (app_dir / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")


def _baseline_dbs(app_dir: Path) -> tuple[Path, Path]:
    """Create stats.db + cache.db with the shared schema + alerted_at
    migration applied. Returns (stats_path, cache_path)."""
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    _add_alerted_at_columns(stats_path)
    return stats_path, cache_path


def _seed_active_week_baseline(
    stats_conn: sqlite3.Connection,
    cache_conn: sqlite3.Connection,
    *,
    pct_just_below: float = 88.0,
    cost_just_below: float = 47.0,
) -> tuple[int, int]:
    """Seed the common 'almost-at-90%' week state shared by most scenarios.

    Writes:
      * 6 weekly_usage_snapshots ramping to ``pct_just_below``
      * 1 weekly_cost_snapshots row with ``cost_just_below``
      * baseline session_entries so cmd_record_usage's cumulative-cost
        re-read returns a non-trivial value during alert dispatch.
    Returns (last_usage_id, last_cost_id) for milestone foreign-key use.
    """
    last_usage_id = 0
    samples = [
        (12, 15.0), (24, 30.0), (36, 50.0), (48, 70.0),
        (54, 80.0), (60, pct_just_below),
    ]
    for hours_in, pct in samples:
        captured = WEEK_START + dt.timedelta(hours=hours_in)
        cur = stats_conn.execute(
            "INSERT INTO weekly_usage_snapshots(captured_at_utc, week_start_date, "
            "week_end_date, week_start_at, week_end_at, weekly_percent, source, "
            "payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                _iso(captured), WEEK_START_DATE, WEEK_END_DATE,
                WEEK_START_ISO, WEEK_END_ISO, pct,
                "fixture", json.dumps({"fixture": True}),
            ),
        )
        last_usage_id = int(cur.lastrowid)
    last_cost_id = _seed_week_costs(
        stats_conn, captured_at=WEEK_START + dt.timedelta(hours=60),
        cost_usd=cost_just_below,
    )
    _seed_session_entries_for_cost(cache_conn, total_cost_anchor_usd=cost_just_below)
    return last_usage_id, last_cost_id


# ─── 10 scenarios ────────────────────────────────────────────────────


def _build_disabled(out: Path) -> None:
    """Scenario 1: alerts.enabled=false; record-usage with a 90-crossing
    payload (89 pre-seeded → 91 live drives a 90 INSERT). Asserts
    alerted_at IS NULL on every milestone row, no log line written."""
    name = "disabled"
    scenario_dir, app_dir, _ = _scenario_paths(out, name)
    stats_path, cache_path = _baseline_dbs(app_dir)
    with sqlite3.connect(stats_path) as sc, sqlite3.connect(cache_path) as cc:
        last_usage_id, last_cost_id = _seed_active_week_baseline(sc, cc)
        _seed_percent_milestone(
            sc, captured_at=WEEK_START + dt.timedelta(hours=60),
            threshold=89, cumulative_cost_usd=46.5,
            usage_id=last_usage_id, cost_id=last_cost_id, alerted_at=None,
        )
        sc.commit()
        cc.commit()
    _write_config(app_dir, alerts_block={"enabled": False})
    _write_input_env(scenario_dir, percent=91.0)
    _write_gitignore(scenario_dir)


def _build_enabled_no_crossing(out: Path) -> None:
    """Scenario 2: alerts.enabled=true; under thresholds (88% → 89%);
    no new alerted_at writes."""
    name = "enabled-no-crossing"
    scenario_dir, app_dir, _ = _scenario_paths(out, name)
    stats_path, cache_path = _baseline_dbs(app_dir)
    with sqlite3.connect(stats_path) as sc, sqlite3.connect(cache_path) as cc:
        last_usage_id, last_cost_id = _seed_active_week_baseline(sc, cc)
        # Pre-seed milestone at 88% (no alert because not in {90, 95}) so
        # the 89% record-usage tick has a max_existing to step from.
        _seed_percent_milestone(
            sc, captured_at=WEEK_START + dt.timedelta(hours=60),
            threshold=88, cumulative_cost_usd=46.0,
            usage_id=last_usage_id, cost_id=last_cost_id, alerted_at=None,
        )
        sc.commit()
        cc.commit()
    _write_config(app_dir, alerts_block={"enabled": True,
                                          "weekly_thresholds": [90, 95]})
    _write_input_env(scenario_dir, percent=89.0)
    _write_gitignore(scenario_dir)


def _build_enabled_weekly_90(out: Path) -> None:
    """Scenario 3: enabled; weekly crosses 90%; assert alerted_at set on
    the 90 milestone + envelope-row equivalent.

    Pre-seeds the 89% milestone so the live tick at 91% advances
    start_threshold to 90, INSERTing 90 + 91 — only 90 is in
    weekly_thresholds, so exactly one dispatch fires.
    """
    name = "enabled-weekly-90"
    scenario_dir, app_dir, _ = _scenario_paths(out, name)
    stats_path, cache_path = _baseline_dbs(app_dir)
    with sqlite3.connect(stats_path) as sc, sqlite3.connect(cache_path) as cc:
        last_usage_id, last_cost_id = _seed_active_week_baseline(sc, cc)
        _seed_percent_milestone(
            sc, captured_at=WEEK_START + dt.timedelta(hours=60),
            threshold=89, cumulative_cost_usd=46.5,
            usage_id=last_usage_id, cost_id=last_cost_id, alerted_at=None,
        )
        sc.commit()
        cc.commit()
    _write_config(app_dir, alerts_block={"enabled": True,
                                          "weekly_thresholds": [90, 95]})
    _write_input_env(scenario_dir, percent=91.0)
    _write_gitignore(scenario_dir)


def _build_enabled_five_hour_95(out: Path) -> None:
    """Scenario 4: enabled; 5h crosses 95%; assert alerted_at on the 5h
    milestone + envelope row.

    Pre-seeds:
      * a five_hour_blocks parent row anchoring the active block
      * a five_hour_milestones row at 89% (alerted_at NULL) so the live
        tick at 96% advances start_threshold to 90 and INSERTs 90+91+
        ...+96. five_hour_thresholds=[90, 95] → 90 and 95 fire.

    Five-hour milestones are forward-only (CLAUDE.md: spec §4.3 +
    write-once gotcha) so the pre-seed is the only way to populate the
    catch-up max_existing without backfill.
    """
    name = "enabled-five-hour-95"
    scenario_dir, app_dir, _ = _scenario_paths(out, name)
    stats_path, cache_path = _baseline_dbs(app_dir)
    canonical_5h_key = (FIVE_H_RESETS_AT_EPOCH // 600) * 600
    with sqlite3.connect(stats_path) as sc, sqlite3.connect(cache_path) as cc:
        last_usage_id, _last_cost_id = _seed_active_week_baseline(sc, cc)
        # Seed a prior 5h snapshot so the canonical-key resolution in
        # cmd_record_usage finds an anchor (Tier 2 lookup against
        # weekly_usage_snapshots).
        cur = sc.execute(
            "INSERT INTO weekly_usage_snapshots(captured_at_utc, week_start_date, "
            "week_end_date, week_start_at, week_end_at, weekly_percent, "
            "five_hour_percent, five_hour_resets_at, five_hour_window_key, "
            "source, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                _iso(FIVE_H_BLOCK_START + dt.timedelta(hours=2)),
                WEEK_START_DATE, WEEK_END_DATE,
                WEEK_START_ISO, WEEK_END_ISO, 88.0,
                89.0, _iso(FIVE_H_RESETS_AT),
                canonical_5h_key,
                "fixture", json.dumps({"fixture": True}),
            ),
        )
        snapshot_id_5h = int(cur.lastrowid)
        # Seed the parent five_hour_blocks row.
        cur = sc.execute(
            "INSERT INTO five_hour_blocks("
            "five_hour_window_key, five_hour_resets_at, block_start_at, "
            "first_observed_at_utc, last_observed_at_utc, "
            "final_five_hour_percent, "
            "total_input_tokens, total_output_tokens, "
            "total_cache_create_tokens, total_cache_read_tokens, "
            "total_cost_usd, is_closed, "
            "created_at_utc, last_updated_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                canonical_5h_key, _iso(FIVE_H_RESETS_AT),
                _iso(FIVE_H_BLOCK_START),
                _iso(FIVE_H_BLOCK_START), _iso(FIVE_H_BLOCK_START + dt.timedelta(hours=2)),
                89.0,
                0, 0, 0, 0, 0.0, 0,
                _iso(FIVE_H_BLOCK_START), _iso(FIVE_H_BLOCK_START + dt.timedelta(hours=2)),
            ),
        )
        block_id = int(cur.lastrowid)
        # Seed 89% milestone so live 96% tick has max_existing=89,
        # advancing start_threshold to 90 and crossing 90, 95 in the
        # catch-up loop.
        sc.execute(
            "INSERT INTO five_hour_milestones("
            "block_id, five_hour_window_key, percent_threshold, "
            "captured_at_utc, usage_snapshot_id, "
            "block_input_tokens, block_output_tokens, "
            "block_cache_create_tokens, block_cache_read_tokens, "
            "block_cost_usd, marginal_cost_usd, "
            "seven_day_pct_at_crossing, alerted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                block_id, canonical_5h_key, 89,
                _iso(FIVE_H_BLOCK_START + dt.timedelta(hours=2)),
                snapshot_id_5h,
                0, 0, 0, 0, 0.0, None, 88.0, None,
            ),
        )
        sc.commit()
        cc.commit()
    _write_config(app_dir, alerts_block={"enabled": True,
                                          "weekly_thresholds": [90, 95],
                                          "five_hour_thresholds": [90, 95]})
    _write_input_env(
        scenario_dir,
        percent=89.0,  # below weekly thresholds — only 5h fires
        five_hour_percent=96.0,
    )
    _write_gitignore(scenario_dir)


def _build_multi_threshold_jump(out: Path) -> None:
    """Scenario 5: 88% → 96% in one tick. Two milestone INSERTs pass
    weekly thresholds {90, 95}; both should dispatch + carry alerted_at.
    Specifically asserts that two envelope-equivalent rows exist."""
    name = "multi-threshold-jump"
    scenario_dir, app_dir, _ = _scenario_paths(out, name)
    stats_path, cache_path = _baseline_dbs(app_dir)
    with sqlite3.connect(stats_path) as sc, sqlite3.connect(cache_path) as cc:
        last_usage_id, last_cost_id = _seed_active_week_baseline(sc, cc)
        # Pre-seed milestone at 87% so the jump-loop starts at 88 and
        # exercises the catch-up behavior crossing both 90 and 95.
        _seed_percent_milestone(
            sc, captured_at=WEEK_START + dt.timedelta(hours=60),
            threshold=87, cumulative_cost_usd=45.0,
            usage_id=last_usage_id, cost_id=last_cost_id, alerted_at=None,
        )
        sc.commit()
        cc.commit()
    _write_config(app_dir, alerts_block={"enabled": True,
                                          "weekly_thresholds": [90, 95]})
    _write_input_env(scenario_dir, percent=96.0)
    _write_gitignore(scenario_dir)


def _build_disabled_then_enabled(out: Path) -> None:
    """Scenario 6: alerts off, crossing already happened (alerted_at NULL),
    alerts on → second tick at same percent must NOT retroactively fire.

    Mechanism: the milestone row already exists (rowcount==0 on second
    INSERT OR IGNORE), so the alert-dispatch path in cmd_record_usage
    gates on ``inserted == 1`` and skips. Validates Q4 invariant
    (no retroactive arming).
    """
    name = "disabled-then-enabled"
    scenario_dir, app_dir, _ = _scenario_paths(out, name)
    stats_path, cache_path = _baseline_dbs(app_dir)
    with sqlite3.connect(stats_path) as sc, sqlite3.connect(cache_path) as cc:
        last_usage_id, last_cost_id = _seed_active_week_baseline(sc, cc)
        # Pre-seed the 90% milestone WITHOUT alerted_at (simulates the
        # "alerts were off when this crossed" history).
        _seed_percent_milestone(
            sc, captured_at=WEEK_START + dt.timedelta(hours=62),
            threshold=90, cumulative_cost_usd=47.32,
            usage_id=last_usage_id, cost_id=last_cost_id, alerted_at=None,
        )
        sc.commit()
        cc.commit()
    # Alerts ENABLED at fixture build time (the test runs a single
    # record-usage tick with alerts on; the asserted invariant is that
    # the pre-existing 90% milestone is NOT retroactively armed).
    _write_config(app_dir, alerts_block={"enabled": True,
                                          "weekly_thresholds": [90, 95]})
    _write_input_env(scenario_dir, percent=91.0)
    _write_gitignore(scenario_dir)


def _build_mid_week_reset(out: Path) -> None:
    """Scenario 7: mid-week reset. Original week had milestones keyed on
    the ORIGINAL ``week_start_date`` (per CLAUDE.md gotcha: milestone
    tables stay keyed on the pre-shift date even after the week_start_at
    shift). After reset, ``cmd_record_usage`` derives a NEW week_start_date
    from the post-shift resets_at, so milestone INSERTs under that NEW
    key form a fresh history; pre-existing milestones in the OLD-key
    history are orphaned (intentional per the spec's "new crossings post-
    reset re-arm because they're new milestone INSERTs").

    Pre-seeds:
      * a 89% milestone in the NEW week (post-shift week_start_date) so
        the live tick at 91% advances into 90 + 91 inside the new
        history.
      * a week_reset_events row so the cross-flag interval predicate has
        the right pre-state (not directly asserted, but the row's
        presence is part of the documented mid-week-reset surface).

    Asserts the 90 milestone fires alerted_at + log line in the new
    week's history while the original-key history stays orphaned.
    """
    name = "mid-week-reset"
    scenario_dir, app_dir, _ = _scenario_paths(out, name)
    stats_path, cache_path = _baseline_dbs(app_dir)
    new_week_end = WEEK_END + dt.timedelta(hours=12)  # reset shifts forward
    new_week_start = new_week_end - dt.timedelta(days=7)
    new_week_start_date = new_week_start.date().isoformat()
    new_week_end_date = new_week_end.date().isoformat()
    new_week_start_iso = _iso(new_week_start)
    new_week_end_iso = _iso(new_week_end)
    new_resets_at_epoch = int(new_week_end.timestamp())
    with sqlite3.connect(stats_path) as sc, sqlite3.connect(cache_path) as cc:
        last_usage_id, last_cost_id = _seed_active_week_baseline(sc, cc)
        seed_week_reset_event(
            sc,
            detected_at_utc=_iso(WEEK_START + dt.timedelta(hours=60)),
            old_week_end_at=WEEK_END_ISO,
            new_week_end_at=new_week_end_iso,
            effective_reset_at_utc=_iso(WEEK_START + dt.timedelta(hours=60)),
        )
        # Seed an 89% milestone keyed on the NEW week_start_date so the
        # live tick at 91% advances start_threshold to 90 in the new
        # history. cmd_record_usage queries
        #   SELECT MAX(percent_threshold) FROM percent_milestones
        #   WHERE week_start_date = ?
        # against the post-reset week_start_date — a milestone keyed on
        # the pre-reset date wouldn't satisfy the predicate.
        sc.execute(
            "INSERT INTO percent_milestones("
            "captured_at_utc, week_start_date, week_end_date, "
            "week_start_at, week_end_at, percent_threshold, "
            "cumulative_cost_usd, marginal_cost_usd, "
            "usage_snapshot_id, cost_snapshot_id, alerted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                _iso(new_week_start + dt.timedelta(hours=60)),
                new_week_start_date, new_week_end_date,
                new_week_start_iso, new_week_end_iso, 89,
                46.5, None, last_usage_id, last_cost_id, None,
            ),
        )
        sc.commit()
        cc.commit()
    _write_config(app_dir, alerts_block={"enabled": True,
                                          "weekly_thresholds": [90, 95]})
    # The harness passes the NEW resets_at_epoch (post-shift) so
    # cmd_record_usage's week-derivation reflects the shifted boundary.
    _write_input_env(
        scenario_dir,
        percent=91.0,
        extra={"NEW_RESETS_AT": str(new_resets_at_epoch)},
    )
    _write_gitignore(scenario_dir)


def _build_concurrent_record_usage(out: Path) -> None:
    """Scenario 8: simulates concurrent record-usage racing on the same
    crossing. Pre-seeds the 90 milestone WITH alerted_at SET (the first
    racer's commit), then runs record-usage with the same crossing — the
    INSERT OR IGNORE returns rowcount=0, the dispatch path is skipped,
    and the alerted_at value is unchanged. Validates the IS NULL guard
    against a second write under the race contract.

    NOTE: this is NOT a true fork-test. The actual race contract is
    exercised in two places:
      * insert_percent_milestone returns rowcount=1 vs 0 (covered by
        bin/cctally-percent-milestone-idempotency-test)
      * the alerted_at UPDATE has ``AND alerted_at IS NULL`` so a
        second writer can't overwrite a first writer's value.
    The spec acknowledges this trade-off: "two record-usage processes
    racing on the same crossing" is structurally equivalent to "one
    invocation against state where the row is already armed."
    """
    name = "concurrent-record-usage"
    scenario_dir, app_dir, _ = _scenario_paths(out, name)
    stats_path, cache_path = _baseline_dbs(app_dir)
    pinned_alerted_at = "2026-04-29T14:00:00Z"
    with sqlite3.connect(stats_path) as sc, sqlite3.connect(cache_path) as cc:
        last_usage_id, last_cost_id = _seed_active_week_baseline(sc, cc)
        _seed_percent_milestone(
            sc, captured_at=WEEK_START + dt.timedelta(hours=62),
            threshold=90, cumulative_cost_usd=47.32,
            usage_id=last_usage_id, cost_id=last_cost_id,
            alerted_at=pinned_alerted_at,  # pre-armed by "the first racer"
        )
        sc.commit()
        cc.commit()
    _write_config(app_dir, alerts_block={"enabled": True,
                                          "weekly_thresholds": [90, 95]})
    _write_input_env(
        scenario_dir,
        percent=91.0,
        extra={"PRE_ARMED_ALERTED_AT": pinned_alerted_at},
    )
    _write_gitignore(scenario_dir)


def _build_unknown_config_key(out: Path) -> None:
    """Scenario 9: alerts.unknown_key=foo; one stderr warning emitted by
    _get_alerts_config; record-usage proceeds (no exit 2). Pre-seeds 89%
    so the 91% live tick crosses 90 and exercises the dispatch path
    (proves the warn is non-fatal)."""
    name = "unknown-config-key"
    scenario_dir, app_dir, _ = _scenario_paths(out, name)
    stats_path, cache_path = _baseline_dbs(app_dir)
    with sqlite3.connect(stats_path) as sc, sqlite3.connect(cache_path) as cc:
        last_usage_id, last_cost_id = _seed_active_week_baseline(sc, cc)
        _seed_percent_milestone(
            sc, captured_at=WEEK_START + dt.timedelta(hours=60),
            threshold=89, cumulative_cost_usd=46.5,
            usage_id=last_usage_id, cost_id=last_cost_id, alerted_at=None,
        )
        sc.commit()
        cc.commit()
    _write_config(app_dir, alerts_block={
        "enabled": True,
        "weekly_thresholds": [90, 95],
        "unknown_key": "foo",  # forward-compat warn-and-ignore
    })
    _write_input_env(scenario_dir, percent=91.0)
    _write_gitignore(scenario_dir)


def _build_osascript_missing(out: Path) -> None:
    """Scenario 10: CCTALLY_TEST_POPEN_FACTORY=raise_filenotfound flips the
    Popen factory inside _dispatch_alert_notification to one that raises
    FileNotFoundError. Pre-seeds 89% so the 91% live tick INSERTs 90 and
    exercises the dispatch path. Asserts:
      * milestone INSERT committed (alerted_at SET — set-then-dispatch)
      * alerts.log line written with status "spawn_error: FileNotFoundError: ..."
      * record-usage exit 0 (spawn errors must NOT fail the parent)
    """
    name = "osascript-missing"
    scenario_dir, app_dir, _ = _scenario_paths(out, name)
    stats_path, cache_path = _baseline_dbs(app_dir)
    with sqlite3.connect(stats_path) as sc, sqlite3.connect(cache_path) as cc:
        last_usage_id, last_cost_id = _seed_active_week_baseline(sc, cc)
        _seed_percent_milestone(
            sc, captured_at=WEEK_START + dt.timedelta(hours=60),
            threshold=89, cumulative_cost_usd=46.5,
            usage_id=last_usage_id, cost_id=last_cost_id, alerted_at=None,
        )
        sc.commit()
        cc.commit()
    _write_config(app_dir, alerts_block={"enabled": True,
                                          "weekly_thresholds": [90, 95]})
    _write_input_env(
        scenario_dir,
        percent=91.0,
        extra={"EXTRA_ENV_CCTALLY_TEST_POPEN_FACTORY": "raise_filenotfound"},
    )
    _write_gitignore(scenario_dir)


SCENARIOS = (
    ("disabled", _build_disabled),
    ("enabled-no-crossing", _build_enabled_no_crossing),
    ("enabled-weekly-90", _build_enabled_weekly_90),
    ("enabled-five-hour-95", _build_enabled_five_hour_95),
    ("multi-threshold-jump", _build_multi_threshold_jump),
    ("disabled-then-enabled", _build_disabled_then_enabled),
    ("mid-week-reset", _build_mid_week_reset),
    ("concurrent-record-usage", _build_concurrent_record_usage),
    ("unknown-config-key", _build_unknown_config_key),
    ("osascript-missing", _build_osascript_missing),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_FIXTURES_DIR,
        help="Output directory (default: in-tree tests/fixtures/alerts/).",
    )
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in SCENARIOS:
        fn(out)
        print(f"built: {name}", file=sys.stderr)
    # Top-level .gitignore for the alerts/ tree (matches dashboard/).
    if out == DEFAULT_FIXTURES_DIR:
        toplevel = out / ".gitignore"
        if not toplevel.exists():
            toplevel.write_text(
                "# Runtime artifacts spawned when bin/cctally-alerts-test runs\n"
                "# under HOME=<scratch>/<scenario>. The committed per-scenario\n"
                "# tree is:\n"
                "#   <scenario>/input.env\n"
                "#   <scenario>/.gitignore\n"
                "#   <scenario>/.local/share/cctally/{stats,cache}.db\n"
                "#   <scenario>/.local/share/cctally/config.json (some scenarios)\n"
                "*.db-wal\n"
                "*.db-shm\n"
                "*.log\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
