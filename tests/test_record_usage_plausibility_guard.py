"""Plausibility-guard regression for cmd_record_usage (issue #112).

A single statusline tick on 2026-04-28T10:43:21Z fed cctally
`--resets-at 1745869800` (= 2025-04-28T19:53:20Z, exactly 366 days off).
cctally accepted the value, derived `week_start_date=2025-04-29`, and
wrote a phantom-year row that displaced real `2026-04-25 → next-reset`
data in the trend table. These tests lock the defensive guard that
rejects out-of-band epochs before they hit any side-effect write.

Acceptance cases A1-A8 from the spec:
    docs/superpowers/specs/2026-05-28-issue-112-record-usage-plausibility-guard.md
"""
from __future__ import annotations

import argparse
import datetime as dt

import pytest

from conftest import load_script, redirect_paths


# Pinned wall-clock for every test in this file. Chosen so the bug
# payload (epoch 1745869800 = 2025-04-28T19:53:20Z) is ~395 days in the
# past (well outside the 30d past slack).
_AS_OF_ISO = "2026-05-28T12:00:00Z"
_AS_OF_DT = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
_AS_OF_EPOCH = int(_AS_OF_DT.timestamp())

# Exactly the bug payload from the diagnosis session.
_PHANTOM_YEAR_EPOCH = 1745869800  # 2025-04-28T19:53:20Z

# Common-case payloads (well inside band).
_GOOD_WEEK_RESETS_EPOCH = _AS_OF_EPOCH + 5 * 86400          # +5d
_GOOD_5H_RESETS_EPOCH = _AS_OF_EPOCH + 3 * 3600             # +3h


@pytest.fixture
def ns(monkeypatch, tmp_path):
    """Fresh script namespace with paths pinned + wall-clock pinned."""
    n = load_script()
    redirect_paths(n, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF", _AS_OF_ISO)
    return n


def _run(
    ns,
    *,
    percent: float = 30.0,
    resets_at: int,
    five_hour_percent: float | None = None,
    five_hour_resets_at: int | None = None,
) -> int:
    args = argparse.Namespace(
        percent=percent,
        resets_at=str(resets_at),
        five_hour_percent=five_hour_percent,
        five_hour_resets_at=(
            str(five_hour_resets_at)
            if five_hour_resets_at is not None
            else None
        ),
    )
    return ns["cmd_record_usage"](args)


def _snapshot_row_count(ns) -> int:
    with ns["open_db"]() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0]


# --- A1: year-off past rejected ----------------------------------------------

def test_a1_year_past_resets_at_rejected(ns, capsys):
    rc = _run(ns, resets_at=_PHANTOM_YEAR_EPOCH)
    assert rc == 2, f"expected exit 2, got {rc}"
    assert _snapshot_row_count(ns) == 0
    err = capsys.readouterr().err
    assert "[record-usage] rejecting --resets-at=" in err
    assert str(_PHANTOM_YEAR_EPOCH) in err
    assert "outside plausibility band" in err
    assert "No row written" in err


# --- A2: month-off future rejected -------------------------------------------

def test_a2_month_future_resets_at_rejected(ns, capsys):
    rc = _run(ns, resets_at=_AS_OF_EPOCH + 30 * 86400)
    assert rc == 2
    assert _snapshot_row_count(ns) == 0
    err = capsys.readouterr().err
    assert "[record-usage] rejecting --resets-at=" in err


# --- A3: common-case payload accepted ----------------------------------------

def test_a3_common_case_accepted(ns, capsys):
    rc = _run(
        ns,
        resets_at=_GOOD_WEEK_RESETS_EPOCH,
        five_hour_percent=10.0,
        five_hour_resets_at=_GOOD_5H_RESETS_EPOCH,
    )
    assert rc == 0
    assert _snapshot_row_count(ns) == 1
    err = capsys.readouterr().err
    assert "rejecting" not in err


# --- A4: valid weekly + year-off 5h → drop 5h fields, write weekly ----------

