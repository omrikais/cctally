"""Tests for gap-row formatting + sub-minute suppression (issue #79).

Two surfaces under test:

  1. ``_render_blocks_table`` (bin/_lib_render.py) — minute-resolution gap
     text. Pre-fix, ``round(total_seconds / 3600)`` clamped to 1 collapsed
     every sub-hour gap to ``"1h gap"``.

  2. ``_group_entries_into_blocks`` (bin/_lib_blocks.py) — sub-minute gaps
     are suppressed entirely (no row emitted). The threshold lives at the
     phase-3 gap-insertion site so the renderer never sees them.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script


@pytest.fixture(scope="module")
def ns():
    return load_script()


def _entry(ns, ts: dt.datetime, *, model: str = "claude-sonnet-4-6", tokens: int = 100):
    UsageEntry = ns["UsageEntry"]
    return UsageEntry(
        timestamp=ts,
        model=model,
        usage={
            "input_tokens": tokens,
            "output_tokens": tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        cost_usd=None,
        source_path="/tmp/synth.jsonl",
    )


def _gap_block(ns, *, start: dt.datetime, end: dt.datetime):
    """Build a free-standing gap Block matching the dataclass shape."""
    Block = ns["Block"]
    return Block(
        start_time=start,
        end_time=end,
        actual_end_time=None,
        is_active=False,
        is_gap=True,
        entries_count=0,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        total_tokens=0,
        cost_usd=0.0,
        models=[],
        burn_rate=None,
        projection=None,
    )


def _activity_block(ns, *, start: dt.datetime, end: dt.datetime, tokens: int = 1000):
    """Build a non-gap Block flanking a gap row in renderer tests."""
    Block = ns["Block"]
    return Block(
        start_time=start,
        end_time=end,
        actual_end_time=start + dt.timedelta(minutes=5),
        is_active=False,
        is_gap=False,
        entries_count=1,
        input_tokens=tokens,
        output_tokens=tokens,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        total_tokens=2 * tokens,
        cost_usd=0.05,
        models=["claude-sonnet-4-6"],
        burn_rate=None,
        projection=None,
    )


def _render_with_gap(ns, *, gap_minutes: float) -> str:
    """Render a three-block table whose middle block is a gap of given duration."""
    render = ns["_render_blocks_table"]
    a_start = dt.datetime(2026, 4, 23, 8, 0, tzinfo=dt.timezone.utc)
    a_end = a_start + dt.timedelta(hours=1)
    gap_start = a_end
    gap_end = gap_start + dt.timedelta(seconds=int(gap_minutes * 60))
    b_start = gap_end
    b_end = b_start + dt.timedelta(hours=1)
    blocks = [
        _activity_block(ns, start=a_start, end=a_end),
        _gap_block(ns, start=gap_start, end=gap_end),
        _activity_block(ns, start=b_start, end=b_end),
    ]
    return render(blocks, now=b_end + dt.timedelta(hours=1))


# ── formatter (renderer) ────────────────────────────────────────────────


def test_render_short_gap_renders_minutes(ns):
    out = _render_with_gap(ns, gap_minutes=5)
    assert "(5m gap)" in out
    assert "(1h gap)" not in out


def test_render_thirty_minute_gap(ns):
    out = _render_with_gap(ns, gap_minutes=30)
    assert "(30m gap)" in out


def test_render_one_hour_gap_omits_minutes(ns):
    out = _render_with_gap(ns, gap_minutes=60)
    assert "(1h gap)" in out
    assert "1h 00m gap" not in out


def test_render_hour_and_minute_gap(ns):
    out = _render_with_gap(ns, gap_minutes=90)
    assert "(1h 30m gap)" in out


def test_render_multi_hour_gap_no_minutes(ns):
    out = _render_with_gap(ns, gap_minutes=12 * 60)
    assert "(12h gap)" in out


# ── sub-minute suppression (grouper) ────────────────────────────────────


def test_subminute_gap_is_suppressed_no_row(ns):
    """A < 60s gap between two recorded windows must not emit a gap row.

    Engineered: two adjacent recorded anchors with canonical_intervals
    such that rs1 < bs2 (so the predicate fires) but the actual entries
    are 40 seconds apart (well below the 60s threshold).
    """
    group = ns["_group_entries_into_blocks"]
    base = dt.datetime(2026, 4, 23, 10, 0, tzinfo=dt.timezone.utc)
    R1 = dt.datetime(2026, 4, 23, 15, 0, tzinfo=dt.timezone.utc)
    R2 = dt.datetime(2026, 4, 23, 20, 0, tzinfo=dt.timezone.utc)
    bs1, rs1 = base, dt.datetime(2026, 4, 23, 14, 59, 30, tzinfo=dt.timezone.utc)
    bs2, rs2 = dt.datetime(2026, 4, 23, 15, 0, 10, tzinfo=dt.timezone.utc), R2

    entry_a = _entry(ns, dt.datetime(2026, 4, 23, 14, 59, 25, tzinfo=dt.timezone.utc))
    entry_b = _entry(ns, dt.datetime(2026, 4, 23, 15, 0, 5, tzinfo=dt.timezone.utc))

    blocks = group(
        [entry_a, entry_b],
        mode="auto",
        recorded_windows=[R1, R2],
        canonical_intervals={R1: (bs1, rs1), R2: (bs2, rs2)},
        now=R2 + dt.timedelta(hours=1),
    )
    gaps = [b for b in blocks if b.is_gap]
    assert gaps == [], f"expected no gap row, got {len(gaps)}: {gaps}"
    assert len([b for b in blocks if not b.is_gap]) == 2


def test_gap_at_threshold_still_emits_row(ns):
    """A 65s gap (just above the suppression threshold) MUST emit a gap row.

    Boundary check: the suppression predicate is ``< 60``, so 60s+ stays.
    """
    group = ns["_group_entries_into_blocks"]
    base = dt.datetime(2026, 4, 23, 10, 0, tzinfo=dt.timezone.utc)
    R1 = dt.datetime(2026, 4, 23, 15, 0, tzinfo=dt.timezone.utc)
    R2 = dt.datetime(2026, 4, 23, 20, 0, tzinfo=dt.timezone.utc)
    bs1, rs1 = base, dt.datetime(2026, 4, 23, 14, 59, 0, tzinfo=dt.timezone.utc)
    bs2, rs2 = dt.datetime(2026, 4, 23, 15, 0, 0, tzinfo=dt.timezone.utc), R2

    entry_a = _entry(ns, dt.datetime(2026, 4, 23, 14, 58, 55, tzinfo=dt.timezone.utc))
    entry_b = _entry(ns, dt.datetime(2026, 4, 23, 15, 0, 0, tzinfo=dt.timezone.utc))

    blocks = group(
        [entry_a, entry_b],
        mode="auto",
        recorded_windows=[R1, R2],
        canonical_intervals={R1: (bs1, rs1), R2: (bs2, rs2)},
        now=R2 + dt.timedelta(hours=1),
    )
    gaps = [b for b in blocks if b.is_gap]
    assert len(gaps) == 1, f"expected one gap row, got {len(gaps)}"
