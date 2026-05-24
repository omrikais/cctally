"""Tests for the ``compact=`` kwarg on Session A's new renderer surface.

Session A spec §7.6.1 / Review-A P2-B: ``--compact`` must force a
distinct table layout on every Claude-side renderer that exposes
``--compact`` at the CLI, regardless of the actual terminal width.
Wired on ``_render_bucket_table`` (daily/monthly), ``_render_blocks_table``
(blocks), and ``_render_claude_session_table`` (session) in Session A.
Issue #91 extends real compact rendering to the five remaining
in-scope commands, in two shapes (see the issue for the per-renderer
rationale):

  Shape A — force the existing proportional scale-down branch
  (mirrors the reference renderers above):
    - ``_render_project_table``   (cmd_project)
    - ``_layout_cache_table`` via ``_render_cache_report_table``
                                 (cmd_cache_report)

  Shape B — reduce per-cell padding 1→0 on content-sized boxed tables
  that have no responsive-width code path to force:
    - ``_boxed_table``            (cmd_range_cost --breakdown)
    - ``_render_five_hour_blocks_table`` → ``_boxed_table``
                                 (cmd_five_hour_blocks)
    - ``_diff_render_section_table`` (cmd_diff)

Strategy: render the same dataset twice — once with ``compact=False``
and once with ``compact=True`` — under a fixed wide ``COLUMNS`` so the
auto-detected width-overflow branch does NOT fire on the non-compact
render. Assert the outputs differ — proving ``--compact`` actually
changes layout, not just lands on ``args.compact`` as a no-op.
"""
from __future__ import annotations

import datetime as dt
import os
import re

import pytest

from conftest import load_script

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _box_line_widths(ns, rendered: str) -> list[int]:
    """Display widths of the box-drawing lines in a rendered table.

    Filters to lines that are part of the table grid (start with a box
    corner / border / vertical glyph) so the leading banner and trailing
    notes don't pollute the width-uniformity check. Widths use the
    renderer's own `_display_width` so wide glyphs count as 2 cells.
    """
    display_width = ns["_display_width"]
    grid_prefixes = ("┌", "├", "└", "│")  # ┌ ├ └ │
    widths = []
    for raw in rendered.splitlines():
        line = _strip_ansi(raw)
        if line.startswith(grid_prefixes):
            widths.append(display_width(line))
    return widths


@pytest.fixture(scope="module")
def ns():
    return load_script()


@pytest.fixture
def wide_terminal(monkeypatch):
    """Force a wide COLUMNS so the non-compact branch is unambiguously
    chosen on baseline renders (no auto-overflow fallback)."""
    monkeypatch.setenv("COLUMNS", "300")
    # Some renderers call os.get_terminal_size() first; make it raise so
    # the COLUMNS fallback fires.
    real = os.get_terminal_size

    def _raise(*a, **k):
        raise OSError("forced for test")

    monkeypatch.setattr(os, "get_terminal_size", _raise)
    yield
    monkeypatch.setattr(os, "get_terminal_size", real)


@pytest.fixture
def narrow_terminal(monkeypatch):
    """Force an 80-column terminal so the compact scale-down branch
    actually shrinks columns (the regression surface for headers wider
    than their scaled column width)."""
    monkeypatch.setenv("COLUMNS", "80")
    real = os.get_terminal_size

    def _raise(*a, **k):
        raise OSError("forced for test")

    monkeypatch.setattr(os, "get_terminal_size", _raise)
    yield
    monkeypatch.setattr(os, "get_terminal_size", real)


# ── blocks renderer ─────────────────────────────────────────────────────


def _activity_block(ns, *, start: dt.datetime, end: dt.datetime, tokens: int):
    Block = ns["Block"]
    return Block(
        start_time=start,
        end_time=end,
        actual_end_time=start + dt.timedelta(minutes=30),
        is_active=False,
        is_gap=False,
        entries_count=1,
        input_tokens=tokens,
        output_tokens=tokens,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        total_tokens=2 * tokens,
        cost_usd=0.12345,
        models=["claude-sonnet-4-6"],
        burn_rate=None,
        projection=None,
    )


