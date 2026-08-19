"""#620 S1 — every `cache-report` terminal row carries a cell per column.

`_layout_cache_table`'s `make_row` iterates `enumerate(cells)` and pads
nothing: a row that supplies fewer cells than there are headers renders
short and its right border never closes. Nothing in the renderer raises,
so the defect reaches a golden as a shorter line and survives a re-take
that nobody reads line by line — which is exactly how the `Eval` column
shipped with ragged breakdown and footer rows.

These tests assert the table is rectangular rather than asserting the
presence of any one column, so the next column that forgets a row type
fails here too.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import pathlib
import sys

import pytest

from conftest import load_script

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _lib_cache_report as kernel  # noqa: E402


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(scope="module")
def ns():
    return load_script()


@pytest.fixture
def default_terminal(monkeypatch):
    """Pin the shipped 120-column default so the layout is deterministic."""
    monkeypatch.setenv("COLUMNS", "120")
    real = os.get_terminal_size

    def _raise(*a, **k):
        raise OSError("forced for test")

    monkeypatch.setattr(os, "get_terminal_size", _raise)
    yield
    monkeypatch.setattr(os, "get_terminal_size", real)


def _breakdown(name: str, *, cost: float) -> kernel.CacheModelBreakdown:
    return kernel.CacheModelBreakdown(
        model_name=name,
        input_tokens=1_200,
        output_tokens=800,
        cache_creation_tokens=4_000,
        cache_read_tokens=90_000,
        cache_hit_percent=94.4,
        cost=cost,
        saved_usd=cost * 3,
        wasted_usd=cost / 4,
        net_usd=cost * 2.75,
    )


def _day_rows() -> list[kernel.CacheRow]:
    """Two day rows, each with two model children, one of them anomalous.

    Breakdown children and an anomaly-marked row are both required: they
    are the row types that took a different construction path from the
    plain data row.
    """
    rows = []
    for i, date in enumerate(("2026-04-13", "2026-04-14")):
        row = kernel.CacheRow(
            date=date,
            input_tokens=2_400,
            output_tokens=1_600,
            cache_creation_tokens=8_000,
            cache_read_tokens=180_000,
            cost=1.25 + i,
            saved_usd=3.75,
            wasted_usd=0.31,
            net_usd=3.44,
            model_breakdowns=[
                _breakdown("claude-sonnet-4-6", cost=0.75),
                _breakdown("claude-opus-4-1", cost=0.50),
            ],
        )
        if i == 1:
            row.anomaly_triggered = True
            row.anomaly_reasons = ["net_negative", "cache_drop"]
        rows.append(row)
    return rows


def _session_rows() -> list[kernel.CacheRow]:
    rows = []
    for i in range(2):
        row = kernel.CacheRow(
            session_id=f"{i:08d}-abcd-efgh-ijkl-mnopqrstuvwx",
            project_path=f"/Users/someone/repos/project-{i}",
            last_activity=dt.datetime(2026, 4, 13, 14, 0, tzinfo=dt.timezone.utc),
            input_tokens=2_400,
            output_tokens=1_600,
            cache_creation_tokens=8_000,
            cache_read_tokens=180_000,
            cost=1.25 + i,
            saved_usd=3.75,
            wasted_usd=0.31,
            net_usd=3.44,
            model_breakdowns=[
                _breakdown("claude-sonnet-4-6", cost=0.75),
                _breakdown("claude-opus-4-1", cost=0.50),
            ],
        )
        if i == 1:
            row.anomaly_triggered = True
            row.anomaly_reasons = ["cache_drop"]
        rows.append(row)
    return rows


def _grid_lines(rendered: str) -> list[str]:
    """The box-drawing lines of the table, banner and notes excluded."""
    prefixes = ("┌", "├", "└", "│", "+", "|")
    out = []
    for raw in rendered.splitlines():
        line = _ANSI_RE.sub("", raw)
        if line.startswith(prefixes):
            out.append(line)
    return out


def _assert_rectangular(ns, rendered: str, label: str) -> None:
    display_width = ns["_display_width"]
    lines = _grid_lines(rendered)
    assert len(lines) >= 6, f"{label}: expected a full table, got {lines!r}"

    widths = {}
    for line in lines:
        widths.setdefault(display_width(line), []).append(line)
    assert len(widths) == 1, (
        f"{label}: table is ragged — line widths {sorted(widths)}. "
        + "; ".join(
            f"width {w}: {vals[0]!r}" for w, vals in sorted(widths.items())
        )
    )

    # Column-separator count is the row-type-agnostic form of the same
    # invariant: N columns means N+1 verticals on every content row, so a
    # row type that forgets a cell fails here even if some future padding
    # happened to keep the widths equal.
    seps = {line.count("│") or line.count("|") for line in lines
            if line.startswith(("│", "|"))}
    assert len(seps) == 1, (
        f"{label}: content rows disagree on column count "
        f"(separator counts {sorted(seps)}) — a row type is missing a cell"
    )


def test_day_table_is_rectangular(ns, default_terminal):
    """Data rows, `└─ model` breakdown rows and the `Total` footer must all
    render the same width — the daily table."""
    rendered = ns["_render_cache_report_table"](
        _day_rows(), "Cache Report", mode="day",
    )
    assert "└─" in _ANSI_RE.sub("", rendered) or "|_" in _ANSI_RE.sub("", rendered), (
        "the fixture must produce breakdown rows or this test asserts nothing"
    )
    assert "Total" in _ANSI_RE.sub("", rendered), "footer row required"
    _assert_rectangular(ns, rendered, "day mode")


def test_session_table_is_rectangular(ns, default_terminal):
    """The by-session table has its own row builders and its own column
    count; it must be rectangular for the same reason."""
    rendered = ns["_render_cache_report_table"](
        _session_rows(), "Cache Report (by session)", mode="session",
        tz=dt.timezone.utc,
    )
    stripped = _ANSI_RE.sub("", rendered)
    assert "└─" in stripped or "|_" in stripped, (
        "the fixture must produce breakdown rows or this test asserts nothing"
    )
    assert "Total" in stripped, "footer row required"
    _assert_rectangular(ns, rendered, "session mode")


@pytest.mark.parametrize("mode", ["day", "session"])
def test_compact_layout_is_rectangular_too(ns, default_terminal, mode):
    """`--compact` takes the proportional scale-down branch, which resizes
    every column; a missing cell is just as invisible there."""
    rows = _day_rows() if mode == "day" else _session_rows()
    rendered = ns["_render_cache_report_table"](
        rows, "Cache Report", mode=mode, tz=dt.timezone.utc, compact=True,
    )
    _assert_rectangular(ns, rendered, f"{mode} mode (compact)")


# --- Remediation: the class is unrepresentable, not merely tested ---------


def test_the_layout_refuses_a_short_row(ns, default_terminal):
    """`make_row` is the chokepoint every row type passes through.

    The tests above assert that today's row builders are rectangular; they
    cannot stop tomorrow's from being ragged, because a short row rendered
    without complaint — `enumerate(cells)` simply stopped early and the row
    closed its border a column too soon. The invariant belongs at the
    chokepoint, so it holds for a row builder nobody has written yet.
    """
    sys.path.insert(
        0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
    import _cctally_cache_report as cr

    headers = ["A", "B", "C"]
    aligns = ["left", "right", "right"]
    full = ([("a", None), ("1", None), ("2", None)], "data")
    short = ([("a", None), ("1", None)], "data")

    def _render(rows):
        return cr._layout_cache_table(
            headers, aligns, rows, "T", False, True,
            expand_col_index=0, numeric_col_indices=(1, 2),
            date_col_index=None,
        )

    # The full-width table renders, so the behaviour below is about the short
    # row and not about the arguments.
    full_body = _render([full])
    assert full_body

    # A short row is PADDED, not asserted away. The guard used to be a bare
    # `assert`, which `python -O` elides — so the unclosed border came back on
    # exactly the interpreter flag that removed the guard — and which raised an
    # uncaught `AssertionError` outside the documented exit taxonomy when it
    # did fire. Padding holds under every flag, so the invariant the docstring
    # claims is structural really is.
    short_body = _render([short])
    assert short_body
    widths = {len(ln) for ln in short_body.splitlines() if ln.strip()}
    full_widths = {len(ln) for ln in full_body.splitlines() if ln.strip()}
    assert widths == full_widths, (
        "a short row must render at the table's full width:\n"
        f"{short_body}"
    )

    # An OVER-long row is a different fault: padding cannot repair it and
    # rendering it would drop the surplus cells. It raises the module's own
    # `ValueError`, which `main()` already reports.
    over = ([("a", None), ("1", None), ("2", None), ("3", None)], "data")
    with pytest.raises(ValueError) as excinfo:
        _render([over])
    assert "cells" in str(excinfo.value), str(excinfo.value)