def test_a4_year_off_5h_drops_5h_keeps_weekly(ns, capsys):
    """Out-of-band 5h is non-fatal: drop the 5h portion and still write
    the weekly snapshot row. Tightening the 5h past band addresses the
    cross-block-pollution gap (entries past the real reset getting
    charged to the previous block via _compute_block_totals); switching
    from `return 2` to drop-and-continue preserves the documented
    manual-replay path (record-usage.md), whose snippet includes the
    5h flags whenever they were present at capture."""
    rc = _run(
        ns,
        resets_at=_GOOD_WEEK_RESETS_EPOCH,
        five_hour_percent=10.0,
        five_hour_resets_at=_PHANTOM_YEAR_EPOCH,
    )
    assert rc == 0
    assert _snapshot_row_count(ns) == 1
    err = capsys.readouterr().err
    assert "[record-usage] dropping --five-hour-resets-at=" in err
    assert "Weekly snapshot still written" in err
    # Must NOT name --resets-at (that one was valid).
    assert "dropping --resets-at=" not in err
    assert "rejecting --resets-at=" not in err
    # 5h fields on the written row must be NULL.
    with ns["open_db"]() as conn:
        row = conn.execute(
            "SELECT five_hour_percent, five_hour_resets_at, "
            "       five_hour_window_key "
            "  FROM weekly_usage_snapshots"
        ).fetchone()
        assert row["five_hour_percent"] is None
        assert row["five_hour_resets_at"] is None
        assert row["five_hour_window_key"] is None


# --- A5: 14-day-old replay inside 30d slack accepted ------------------------

def test_a5_manual_replay_within_past_slack_accepted(ns, capsys):
    """The documented 'manually replay a missed snapshot' use case
    (docs/commands/record-usage.md:54-57) must keep working."""
    fourteen_days_old = _AS_OF_EPOCH - 14 * 86400
    rc = _run(ns, resets_at=fourteen_days_old)
    assert rc == 0
    assert _snapshot_row_count(ns) == 1
    err = capsys.readouterr().err
    assert "rejecting" not in err


# --- A6: ms-epoch input rejects without crash --------------------------------

def test_a6_ms_epoch_rejects_without_crash(ns, capsys):
    """Broken-unit input (13-digit ms-epoch) must reject gracefully
    instead of raising OverflowError from datetime.fromtimestamp."""
    ms_epoch = _PHANTOM_YEAR_EPOCH * 1000  # 13-digit ms-epoch
    # Should not raise.
    rc = _run(ns, resets_at=ms_epoch)
    assert rc == 2
    assert _snapshot_row_count(ns) == 0
    err = capsys.readouterr().err
    assert "[record-usage] rejecting --resets-at=" in err
    assert str(ms_epoch) in err


def test_a6_ms_epoch_on_5h_drops_5h_keeps_weekly(ns, capsys):
    """Same as A6 but for --five-hour-resets-at — the original spec
    placement would have crashed on the fromtimestamp() at line 1284
    before the guard ran. ms-epoch on 5h is non-fatal under the
    drop-and-continue contract: weekly row still writes, 5h fields are
    NULL."""
    ms_epoch = _PHANTOM_YEAR_EPOCH * 1000
    rc = _run(
        ns,
        resets_at=_GOOD_WEEK_RESETS_EPOCH,
        five_hour_percent=10.0,
        five_hour_resets_at=ms_epoch,
    )
    assert rc == 0
    assert _snapshot_row_count(ns) == 1
    err = capsys.readouterr().err
    assert "[record-usage] dropping --five-hour-resets-at=" in err
    with ns["open_db"]() as conn:
        row = conn.execute(
            "SELECT five_hour_resets_at FROM weekly_usage_snapshots"
        ).fetchone()
        assert row["five_hour_resets_at"] is None


# --- A7: refresh-layer boundary maps reject to record_failed ----------------

def test_a7_refresh_layer_maps_reject_to_record_failed(ns, monkeypatch):
    """_refresh_usage_inproc invoked with an OAuth payload that
    produces an out-of-band epoch must surface as
    status='record_failed' (reason='exit 2'), not status='ok'.
    Protects the dashboard / CLI refresh contract from silently
    reporting success on a dropped payload."""
    # Build a fake OAuth payload whose resets_at is a year in the past.
    phantom_iso = dt.datetime.fromtimestamp(
        _PHANTOM_YEAR_EPOCH, tz=dt.timezone.utc
    ).isoformat()

    def _fake_fetch(token: str, timeout_seconds: float) -> dict:
        return {
            "seven_day": {
                "utilization": 42.0,
                "resets_at": phantom_iso,
            },
            "five_hour": None,
        }

    monkeypatch.setitem(ns, "_fetch_oauth_usage", _fake_fetch)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "fake-token")
    monkeypatch.setitem(ns, "_bust_statusline_cache", lambda: "ok")

    result = ns["_refresh_usage_inproc"](timeout_seconds=5.0)
    assert result.status == "record_failed", (
        f"expected record_failed, got {result.status} (reason={result.reason})"
    )
    assert result.reason == "exit 2", (
        f"expected reason='exit 2', got {result.reason!r}"
    )
    assert _snapshot_row_count(ns) == 0


# --- A8: band-edge accepts (boundary tests) ----------------------------------

