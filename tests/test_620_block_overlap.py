"""#620 S1 D8 / A4 — the Blocks panel and `/api/block/<iso>` must describe
the same blocks.

`_dashboard_build_blocks_view` filtered entries to `[week_start_at,
week_end_at)` BEFORE grouping them, so a native five-hour block straddling a
week boundary was folded from only the part of itself that fell inside the
week. `_handle_get_block_detail` fetches the block's own native window and
applies no week clip, so the two paths reported different totals for the
same `start_at`, and the panel's was permanently short.

Selecting a block that overlaps the week is the deliberate part of the
contract and stays; clipping a selected block's contents is the defect.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import shutil
import sys

import pytest

from conftest import load_script, redirect_paths


_SRC = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "dashboard" / "block-week-straddle"
    / ".local" / "share" / "cctally"
)

NOW_UTC = dt.datetime(2026, 4, 19, 12, 0, 0, tzinfo=dt.timezone.utc)
WEEK_START = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END = WEEK_START + dt.timedelta(days=7)

STRADDLE_START = dt.datetime(2026, 4, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
ADJACENT_START = dt.datetime(2026, 4, 13, 7, 0, 0, tzinfo=dt.timezone.utc)
IN_WEEK_START = dt.datetime(2026, 4, 15, 5, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    share = tmp_path / ".local" / "share" / "cctally"
    share.mkdir(parents=True, exist_ok=True)
    for name in ("stats.db", "cache.db"):
        shutil.copy2(_SRC / name, share / name)
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-04-19T12:00:00Z")
    return ns


def _panel_rows(ns):
    conn = ns["open_db"]()
    try:
        return ns["_dashboard_build_blocks_view"](
            conn, NOW_UTC,
            week_start_at=WEEK_START, week_end_at=WEEK_END,
            skip_sync=True,
        )
    finally:
        conn.close()


def _detail_total(ns, start_at: dt.datetime) -> float:
    """Reproduce `_handle_get_block_detail`'s arithmetic exactly: fetch the
    block's own native window, group, take the block whose `start_time`
    equals `start_at`, and sum every entry inside its native interval. No
    week clip, which is the whole point."""
    end_at = start_at + ns["BLOCK_DURATION"]
    recorded, overrides, canonical = ns["_load_recorded_five_hour_windows"](
        start_at - ns["BLOCK_DURATION"], end_at + ns["BLOCK_DURATION"],
    )
    entries_in_window = list(ns["get_entries"](start_at, end_at, skip_sync=True))
    blocks = ns["_group_entries_into_blocks"](
        entries_in_window, mode="auto",
        recorded_windows=recorded,
        block_start_overrides=overrides,
        canonical_intervals=canonical,
        now=NOW_UTC,
    )
    target = next(
        (b for b in blocks if (not b.is_gap) and b.start_time == start_at),
        None,
    )
    assert target is not None, (
        f"the detail route must find a block at {start_at.isoformat()}; got "
        f"{[b.start_time.isoformat() for b in blocks if not b.is_gap]}"
    )
    return target.cost_usd


def _row_for(view, start_at: dt.datetime):
    want = start_at.astimezone(dt.timezone.utc).isoformat()
    for r in view.rows:
        if r.start_at == want:
            return r
    return None


def test_the_fixture_really_straddles_the_week_boundary(app):
    """Guards the guard. If the straddling block held cost on only one side
    of the boundary, or the adjacent block fell outside the widened fetch,
    neither test below could detect anything."""
    ns = app
    before = list(ns["get_entries"](
        STRADDLE_START, WEEK_START, skip_sync=True))
    after = list(ns["get_entries"](
        WEEK_START, STRADDLE_START + ns["BLOCK_DURATION"], skip_sync=True))
    assert before, "the straddling block must hold cost BEFORE the week start"
    assert after, "the straddling block must hold cost AFTER the week start"

    fetch_start = WEEK_START - ns["BLOCK_DURATION"]
    assert ADJACENT_START + ns["BLOCK_DURATION"] <= WEEK_START, (
        "the adjacent block must NOT overlap the week"
    )
    adjacent_entries = list(ns["get_entries"](
        fetch_start, ADJACENT_START + ns["BLOCK_DURATION"], skip_sync=True))
    assert adjacent_entries, (
        "the adjacent block must hold an entry inside the panel's widened "
        "fetch, or an implementation that never groups it would pass by "
        "accident"
    )


def test_panel_and_detail_agree_on_a_straddling_block(app):
    """A4 — one `start_at`, one total."""
    ns = app
    view = _panel_rows(ns)
    row = _row_for(view, STRADDLE_START)
    assert row is not None, (
        f"the panel must list the straddling block; rows: "
        f"{[r.start_at for r in view.rows]}"
    )
    detail = _detail_total(ns, STRADDLE_START)
    assert row.cost_usd == pytest.approx(detail, abs=1e-9), (
        f"panel {row.cost_usd} != detail {detail} for the same block at "
        f"{STRADDLE_START.isoformat()}"
    )


def test_non_overlapping_block_is_not_listed(app):
    """The widened fetch is a candidate filter, not the selection. A block
    that falls inside it but does not overlap the week must not be listed,
    and its cost must not reach the panel's totals."""
    ns = app
    view = _panel_rows(ns)
    assert _row_for(view, ADJACENT_START) is None, (
        "a block that does not overlap the week must not be listed; rows: "
        f"{[r.start_at for r in view.rows]}"
    )
    listed = {r.start_at for r in view.rows}
    assert STRADDLE_START.isoformat() in listed
    assert IN_WEEK_START.isoformat() in listed
    assert view.total_cost_usd == pytest.approx(
        sum(r.cost_usd for r in view.rows), abs=1e-9,
    ), "panel totals must sum exactly the listed blocks"


def test_a_fully_in_week_block_is_unchanged(app):
    """The control. A block that never crosses the boundary must report the
    same total on both paths and must not move because of this change."""
    ns = app
    view = _panel_rows(ns)
    row = _row_for(view, IN_WEEK_START)
    assert row is not None
    assert row.cost_usd == pytest.approx(
        _detail_total(ns, IN_WEEK_START), abs=1e-9,
    )
