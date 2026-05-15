"""Unit tests for bin/build-readme-fixtures.py.

These tests verify the marketing fixture builder produces a deterministic,
schema-conformant DB tree. They do NOT require playwright/termtosvg —
those are integration concerns covered by bin/build-readme-screenshots.sh.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILDER = REPO_ROOT / "bin" / "build-readme-fixtures.py"


def _load_builder():
    """Load the builder module under a synthetic name (the file has no
    `.py` package layout; matches the project's other build-*-fixtures.py
    loader pattern)."""
    spec = importlib.util.spec_from_file_location("_builder", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_builder"] = mod
    spec.loader.exec_module(mod)
    return mod


def _isolated_build(mod, tmp_path, as_of_str="2026-05-05", subdir="home"):
    """Invoke the builder with both `out_dir` AND `tui_snapshot_path`
    redirected under tmp_path. Without an explicit `tui_snapshot_path`,
    the builder's default writes to the in-tree
    `tests/fixtures/readme/tui_snapshot.py`, leaking test side effects
    into the working tree and overwriting the committed marketing
    snapshot. Returns the resolved `out_dir`."""
    out = tmp_path / subdir / ".local" / "share" / "cctally"
    snap = tmp_path / subdir / "tui_snapshot.py"
    mod.build(out_dir=out, as_of_str=as_of_str, tui_snapshot_path=snap)
    return out


def test_builder_module_loads():
    mod = _load_builder()
    assert hasattr(mod, "build")
    assert hasattr(mod, "DEFAULT_AS_OF_FN")


def test_builder_writes_both_dbs(tmp_path):
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    assert (out / "stats.db").exists()
    assert (out / "cache.db").exists()


def test_both_dbs_have_wal(tmp_path):
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    for db_name in ("stats.db", "cache.db"):
        with sqlite3.connect(out / db_name) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal", f"{db_name}: {mode!r}"


def test_eight_weeks_of_usage_snapshots(tmp_path):
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    with sqlite3.connect(out / "stats.db") as conn:
        n = conn.execute(
            "SELECT COUNT(DISTINCT week_start_date) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    assert n == 8, f"expected 8 distinct week_start_date values, got {n}"


def test_four_projects_in_session_entries(tmp_path):
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    with sqlite3.connect(out / "cache.db") as conn:
        rows = conn.execute(
            "SELECT DISTINCT project_path FROM session_files "
            "WHERE project_path IS NOT NULL ORDER BY project_path"
        ).fetchall()
    projects = {r[0].rsplit("/", 1)[-1] for r in rows}
    assert projects == {"web-app", "api-gateway", "data-pipeline", "mobile-client"}, (
        f"unexpected projects: {projects}"
    )


def test_percent_milestones_present(tmp_path):
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    with sqlite3.connect(out / "stats.db") as conn:
        n = conn.execute("SELECT COUNT(*) FROM percent_milestones").fetchone()[0]
    assert n >= 5, f"expected at least 5 milestone rows, got {n}"


def test_five_hour_blocks_present(tmp_path):
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    with sqlite3.connect(out / "stats.db") as conn:
        n = conn.execute("SELECT COUNT(*) FROM five_hour_blocks").fetchone()[0]
    assert n >= 4, f"expected at least 4 block rows, got {n}"


def test_deterministic_for_fixed_as_of(tmp_path):
    mod = _load_builder()
    out_a = _isolated_build(mod, tmp_path, subdir="a")
    out_b = _isolated_build(mod, tmp_path, subdir="b")
    for db_name in ("stats.db", "cache.db"):
        # WAL mode complicates byte equality; compare normalized SQL dumps instead.
        with sqlite3.connect(out_a / db_name) as ca:
            dump_a = list(ca.iterdump())
        with sqlite3.connect(out_b / db_name) as cb:
            dump_b = list(cb.iterdump())
        assert dump_a == dump_b, f"{db_name}: builder is not deterministic"


def test_writes_tui_snapshot_module(tmp_path):
    mod = _load_builder()
    out = tmp_path / "home" / ".local" / "share" / "cctally"
    snap_path = tmp_path / "tui_snapshot.py"
    mod.build(out_dir=out, as_of_str="2026-05-05", tui_snapshot_path=snap_path)
    assert snap_path.exists()
    text = snap_path.read_text()
    assert "SNAPSHOT" in text, "snapshot module must export SNAPSHOT"


def test_session_entries_cover_all_projects_regardless_of_weekday(tmp_path):
    """Regression: the builder must seed ≥1 session_entries row per project
    no matter which weekday `as_of` falls on.

    Prior bug: `day_offset = (proj_idx + sess) % 6` could place a
    (project, session) pair's `base_dt` past `as_of` on Mon/Tue/Wed/Thu,
    causing the inner `if ts > as_of: break` to skip the entire session.
    Reviewer ran `as_of_str='2026-05-05'` (Tuesday) and observed
    `web-app: 16, api-gateway: 8, data-pipeline: 0, mobile-client: 0`.
    """
    mod = _load_builder()
    expected_projects = {"web-app", "api-gateway", "data-pipeline", "mobile-client"}
    # Mon..Sun anchored on a known calendar week (2026-05-04 = Monday).
    weekday_dates = [
        ("Mon", "2026-05-04"),
        ("Tue", "2026-05-05"),
        ("Wed", "2026-05-06"),
        ("Thu", "2026-05-07"),
        ("Fri", "2026-05-08"),
        ("Sat", "2026-05-09"),
        ("Sun", "2026-05-10"),
    ]
    for label, as_of_str in weekday_dates:
        out = _isolated_build(mod, tmp_path, as_of_str=as_of_str, subdir=label)
        with sqlite3.connect(out / "cache.db") as conn:
            rows = conn.execute(
                "SELECT DISTINCT sf.project_path "
                "FROM session_entries se "
                "JOIN session_files sf ON se.source_path = sf.path "
                "WHERE sf.project_path IS NOT NULL"
            ).fetchall()
        seen = {r[0].rsplit("/", 1)[-1] for r in rows}
        assert seen == expected_projects, (
            f"{label} ({as_of_str}): expected all 4 projects in session_entries, "
            f"got {sorted(seen)}"
        )


def test_cli_invocation_smoke():
    """The script is also runnable as a CLI."""
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--help"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--as-of" in result.stdout
    assert "--out" in result.stdout


def test_daily_panel_has_data_across_30_days(tmp_path):
    """Regression: Daily panel renders 30-day heatmap; ensure entries span all days."""
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    with sqlite3.connect(out / "cache.db") as conn:
        # Group entry timestamps by date (UTC) — count distinct days.
        days = conn.execute(
            "SELECT COUNT(DISTINCT substr(timestamp_utc, 1, 10)) FROM session_entries"
        ).fetchone()[0]
    assert days >= 28, f"expected ≥28 distinct days of session_entries, got {days}"


def test_each_project_has_at_least_five_entries(tmp_path):
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    with sqlite3.connect(out / "cache.db") as conn:
        rows = conn.execute(
            """SELECT sf.project_path, COUNT(*) FROM session_entries se
               JOIN session_files sf ON se.source_path = sf.path
               GROUP BY sf.project_path"""
        ).fetchall()
    counts = {r[0].rsplit("/", 1)[-1]: r[1] for r in rows}
    for proj in ("web-app", "api-gateway", "data-pipeline", "mobile-client"):
        assert counts.get(proj, 0) >= 5, f"{proj}: {counts.get(proj, 0)} entries (want ≥5)"


def test_dollar_per_percent_has_visible_variance(tmp_path):
    """Trend chart goes flat if all weeks have ~same $/1%; ensure variance ≥ $0.10.

    Joins one (latest) usage snapshot per `week_start_date` against the
    matching cost snapshot. The current week now has multiple snapshots
    (added in round-3 to lift forecast confidence), so we collapse with
    `MAX(captured_at_utc)` to pick the canonical "$/1% as of week end" -
    matching what the Trend chart renders.
    """
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    with sqlite3.connect(out / "stats.db") as conn:
        rows = conn.execute(
            """
            SELECT u.weekly_percent, c.cost_usd
              FROM weekly_usage_snapshots u
              JOIN weekly_cost_snapshots c
                ON u.week_start_date = c.week_start_date
             WHERE (u.week_start_date, u.captured_at_utc) IN (
                   SELECT week_start_date, MAX(captured_at_utc)
                     FROM weekly_usage_snapshots
                 GROUP BY week_start_date
             )
            """
        ).fetchall()
    ratios = [c / p for p, c in rows if p > 0]
    spread = max(ratios) - min(ratios)
    assert spread >= 0.10, f"$/1% spread {spread:.3f} < $0.10 (chart will look flat)"


def test_daily_panel_has_distinct_intensity_buckets(tmp_path):
    """Regression (round-3): Daily heatmap goes uniform-dark if every day
    lands in the same intensity bucket. Verify the per-day cost spread
    spans at least 4 distinct quintiles so the rendered grid shows
    visible color variance.

    `_compute_intensity_buckets` (bin/cctally) places days in bucket 1..5
    via quintile thresholds over non-zero costs. We replicate just enough
    of that math here to assert the cost vector is "interesting": the
    delta between min and max non-zero per-day cost is at least 2x.
    """
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    with sqlite3.connect(out / "cache.db") as conn:
        rows = conn.execute(
            """SELECT substr(timestamp_utc, 1, 10) AS d,
                      SUM(input_tokens + output_tokens
                          + cache_create_tokens + cache_read_tokens) AS toks
                 FROM session_entries
             GROUP BY d"""
        ).fetchall()
    nonzero = [t for _, t in rows if t > 0]
    assert len(nonzero) >= 28, f"expected ≥28 nonzero days, got {len(nonzero)}"
    spread_ratio = max(nonzero) / min(nonzero)
    assert spread_ratio >= 2.0, (
        f"per-day token spread {spread_ratio:.2f}x < 2x; heatmap will look uniform"
    )


def test_latest_snapshot_binds_to_open_block(tmp_path):
    """Regression (round-3): the dashboard's
    `_select_current_block_for_envelope` joins the latest
    `weekly_usage_snapshots.five_hour_window_key` against
    `five_hour_blocks.five_hour_window_key`. Without this binding the
    current-week panel falls back to the legacy single-big-number layout.
    """
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    with sqlite3.connect(out / "stats.db") as conn:
        latest_key = conn.execute(
            "SELECT five_hour_window_key FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()[0]
        assert latest_key is not None, (
            "latest weekly_usage_snapshots row has NULL five_hour_window_key; "
            "dashboard will not bind to a current 5h block"
        )
        match = conn.execute(
            "SELECT id, is_closed, five_hour_resets_at "
            "  FROM five_hour_blocks "
            " WHERE five_hour_window_key = ?",
            (latest_key,),
        ).fetchone()
        assert match is not None, (
            f"no five_hour_blocks row matches latest snapshot's "
            f"window_key={latest_key}"
        )
        is_closed, resets_at = match[1], match[2]
        assert is_closed == 0, (
            f"matched block id={match[0]} is_closed={is_closed} (must be 0; "
            f"selector filters on is_closed=0)"
        )
        # `five_hour_resets_at > now_utc` is enforced at envelope-build
        # time. The fixture builder normalizes `as_of` to Thursday 14:00
        # UTC; the open block runs Thursday 10:00 → 15:00, so resets_at
        # 15:00 must be strictly after 14:00. Spot-check that here.
        assert resets_at > "2026-05-07T14:00:00Z", (
            f"open block five_hour_resets_at={resets_at!r} not after fixture "
            f"as_of (Thursday 14:00 UTC)"
        )


def test_current_week_has_three_snapshots_for_high_confidence(tmp_path):
    """Regression (round-3): forecast `confidence` requires
    `snapshot_count >= 3` AND at least one sample with `captured_at_utc
    <= now-24h` (see `_assess_forecast_confidence` + the
    `has_sample_ge_24h` gate in `_load_forecast_inputs`). Verify the
    current week has three snapshots spanning ≥24h.
    """
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    # `as_of_str` Tuesday → Thursday 14:00 UTC anchor inside builder.
    as_of = "2026-05-07T14:00:00Z"
    twenty_four_ago = "2026-05-06T14:00:00Z"
    with sqlite3.connect(out / "stats.db") as conn:
        # Identify the current-week start_date by selecting the snapshot
        # whose captured_at_utc is closest to (and ≤) as_of.
        cur_week_start = conn.execute(
            "SELECT week_start_date FROM weekly_usage_snapshots "
            "WHERE captured_at_utc <= ? "
            "ORDER BY captured_at_utc DESC LIMIT 1",
            (as_of,),
        ).fetchone()[0]
        cur_rows = conn.execute(
            "SELECT captured_at_utc, weekly_percent FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "ORDER BY captured_at_utc",
            (cur_week_start,),
        ).fetchall()
    assert len(cur_rows) >= 3, (
        f"current week ({cur_week_start}) has {len(cur_rows)} snapshots; "
        f"forecast `confidence` requires ≥3"
    )
    # At least one sample older than 24h (the has_sample_ge_24h gate).
    has_old = any(captured <= twenty_four_ago for captured, _ in cur_rows)
    assert has_old, (
        f"no current-week snapshot with captured_at <= {twenty_four_ago}; "
        f"forecast will fall back to confidence='low' via no_sample_ge_24h"
    )


def test_config_json_is_byte_deterministic(tmp_path):
    """Builder-determinism extends to config.json (not just SQLite). The
    file uses a fixed `_MARKETING_FIXTURE_COLLECTOR_TOKEN` so back-to-back
    builds produce byte-identical JSON. Regression: switching to
    `secrets.token_hex(16)` would silently break this without flagging
    the existing SQLite-only determinism harness.
    """
    mod = _load_builder()
    out_a = _isolated_build(mod, tmp_path, subdir="a")
    out_b = _isolated_build(mod, tmp_path, subdir="b")
    assert (out_a / "config.json").read_bytes() == (out_b / "config.json").read_bytes(), (
        "config.json is not byte-deterministic across builds"
    )


def test_config_json_pins_la_display_tz(tmp_path):
    """Regression (round-3): screenshots must render in LA tz, not host
    TZ. The fixture builder writes config.json with display.tz pinned
    so dashboard + CLI surfaces resolve dates consistently across hosts.
    """
    mod = _load_builder()
    out = _isolated_build(mod, tmp_path)
    config_path = out / "config.json"
    assert config_path.exists(), f"{config_path} not written"
    import json
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    assert cfg.get("display", {}).get("tz") == "America/Los_Angeles", (
        f"display.tz mismatch in {config_path}: {cfg.get('display')}"
    )
