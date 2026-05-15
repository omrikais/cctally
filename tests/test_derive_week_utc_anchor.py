"""Regression: `_derive_week_from_payload` must anchor the bucket-key
date on the canonical UTC ISO, not on host-local TZ.

Root cause (May 2026 production data): `parse_iso_datetime` ends with
`.astimezone()` (no args), which converts to host-local TZ. The original
``_derive_week_from_payload`` then took `.date()` of that local-TZ
datetime to populate ``DerivedWeekWindow.week_start`` — the bucket key
written to ``weekly_usage_snapshots.week_start_date``. When the cctally
process inherited a TZ whose offset placed the UTC moment on a
different calendar date, the same physical subscription week silently
forked across two ``week_start_date`` values. Result: two trend rows
in ``cctally report`` for the same window — one updated, one frozen.

Symmetric bug in ``pick_week_selection`` (cost-snapshot bucket key).
Both surfaces are exercised below.

The fix re-canonicalizes to UTC before ``.date()`` so the bucket key
matches what ``cmd_record_usage`` writes (it derives ``week_start_date``
directly from ``resets_at`` in ``dt.timezone.utc``).
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from conftest import load_script


@pytest.fixture
def ns():
    return load_script()


def _force_pacific_tz(monkeypatch):
    """Pin host TZ to America/Los_Angeles for the duration of the test.

    The `time.tzset` call is required so `datetime.astimezone()` (which
    reads `/etc/localtime` / `time.timezone`) picks up the new value
    without restarting the interpreter. macOS + Linux only — Windows
    has no `tzset`, but pytest hosts in CI/dev are POSIX.
    """
    import time

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    if hasattr(time, "tzset"):
        time.tzset()


def test_derive_week_anchors_on_utc_date_regardless_of_host_tz(ns, monkeypatch):
    """A UTC moment that lands on May 9 (UTC) must yield week_start=May 9
    even when the host TZ would put the same instant on May 8 local."""
    _force_pacific_tz(monkeypatch)

    # 2026-05-09T05:00:00Z is the canonical Anthropic weekly reset moment
    # for the user's subscription. On Pacific (-07:00) this is May 8 22:00
    # local — `.date()` of the local datetime is 2026-05-08. The pre-fix
    # code silently produced a `2026-05-08` bucket key for what was
    # physically still the May 9 subscription week, forking the
    # `weekly_usage_snapshots.week_start_date` column.
    payload = {
        "source": "statusline",
        "weeklyPercent": 61.0,
        "weekStartDate": "2026-05-09",   # canonical UTC-based
        "weekEndDate": "2026-05-16",
        "weekStartAt": "2026-05-09T05:00:00+00:00",
        "weekEndAt": "2026-05-16T05:00:00+00:00",
    }
    win = ns["_derive_week_from_payload"](payload, "sunday")

    assert win.week_start == dt.date(2026, 5, 9), (
        f"week_start forked to host-local TZ date: got {win.week_start}, "
        f"expected 2026-05-09. Pre-fix code returned 2026-05-08 on Pacific."
    )
    assert win.week_end == dt.date(2026, 5, 16)
    # Canonical ISO is preserved — only the bucket-key DATE is UTC-anchored.
    assert win.week_start_at == "2026-05-09T05:00:00+00:00"
    assert win.week_end_at == "2026-05-16T05:00:00+00:00"


def test_pick_week_selection_anchors_on_utc_date_regardless_of_host_tz(
    ns, monkeypatch, tmp_path
):
    """Cost-snapshot bucket key must match the usage bucket key under
    a TZ-shifted host process. Otherwise `cctally report` shows two
    trend rows for the same subscription week."""
    _force_pacific_tz(monkeypatch)

    # Seed an in-memory DB with one usage snapshot carrying the
    # canonical +00:00 boundary. `pick_week_selection` reads the latest
    # row and re-derives `week_start` / `week_end` from its boundary
    # columns. Pre-fix: `.date()` of the host-local datetime → May 8.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE weekly_usage_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at_utc TEXT NOT NULL,
            week_start_date TEXT NOT NULL,
            week_end_date TEXT NOT NULL,
            week_start_at TEXT,
            week_end_at TEXT,
            weekly_percent REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'statusline',
            payload_json TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-05-15T11:00:00Z",
            "2026-05-09",
            "2026-05-16",
            "2026-05-09T05:00:00+00:00",
            "2026-05-16T05:00:00+00:00",
            61.0,
            "statusline",
            "{}",
        ),
    )
    conn.commit()

    sel = ns["pick_week_selection"](conn, None, None, "sunday")
    conn.close()

    assert sel.week_start == dt.date(2026, 5, 9), (
        f"pick_week_selection forked to host-local TZ date: "
        f"got {sel.week_start}, expected 2026-05-09."
    )
    assert sel.week_end == dt.date(2026, 5, 16)
