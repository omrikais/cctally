"""#620 S1 D1 — the projects snapshot cache is keyed by its real interval.

`bin/_lib_snapshot_cache.py` was structurally seven-day: a closed week was
keyed by its start alone and its end reconstructed as `start + 7d`, and the
current-week accumulator's identity was that same start. A usage-snapshot
advance deliberately did NOT invalidate cached cost, on the stated grounds
that boundaries cannot move.

Once the panel follows real resets that premise is false. An early reset
delivered mid-week moves a boundary, and the same start then names a
different interval — so a start-keyed cache serves an aggregate folded over
a window that no longer exists.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import shutil
import sqlite3
import sys

import pytest

from conftest import load_script  # noqa: E402

_NS = load_script()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _cctally_dashboard  # noqa: E402
import _lib_snapshot_cache as sc  # noqa: E402


_SRC = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "dashboard" / "non-monday-anchor"
    / ".local" / "share" / "cctally"
)

NOW_UTC = dt.datetime(2026, 4, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
W1_START = dt.datetime(2026, 4, 9, 9, 0, 0, tzinfo=dt.timezone.utc)
W0_START = dt.datetime(2026, 4, 16, 9, 0, 0, tzinfo=dt.timezone.utc)


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def store(tmp_path):
    """A writable copy of `non-monday-anchor`, so a test can deliver a late
    boundary change without touching the committed fixture."""
    app = tmp_path / "cctally"
    app.mkdir(parents=True)
    for name in ("stats.db", "cache.db"):
        shutil.copy2(_SRC / name, app / name)
    conn = sqlite3.connect(app / "stats.db")
    try:
        conn.execute("ATTACH DATABASE ? AS cache_db", (str(app / "cache.db"),))
        conn.execute(
            "CREATE TEMP VIEW session_entries AS "
            "SELECT * FROM cache_db.session_entries"
        )
        conn.execute(
            "CREATE TEMP VIEW session_files AS "
            "SELECT * FROM cache_db.session_files"
        )
        sc.reset_projects_env_state()
        _cctally_dashboard._projects_reset_memo()
        yield conn
    finally:
        conn.close()
        sc.reset_projects_env_state()
        _cctally_dashboard._projects_reset_memo()


def _build(conn, *, cached: bool):
    _cctally_dashboard._projects_reset_memo()
    return _cctally_dashboard._build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        use_projects_env_cache=cached,
    )


def _week_cost(env, start: dt.datetime) -> float:
    key = start.date().isoformat()
    for w in env["trend"]["weeks"]:
        if w["week_start_date"] == key:
            return w["total_cost_usd"]
    raise AssertionError(
        f"week {key} absent from trend {[w['week_start_date'] for w in env['trend']['weeks']]}"
    )


def _deliver_early_reset(conn, *, start: dt.datetime, end: dt.datetime,
                         pct: float):
    """Write one `weekly_usage_snapshots` row announcing a new week that
    begins mid-way through an existing one — the shape an early reset takes
    when Anthropic ends a cycle before its original `resets_at`."""
    conn.execute(
        "INSERT INTO weekly_usage_snapshots("
        "  captured_at_utc, week_start_date, week_end_date, week_start_at, "
        "  week_end_at, weekly_percent, source, payload_json, account_key) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            _iso(start + dt.timedelta(hours=1)),
            start.date().isoformat(), end.date().isoformat(),
            _iso(start), _iso(end), pct, "fixture",
            json.dumps({"fixture": True}), "unattributed",
        ),
    )
    conn.commit()


def test_warm_and_cold_agree(store):
    """A warm read and a cold read of the same window must publish the same
    per-project cost and the same `attributed_pct`, including across the
    fixture's six-day week — an aggregator that assumes `start + 7d` folds
    that week over the wrong span."""
    cold = _build(store, cached=False)
    sc.reset_projects_env_state()
    warm_miss = _build(store, cached=True)   # populates the cache
    warm_hit = _build(store, cached=True)    # serves it

    for label, got in (("warm-miss", warm_miss), ("warm-hit", warm_hit)):
        assert got["current_week"] == cold["current_week"], (
            f"{label} current_week diverged from the cold walk"
        )
        assert got["trend"] == cold["trend"], (
            f"{label} trend diverged from the cold walk"
        )


def test_late_boundary_change_invalidates_cached_cost(store):
    """A closed week whose end moves must be recomputed, not served.

    `non-monday-anchor`'s week 3 runs [2026-04-09T09:00Z, 2026-04-16T09:00Z)
    and holds four entries. An early reset announced at 2026-04-14T09:00Z
    ends it three days sooner, which drops the 2026-04-15T16:00Z entry out of
    it. A cache keyed on the start alone answers with the old four-entry
    aggregate, because the start did not move.
    """
    before = _build(store, cached=True)
    w1_before = _week_cost(before, W1_START)
    assert w1_before > 0.0

    _deliver_early_reset(
        store,
        start=dt.datetime(2026, 4, 14, 9, tzinfo=dt.timezone.utc),
        end=dt.datetime(2026, 4, 16, 9, tzinfo=dt.timezone.utc),
        pct=11.0,
    )

    after = _build(store, cached=True)
    w1_after = _week_cost(after, W1_START)
    fresh = _build(store, cached=False)
    w1_fresh = _week_cost(fresh, W1_START)

    assert w1_after == pytest.approx(w1_fresh, abs=1e-9), (
        "the cached closed week must be recomputed over its NEW interval: "
        f"cached {w1_after} vs freshly folded {w1_fresh}"
    )
    assert w1_after < w1_before, (
        "the shortened week must lose the entry that fell outside it: "
        f"{w1_after} is not less than {w1_before}"
    )


def test_the_week_key_distinguishes_two_intervals_sharing_a_start(store):
    """The cache identity must carry the interval, not just its start.

    This is the contract the two cost tests rest on, asserted directly so a
    future change that reverts the key to a bare start fails here first and
    unambiguously, rather than as a cost figure somewhere downstream.
    """
    start = dt.datetime(2026, 4, 9, 9, tzinfo=dt.timezone.utc)
    seven_day = sc.projects_env_week_key(
        start, start + dt.timedelta(days=7))
    five_day = sc.projects_env_week_key(
        start, start + dt.timedelta(days=5))
    assert seven_day != five_day, (
        "two intervals that share a start but not an end must not share a "
        f"cache key; both rendered as {seven_day!r}"
    )
    # The single-argument form every existing caller uses keeps meaning the
    # nominal seven-day week, so no caller changes behaviour.
    assert sc.projects_env_week_key(start) == seven_day


def test_current_week_accumulator_refolds_when_its_interval_moves(store):
    """The same rule for the CURRENT week, which is served by the single-slot
    accumulator rather than the closed-week cache.

    The accumulator's identity is the week key, and a stale slot keeps
    folding a running aggregate over a window that no longer exists. This
    covers the only shape a current-week boundary move can take: `now` must
    stay inside the current week, so an early reset that ends it moves the
    START as well, and the new start is what must reach the accumulator's
    identity. The end-only case is unreachable for the current week and is
    covered instead by the key-contract test above.
    """
    before = _build(store, cached=True)
    cw_before = before["current_week"]["total_cost_usd"]
    assert cw_before > 0.0

    _deliver_early_reset(
        store,
        start=dt.datetime(2026, 4, 21, 9, tzinfo=dt.timezone.utc),
        end=dt.datetime(2026, 4, 28, 9, tzinfo=dt.timezone.utc),
        pct=7.0,
    )

    after = _build(store, cached=True)
    fresh = _build(store, cached=False)

    assert after["current_week"]["total_cost_usd"] == pytest.approx(
        fresh["current_week"]["total_cost_usd"], abs=1e-9,
    ), (
        "the current-week accumulator must cold-refold when its interval "
        f"moves: warm {after['current_week']['total_cost_usd']} vs cold "
        f"{fresh['current_week']['total_cost_usd']}"
    )
