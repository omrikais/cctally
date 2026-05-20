"""Unit tests for `_build_projects_envelope` (spec §5.2, §6.2 / plan Task 1).

Drives `bin/build-projects-fixtures.py`'s three SQLite scenarios against
the envelope builder. Tests are pure-function (no fake HOME / monkeypatching
of `CACHE_DB_PATH`): the fixture DBs carry both cache-side
(``session_entries``, ``session_files``) and stats-side
(``weekly_usage_snapshots``) tables in one file, so a single
``sqlite3.connect()`` is sufficient.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys

import pytest

# `_cctally_dashboard` does `sys.modules["cctally"].BLOCK_DURATION` at
# import time, so the `cctally` namespace must be populated first.
# `conftest.load_script` registers it. Resolve the dashboard sibling
# *afterwards* so its module-level ``sys.modules["cctally"].X`` reads
# resolve cleanly.
from conftest import load_script  # noqa: E402


_NS = load_script()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _cctally_dashboard  # noqa: E402

_build_projects_envelope = _cctally_dashboard._build_projects_envelope


FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "projects"
NOW_UTC = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


def _open(path: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def test_current_week_rows_sorted_desc_by_cost():
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    rows = env["current_week"]["rows"]
    assert len(rows) >= 2
    costs = [r["cost_usd"] for r in rows]
    assert costs == sorted(costs, reverse=True), \
        f"rows not desc by cost: {costs}"


def test_current_week_total_matches_row_sum():
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    cw = env["current_week"]
    assert abs(
        cw["total_cost_usd"] - sum(r["cost_usd"] for r in cw["rows"])
    ) < 1e-9


def test_attributed_pct_none_when_no_snapshot():
    """Per spec §2.7: weeks without weekly_usage_snapshots → attributed_pct=None."""
    conn = _open(FIXTURE_DIR / "edge-cases.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    pcts = [r["attributed_pct"] for r in env["current_week"]["rows"]]
    # edge-cases fixture has no weekly_usage_snapshots row this week
    assert all(p is None for p in pcts), f"expected all-None: {pcts}"


def test_disambiguation_collision_keys():
    """`foo (repos)` vs `foo (forks)` in edge-cases.db."""
    conn = _open(FIXTURE_DIR / "edge-cases.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    keys = {r["key"] for r in env["current_week"]["rows"]}
    assert "foo (repos)" in keys, f"keys: {keys}"
    assert "foo (forks)" in keys, f"keys: {keys}"


def test_unknown_bucket_emitted():
    conn = _open(FIXTURE_DIR / "edge-cases.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    keys = {r["key"] for r in env["current_week"]["rows"]}
    assert "(unknown)" in keys, f"keys: {keys}"


def test_trend_weeks_oldest_to_newest():
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    trend = env["trend"]
    dates = [w["week_start_date"] for w in trend["weeks"]]
    assert dates == sorted(dates), f"weeks not oldest→newest: {dates}"


def test_trend_per_project_weekly_cost_aligned():
    """`weekly_cost[j]` index aligns with `weeks[j]`."""
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    n_weeks = len(env["trend"]["weeks"])
    for p in env["trend"]["projects"]:
        assert len(p["weekly_cost"]) == n_weeks
        assert len(p["weekly_pct"]) == n_weeks


def test_window_weeks_clamped_to_history():
    """`weeks_back=12` on a fixture whose entries cover ≤1 week →
    `window_weeks` reflects the actual emitted span (≤12)."""
    conn = _open(FIXTURE_DIR / "single-week.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    assert env["trend"]["window_weeks"] <= 12
    assert env["trend"]["window_weeks"] == len(env["trend"]["weeks"])


def test_determinism():
    """Same inputs → byte-identical output (memory: R-PROJ5 invariant)."""
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env_a = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    env_b = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    assert json.dumps(env_a, sort_keys=True) == json.dumps(env_b, sort_keys=True)


def test_memo_cache_hit_returns_same_object():
    """Pre-probe memo: second call with the same (max_id, cw_key,
    weeks_back) returns the IDENTICAL object (id() match), proving the
    inner aggregation walk did not re-run."""
    # Reset the memo so we measure a clean state.
    _cctally_dashboard._projects_reset_memo()
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env_a = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    env_b = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    # Cache HIT: the second call returns the very same dict.
    assert env_a is env_b, (
        "memo MUST return the same object reference on cache hit"
    )


def test_memo_invalidates_on_weeks_back_change():
    """Different `weeks_back` → different memo key → fresh aggregation."""
    _cctally_dashboard._projects_reset_memo()
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env_a = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    env_b = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=4,
    )
    assert env_a is not env_b
    # Smaller window: trend.window_weeks shrinks.
    assert env_b["trend"]["window_weeks"] <= 4


def test_current_week_rows_populated_after_midweek_reset():
    """B1 regression: ``TuiCurrentWeek.week_start_at`` shifted by
    ``_apply_midweek_reset_override`` (e.g. Friday 13:00 UTC after an
    Anthropic-shifted mid-week reset) MUST NOT empty out the panel.

    Buggy behavior: ``cw_start`` was set to the raw mid-week instant
    (Friday 00:00 UTC after a `.replace(microsecond=0)` snap), but the
    bucket aggregator anchors every entry to its ISO-Monday via
    ``_week_for``. The lookup ``buckets.get((bp, cw_start))`` then
    targeted Friday 00:00 UTC and missed every Monday-keyed bucket,
    yielding ``rows: []`` / ``total_cost_usd: 0.0``.

    Fixed behavior: ``cw_start`` is canonicalized to the containing
    ISO-Monday-UTC week start via ``_projects_week_start_monday_utc``,
    so the lookup matches whichever bucket the aggregator wrote.

    Authoritative CLAUDE.md memory: "``TuiCurrentWeek.week_start_at``
    is NOT a valid ``week_start_date`` lookup key after a mid-week
    reset."
    """
    from types import SimpleNamespace

    # multi-week.db has 4 projects active in the current week (the
    # NOW_UTC=2026-05-19 Tuesday-anchored bucket = Monday 2026-05-18 UTC).
    conn = _open(FIXTURE_DIR / "multi-week.db")

    # Mid-week reset instant: Friday 2026-05-22 13:00 UTC — same calendar
    # week as NOW_UTC, but a non-Monday boundary that the legacy code
    # would have stranded as the bucket-lookup key.
    midweek_reset_at = dt.datetime(
        2026, 5, 22, 13, 0, 0, tzinfo=dt.timezone.utc,
    )
    cw_stub = SimpleNamespace(week_start_at=midweek_reset_at)

    _cctally_dashboard._projects_reset_memo()
    env = _build_projects_envelope(
        conn,
        now_utc=NOW_UTC,
        current_week=cw_stub,
        weeks_back=12,
    )

    cw = env["current_week"]
    # Canonical Monday anchor: 2026-05-18 (the Monday containing both
    # NOW_UTC and the mid-week reset instant).
    assert cw["week_start_date"] == "2026-05-18", \
        f"cw_start should snap to Monday-UTC: {cw['week_start_date']}"
    assert cw["week_start_at"] == "2026-05-18T00:00:00Z", \
        f"cw_start ISO should be Monday-UTC 00:00:00Z: {cw['week_start_at']}"
    assert len(cw["rows"]) > 0, (
        "B1 regression: current_week.rows MUST be populated after a "
        f"mid-week reset; got {cw['rows']!r}"
    )
    assert cw["total_cost_usd"] > 0.0, (
        "B1 regression: current_week.total_cost_usd MUST be > 0 after "
        f"a mid-week reset; got {cw['total_cost_usd']!r}"
    )
    # Row-sum invariant still holds (mirrors test_current_week_total_matches_row_sum).
    assert abs(
        cw["total_cost_usd"] - sum(r["cost_usd"] for r in cw["rows"])
    ) < 1e-9


def test_memo_invalidates_on_new_session_entry():
    """A new row in `session_entries` bumps `MAX(id)` → memo must miss.

    This is the per-tick raison d'être of the memo (cache busts when fresh
    activity arrives between two sync ticks). Without this test the
    invalidation path is silently regressable.
    """
    import shutil
    import tempfile

    _cctally_dashboard._projects_reset_memo()
    # Copy multi-week.db to a temp file so we can mutate it freely.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = pathlib.Path(tmp.name)
    try:
        shutil.copyfile(FIXTURE_DIR / "multi-week.db", tmp_path)
        conn = _open(tmp_path)
        env_a = _build_projects_envelope(
            conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        )
        # Insert one new session_entries row. We don't care about the
        # numeric values — only that MAX(id) advances by 1.
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, "
            " input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("/tmp/synthetic.jsonl", 0,
             NOW_UTC.isoformat().replace("+00:00", "Z"),
             "claude-sonnet-4-5", 100, 100),
        )
        conn.commit()
        env_b = _build_projects_envelope(
            conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        )
        assert env_a is not env_b, (
            "memo MUST invalidate when MAX(session_entries.id) advances"
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_memo_invalidates_on_new_weekly_usage_snapshot():
    """A new row in `weekly_usage_snapshots` bumps `MAX(id)` → memo MUST
    miss so attributed_pct / trend total_pct reflect the fresh
    weekly_percent. Regression for code-review Fix 3: the throttled OAuth
    refresh path advances weekly_percent independently from
    session_entries writes; previously the memo only probed
    session_entries.MAX(id) and served stale attribution.
    """
    import shutil
    import tempfile

    _cctally_dashboard._projects_reset_memo()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = pathlib.Path(tmp.name)
    try:
        shutil.copyfile(FIXTURE_DIR / "multi-week.db", tmp_path)
        conn = _open(tmp_path)
        env_a = _build_projects_envelope(
            conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        )
        # Insert a synthetic weekly_usage_snapshots row. Match the
        # columns that exist in the fixture schema (table is shared with
        # the live DB shape; we only need MAX(id) to advance).
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(week_start_date, week_end_date, captured_at_utc, "
            " weekly_percent) "
            "VALUES (?, ?, ?, ?)",
            ("2026-05-18", "2026-05-25",
             NOW_UTC.isoformat().replace("+00:00", "Z"),
             42.0),
        )
        conn.commit()
        env_b = _build_projects_envelope(
            conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        )
        assert env_a is not env_b, (
            "memo MUST invalidate when MAX(weekly_usage_snapshots.id) "
            "advances (code-review Fix 3)"
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
