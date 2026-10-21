"""The TUI Current Week panel's 5-hour credit badge fits the panel (#825/#832,
UI-QA finding on fix round K16c).

The 5-hour row is " 5-hour " + bar + " NNN%" + the credit badge
(" ⚡ -50pp", one delta per credit). With the bar at the Used row's width
(panel width - 20) the row leaves five cells of the interior (width - 2)
free, and the badge's wide ⚡ glyph alone costs two cells, so a one-credit
badge overflowed by four cells in the 120-column layout: the delta wrapped
onto the next row and the Forecast panel lost its left border. The 5-hour
bar now gives up the cells the badge needs, and keeps the Used row's width
when there is no badge.

The box around the panel also padded each row by CHARACTERS, so even a
fitting badge row came out one cell wider than its column (the ⚡ is one
character, two cells), in the conventional panel and in the expressive
hero alike. `_tui_tagged_box_lines` now pads by cells; every boxed row is
exactly the column's width.
"""
import dataclasses
import datetime as dt
import importlib

import pytest

from conftest import load_script, redirect_paths

NOW = dt.datetime(2026, 10, 12, 5, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def mods(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns, importlib.import_module("_cctally_tui")


def _lines(ns, tui, width, credits, variant="conventional"):
    cw = ns["TuiCurrentWeek"](
        week_start_at=NOW - dt.timedelta(days=3),
        week_end_at=NOW + dt.timedelta(days=4),
        used_pct=21.0, five_hour_pct=20.0,
        five_hour_resets_at=NOW + dt.timedelta(hours=3),
        spent_usd=10.0, dollars_per_percent=0.5,
        latest_snapshot_at=NOW - dt.timedelta(minutes=1),
        five_hour_block=None if credits is None else {"credits": credits},
    )
    snap = dataclasses.replace(tui._tui_empty_snapshot(NOW), current_week=cw)
    runtime = ns["RuntimeState"](
        variant="conventional", focus_index=0, session_scroll=0,
        show_help=False, toast=None, color_enabled=False, tz="utc",
    )
    if variant == "hero":
        return tui._tui_panel_current_week_hero(snap, runtime, width)
    return tui._tui_panel_current_week(snap, runtime, width, focused=True)


def _row(tui, lines, label):
    [row] = [line for line in lines if label in line]
    return tui._tui_lines_to_text([row])


@pytest.mark.parametrize("credits", [
    [{"delta_pp": -50.0}],
    [{"delta_pp": -50.0}, {"delta_pp": -7.0}],
], ids=["one-credit", "two-credits"])
@pytest.mark.parametrize("width", [58, 60, 80])
def test_the_credit_badge_fits_the_panel_interior(mods, width, credits):
    ns, tui = mods
    row = _row(tui, _lines(ns, tui, width, credits), " 5-hour ")
    # Inside the interior (width - 2) with one blank cell left before the
    # right border, as every other row of the panel keeps.
    assert row.cell_len <= width - 3, (width, row.plain, row.cell_len)
    for credit in credits:
        assert f"{credit['delta_pp']:+.0f}pp" in row.plain, row.plain
    assert " 20%" in row.plain, row.plain


@pytest.mark.parametrize("width", [58, 60, 80])
def test_without_a_credit_both_bars_keep_one_width(mods, width):
    ns, tui = mods
    lines = _lines(ns, tui, width, None)
    used = _row(tui, lines, " Used ").plain
    five = _row(tui, lines, " 5-hour ").plain
    bar = lambda text: sum(text.count(glyph) for glyph in "█░")
    assert bar(five) == bar(used) == width - 20, (used, five)


_CREDITS = {
    "one-credit": [{"delta_pp": -50.0}],
    "two-credits": [{"delta_pp": -50.0}, {"delta_pp": -7.0}],
}


@pytest.mark.parametrize("variant", ["conventional", "hero"])
@pytest.mark.parametrize("credits", sorted(_CREDITS))
@pytest.mark.parametrize("width", [58, 60, 80])
def test_every_boxed_row_is_exactly_the_column_width(mods, width, credits,
                                                     variant):
    ns, tui = mods
    deltas = _CREDITS[credits]
    boxed = tui._tui_tagged_box_lines(
        width=width, body_tagged=_lines(ns, tui, width, deltas, variant),
        title="{accent.b}Current Week{/}", pin="{dim}[1]{/}")
    rows = [tui._tui_lines_to_text([row]) for row in boxed]
    wrong = [(row.cell_len, row.plain) for row in rows if row.cell_len != width]
    assert not wrong, (width, wrong)
    badge = ", ".join(f"{d['delta_pp']:+.0f}pp" for d in deltas)
    assert any(badge in row.plain for row in rows), [r.plain for r in rows]