def test_compact_changes_blocks_rendering(ns, wide_terminal):
    """`_render_blocks_table(compact=True)` must produce a measurably
    different rendering than the default at a 300-column terminal —
    proving the kwarg actually flows through to the proportional-layout
    code path (mirrors `_render_codex_session_table`'s `force_compact`).

    Note: under a wide terminal with a small dataset, the scale factor
    (available/total_col) is > 1, so compact mode here expands columns
    rather than shrinking them — that's expected, and identical to the
    existing codex `force_compact` semantics. What matters for the
    --compact flag is that the proportional branch fires (different
    output) regardless of auto-overflow; that's what we assert.
    """
    render = ns["_render_blocks_table"]
    start = dt.datetime(2026, 4, 23, 8, 0, tzinfo=dt.timezone.utc)
    blocks = [
        _activity_block(ns, start=start, end=start + dt.timedelta(hours=1), tokens=1000),
        _activity_block(
            ns, start=start + dt.timedelta(hours=6), end=start + dt.timedelta(hours=7),
            tokens=2000,
        ),
    ]
    now = start + dt.timedelta(hours=12)

    wide = render(blocks, now=now, compact=False)
    compact = render(blocks, now=now, compact=True)

    assert wide != compact, "compact kwarg had no effect on blocks render"


# ── claude session renderer ─────────────────────────────────────────────


def _claude_session(ns, *, sid: str, project: str, tokens: int):
    ClaudeSessionUsage = ns["ClaudeSessionUsage"]
    last = dt.datetime(2026, 4, 23, 14, 30, tzinfo=dt.timezone.utc)
    return ClaudeSessionUsage(
        session_id=sid,
        project_path=project,
        source_paths=[],
        first_activity=last - dt.timedelta(hours=1),
        last_activity=last,
        input_tokens=tokens,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        output_tokens=tokens,
        total_tokens=2 * tokens,
        cost_usd=0.42,
        models=["claude-sonnet-4-6"],
        model_breakdowns=[],
    )


def test_compact_changes_session_rendering(ns, wide_terminal):
    """`_render_claude_session_table(compact=True)` forces the
    proportional-layout branch at any terminal width — the rendered
    output must differ from the default (mirrors `_render_codex_session
    _table`'s `force_compact`). Width comparisons under a wide terminal
    are unreliable because scale = available/total can be >1; the
    invariant under test is "compact kwarg observably flows through to
    the renderer's layout decision".
    """
    render = ns["_render_claude_session_table"]
    sessions = [
        _claude_session(
            ns,
            sid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            project="/Users/alice/very/long/project/path/that/will/help/widen/this",
            tokens=12345,
        ),
        _claude_session(
            ns,
            sid="11111111-2222-3333-4444-555555555555",
            project="/Users/alice/another/quite/long/project/path/here",
            tokens=6789,
        ),
    ]

    wide = render(sessions, compact=False)
    compact = render(sessions, compact=True)

    assert wide != compact, "compact kwarg had no effect on session render"


def test_compact_session_border_alignment_on_narrow_terminal(
    ns, narrow_terminal
):
    """Codex review (round 2): `--compact` must not shrink a column below
    its (unsplittable) header width. On an 80-col terminal the scale-down
    branch previously floored columns at 8 chars, but headers like
    "Cache Create" (12), "Cost (USD)" (10) and "Last Activity" (13) are
    padded — never truncated — in the header render, so the header row
    overflowed the box border and the grid misaligned. Every box-drawing
    line (top/header-sep/data-sep/bottom borders + the `│`-delimited
    header & data rows) must share one display width.
    """
    render = ns["_render_claude_session_table"]
    sessions = [
        _claude_session(
            ns,
            sid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            project="/Users/alice/very/long/project/path/that/will/widen/this",
            tokens=12345,
        ),
    ]
    rendered = render(sessions, compact=True)
    widths = _box_line_widths(ns, rendered)
    assert widths, "no box-drawing lines found in compact session render"
    assert len(set(widths)) == 1, (
        f"compact session table misaligned: distinct box-line widths "
        f"{sorted(set(widths))}\n{rendered}"
    )