def test_band_lower_edge_week_resets_accepted(ns):
    """Exactly at now - 30d the value is INSIDE the band (closed lower
    bound). Confirms the >= comparison."""
    rc = _run(ns, resets_at=_AS_OF_EPOCH - 30 * 86400)
    assert rc == 0


def test_band_upper_edge_week_resets_accepted(ns):
    """Exactly at now + 8d the value is INSIDE the band (closed upper
    bound). Confirms the <= comparison."""
    rc = _run(ns, resets_at=_AS_OF_EPOCH + 8 * 86400)
    assert rc == 0


def test_band_just_past_lower_edge_week_resets_rejected(ns, capsys):
    """At now - 30d - 1s the value is OUTSIDE the band."""
    rc = _run(ns, resets_at=_AS_OF_EPOCH - 30 * 86400 - 1)
    assert rc == 2


def test_band_just_past_upper_edge_week_resets_rejected(ns, capsys):
    """At now + 8d + 1s the value is OUTSIDE the band."""
    rc = _run(ns, resets_at=_AS_OF_EPOCH + 8 * 86400 + 1)
    assert rc == 2


def test_band_lower_edge_5h_resets_accepted(ns):
    """At now - 10m the 5h epoch is INSIDE the band (closed lower bound).
    10 minutes matches _FIVE_HOUR_JITTER_FLOOR_SECONDS — enough to absorb
    boundary jitter / clock skew without leaving the next-block-pollution
    gap that a wider past-slack would open in maybe_update_five_hour_block."""
    rc = _run(
        ns,
        resets_at=_GOOD_WEEK_RESETS_EPOCH,
        five_hour_percent=10.0,
        five_hour_resets_at=_AS_OF_EPOCH - 600,
    )
    assert rc == 0


def test_band_upper_edge_5h_resets_accepted(ns):
    rc = _run(
        ns,
        resets_at=_GOOD_WEEK_RESETS_EPOCH,
        five_hour_percent=10.0,
        five_hour_resets_at=_AS_OF_EPOCH + 6 * 3600,
    )
    assert rc == 0


def test_band_just_past_lower_edge_5h_resets_drops_5h(ns, capsys):
    """At now - 10m - 1s the 5h epoch is OUTSIDE the band. Drop the 5h
    fields and continue (drop-and-continue contract); weekly snapshot
    still writes."""
    rc = _run(
        ns,
        resets_at=_GOOD_WEEK_RESETS_EPOCH,
        five_hour_percent=10.0,
        five_hour_resets_at=_AS_OF_EPOCH - 600 - 1,
    )
    assert rc == 0
    assert _snapshot_row_count(ns) == 1
    err = capsys.readouterr().err
    assert "[record-usage] dropping --five-hour-resets-at=" in err
    with ns["open_db"]() as conn:
        row = conn.execute(
            "SELECT five_hour_resets_at FROM weekly_usage_snapshots"
        ).fetchone()
        assert row["five_hour_resets_at"] is None


def test_manual_replay_with_stale_5h_writes_weekly(ns, capsys):
    """Documented manual-replay path (record-usage.md). The status-line
    snippet emits `--five-hour-percent` + `--five-hour-resets-at`
    alongside the weekly flags whenever the upstream payload had a 5h
    block; a 14-day-old replay's 5h epoch is unrecoverably stale, but
    we must still write the weekly snapshot row (the whole point of
    the wider 30d weekly past-slack)."""
    fourteen_days_old = _AS_OF_EPOCH - 14 * 86400
    rc = _run(
        ns,
        resets_at=fourteen_days_old,
        five_hour_percent=10.0,
        five_hour_resets_at=fourteen_days_old + 3 * 3600,
    )
    assert rc == 0
    assert _snapshot_row_count(ns) == 1
    err = capsys.readouterr().err
    assert "[record-usage] dropping --five-hour-resets-at=" in err
    assert "rejecting --resets-at=" not in err
    with ns["open_db"]() as conn:
        row = conn.execute(
            "SELECT week_start_date, five_hour_percent, five_hour_resets_at "
            "  FROM weekly_usage_snapshots"
        ).fetchone()
        # Weekly fields land normally; 5h fields are NULL.
        assert row["week_start_date"] is not None
        assert row["five_hour_percent"] is None
        assert row["five_hour_resets_at"] is None


# --- Negative epoch (very old past, well outside band) -----------------------

def test_negative_epoch_rejected(ns, capsys):
    """A negative Unix epoch (some pre-1970 garbage value) must reject,
    not crash. fromtimestamp on a negative value is platform-dependent
    on Windows; band check protects us regardless."""
    rc = _run(ns, resets_at=-1)
    assert rc == 2
    assert _snapshot_row_count(ns) == 0
