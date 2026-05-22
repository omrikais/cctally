"""Tests for the ``compact=`` kwarg on Session A's new renderer surface.

Session A spec §7.6.1 / Review-A P2-B: ``--compact`` must force the
proportional-layout code path on every Claude-side renderer that
exposes ``--compact`` at the CLI, regardless of the actual terminal
width. Already wired on ``_render_bucket_table`` (daily/monthly) by
Implementor 1; this file covers the two renderers that were missed in
the first pass:

  - ``_render_blocks_table``    (cmd_blocks)
  - ``_render_claude_session_table`` (cmd_session)

Strategy: render the same dataset twice — once with ``compact=False``
and once with ``compact=True`` — under a fixed wide ``COLUMNS`` so the
auto-detected width-overflow branch does NOT fire on the non-compact
render. Assert the outputs differ — the compact branch reshapes column
widths via the same proportional scale as the codex renderer's
``force_compact``.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

from conftest import load_script


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