# ── issue #91: project renderer (Shape A — force scale-down) ─────────────


def _project_row(ns, *, display_key: str, bucket: str, tokens: int, cost: float):
    ProjectKey = ns["ProjectKey"]
    last = dt.datetime(2026, 4, 23, 14, 30, tzinfo=dt.timezone.utc)
    return {
        "key": ProjectKey(bucket_path=bucket, display_key=display_key, git_root=None),
        "sessions": {f"{display_key}-1", f"{display_key}-2"},
        "first_seen": last - dt.timedelta(hours=3),
        "last_seen": last,
        "input": tokens,
        "cache_write": tokens // 2,
        "cache_read": tokens * 3,
        "output": tokens,
        "cost_usd": cost,
        "attributed_pct": 12.5,
        "cost_per_pct": 0.34,
        "models": {},
    }


def test_compact_changes_project_rendering(ns, wide_terminal):
    """`_render_project_table(compact=True)` forces the proportional
    scale-down branch regardless of terminal width (#91 Shape A; mirrors
    `_render_blocks_table`). At 300 cols the small dataset would not
    auto-overflow, so the non-compact baseline uses raw content widths;
    forcing the branch reshapes them, so the outputs must differ."""
    render = ns["_render_project_table"]
    rows = [
        _project_row(ns, display_key="alpha", bucket="/Users/a/alpha",
                     tokens=12345, cost=4.21),
        _project_row(ns, display_key="beta", bucket="/Users/a/beta",
                     tokens=6789, cost=1.07),
    ]

    wide = render(rows, title="Project Usage", compact=False)
    compact = render(rows, title="Project Usage", compact=True)

    assert wide != compact, "compact kwarg had no effect on project render"


def test_compact_project_border_alignment_on_narrow_terminal(
    ns, narrow_terminal
):
    """Codex review (round 2): same header-floor regression as session,
    in the project renderer. Headers "First Seen" (10), "Cache Create"
    (12) and "Cost (USD)" (10) are padded (not truncated) in the header
    render, so flooring columns at 8 left the header wider than the
    border. Assert one uniform display width across all box-drawing
    lines under `--compact` on 80 columns."""
    render = ns["_render_project_table"]
    rows = [
        _project_row(ns, display_key="alpha", bucket="/Users/a/alpha",
                     tokens=12345, cost=4.21),
        _project_row(ns, display_key="beta", bucket="/Users/a/beta",
                     tokens=6789, cost=1.07),
    ]
    rendered = render(rows, title="Project Usage", compact=True)
    widths = _box_line_widths(ns, rendered)
    assert widths, "no box-drawing lines found in compact project render"
    assert len(set(widths)) == 1, (
        f"compact project table misaligned: distinct box-line widths "
        f"{sorted(set(widths))}\n{rendered}"
    )


# ── issue #91: cache-report renderer (Shape A — force scale-down) ────────


def _cache_row(ns, *, date: str, tokens: int, cost: float):
    CacheRow = ns["CacheRow"]
    return CacheRow(
        date=date,
        input_tokens=tokens,
        output_tokens=tokens,
        cache_creation_tokens=tokens // 2,
        cache_read_tokens=tokens * 3,
        cost=cost,
        saved_usd=cost * 2,
        wasted_usd=0.0,
        net_usd=cost,
        model_breakdowns=[],
    )


def test_compact_changes_cache_report_rendering(ns, wide_terminal):
    """`_render_cache_report_table(compact=True)` forces the
    `_layout_cache_table` `compact_mode` branch regardless of terminal
    width (#91 Shape A). Same proof shape as project: baseline at 300
    cols uses wide widths, compact reshapes them."""
    render = ns["_render_cache_report_table"]
    rows = [
        _cache_row(ns, date="2026-04-22", tokens=98765, cost=3.14),
        _cache_row(ns, date="2026-04-23", tokens=43210, cost=1.59),
    ]

    wide = render(rows, "Cache Report", mode="day", compact=False)
    compact = render(rows, "Cache Report", mode="day", compact=True)

    assert wide != compact, "compact kwarg had no effect on cache-report render"


# ── issue #91: range-cost via _boxed_table (Shape B — pad 1→0) ───────────


def test_compact_changes_range_cost_rendering(ns):
    """range-cost --breakdown renders its model table through
    `_boxed_table`; #91 Shape B drops the 1-space cell padding to 0 in
    compact mode. The narrower table differs regardless of row count or
    terminal width (the helper is content-sized, no width probe)."""
    boxed = ns["_boxed_table"]
    headers = ["Model", "Entries", "Cost (USD)"]
    rows = [
        ["claude-sonnet-4-6", "12", "$4.210000000"],
        ["claude-haiku-4-5", "3", "$0.310000000"],
        ["Total", "15", "$4.520000000"],
    ]
    aligns = ["left", "right", "right"]

    wide = boxed(headers, rows, aligns, compact=False)
    compact = boxed(headers, rows, aligns, compact=True)

    assert wide != compact, "compact kwarg had no effect on _boxed_table (range-cost)"


# ── issue #91: five-hour-blocks renderer (Shape B — pad 1→0) ─────────────


def _fhb_block(ns, *, start_iso: str, pct: float, cost: float, active: bool):
    return {
        "block_start_at": start_iso,
        "final_five_hour_percent": pct,
        "total_cost_usd": cost,
        "__is_active": active,
        "crossed_seven_day_reset": False,
        "seven_day_pct_at_block_start": 10.0,
        "seven_day_pct_at_block_end": 12.0,
    }


def test_compact_changes_five_hour_blocks_rendering(ns, wide_terminal, capsys):
    """`cmd_five_hour_blocks` prints its table via `_render_five_hour_blocks
    _table` → `_boxed_table`; #91 threads `args.compact` to the helper's
    Shape-B pad-reduction. The renderer prints, so we capture stdout for
    both modes and assert they differ."""
    import types

    render = ns["_render_five_hour_blocks_table"]
    blocks = [
        _fhb_block(ns, start_iso="2026-04-23T08:00:00+00:00",
                   pct=42.5, cost=1.23, active=False),
        _fhb_block(ns, start_iso="2026-04-23T13:00:00+00:00",
                   pct=61.0, cost=2.34, active=True),
    ]

    def _render(compact: bool) -> str:
        args = types.SimpleNamespace(
            _resolved_tz=None, breakdown=None, compact=compact,
        )
        render(blocks, args)
        return capsys.readouterr().out

    wide = _render(compact=False)
    compact = _render(compact=True)

    assert wide != compact, "compact kwarg had no effect on five-hour-blocks render"


# ── issue #91: diff section table (Shape B — pad 1→0) ────────────────────


def test_compact_changes_diff_rendering(ns):
    """`_diff_render_section_table(compact=True)` drops the 1-space cell
    padding to 0 (#91 Shape B) on the content-sized diff table, which
    has no proportional-width path. The narrower table differs even with
    a single row and regardless of the explicit `width` arg."""
    DiffSection = ns["DiffSection"]
    DiffRow = ns["DiffRow"]
    MB = ns["MetricBundle"]
    ColumnSpec = ns["ColumnSpec"]
    a = MB(12.43, 21034, 1840562, 14_000_000, 421000, 84.2, None)
    b = MB(18.91, 30412, 2640122, 19_200_000, 580000, 79.1, None)
    delta = ns["_build_delta_bundle"](a, b)
    rows = [DiffRow("model:s46", "claude-sonnet-4-6", "changed",
                    a, b, delta, sort_key=6.48)]
    section = DiffSection(
        name="models", scope="all", rows=rows, hidden_count=0,
        columns=[
            ColumnSpec("cost_usd", "Cost", "usd", False),
            ColumnSpec("cache_hit_pct", "Cache %", "pct", False),
            ColumnSpec("tokens_input", "Tokens", "tokens", False),
        ],
    )
    render = ns["_diff_render_section_table"]

    def _render(compact: bool) -> str:
        return render(
            section, total_a=a, total_b=b, width=144, color=False,
            used_pct_mode_a="exact", used_pct_mode_b="exact",
            compact=compact,
        )

    wide = _render(compact=False)
    compact = _render(compact=True)

    assert wide != compact, "compact kwarg had no effect on diff section render"
