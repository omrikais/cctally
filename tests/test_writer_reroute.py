"""Task 6 — writer reroute over the 6a ingest machinery.

This file grows with the reroute. The first wave (machinery wiring + gate P2
pickups) covers three seams the reroute stands on:

  * ``_usage_snapshot_fold_decision`` — the retained accept/skip clamp+dedup
    predicate the ``snapshot_accept`` deriver consults ONCE at capture time
    (spec §4.5 / §5.3). Direct unit tests over seeded rows (gate P2 pickup a).
  * ``alert_sink`` threading — the passed-conn derivation chokepoints append
    their new-crossing payloads to a caller-supplied sink instead of dropping
    them, so the ingester dispatches post-commit (spec §5.2 step 6 / item 2).
  * harvest early-out — the ingest cycle skips the 8 ``journal_id IS NULL``
    harvest scans when the pipeline wrote nothing this cycle (gate P2 pickup b).

Isolation mirrors tests/test_journal_ingest.py + tests/test_derivation_purity.py:
load_script() DROPS cached _cctally_* siblings and reloads fresh, so every test
grabs the fresh _cctally_journal AFTER load_script(); redirect_paths sets
sys.modules["cctally"] + the tmp JOURNAL_DIR / data dir.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import multiprocessing as mp
import os
import pathlib
import sqlite3

import pytest

from conftest import load_script, redirect_paths

# Every HOME-derived path constant resolves under a per-test directory
# (#529 S4). The write detector caught this module creating the maintainer's
# real ~/.local/share/cctally: its tests call load_script() themselves, and
# load_script() re-derives every path constant from HOME on each call, so a
# redirect_paths() patch would be clobbered by the next one.
pytestmark = pytest.mark.usefixtures("isolated_home")

_BIN_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "bin")


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _jr():
    import _cctally_journal
    return _cctally_journal


def _jlib():
    import _lib_journal
    return _lib_journal


_AS_OF = "2026-01-04T09:00:00Z"


def _seed_snapshot(
    conn,
    *,
    captured_at_utc,
    week_start_date,
    weekly_percent,
    week_start_at=None,
    week_end_at=None,
    five_hour_percent=None,
    five_hour_window_key=None,
    source="test",
    weekly_observation_held=0,
):
    if week_start_at is None:
        week_start_at = week_start_date + "T00:00:00+00:00"
    if week_end_at is None:
        week_end_at = week_start_date + "T00:00:00+00:00"
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, "
        " five_hour_percent, five_hour_resets_at, five_hour_window_key, "
        " weekly_observation_held) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (captured_at_utc, week_start_date, week_start_date[:10], week_start_at,
         week_end_at, weekly_percent, source, "{}",
         five_hour_percent, None, five_hour_window_key,
         weekly_observation_held),
    )
    conn.commit()


def _payload(weekly_percent, *, week_start_date="2026-01-01",
             five_hour_percent=None, five_hour_window_key=None):
    p = {
        "week_start_date": week_start_date,
        "week_start_at": week_start_date + "T00:00:00+00:00",
        "week_end_at": "2026-01-08T00:00:00+00:00",
        "weekly_percent": weekly_percent,
    }
    if five_hour_percent is not None:
        p["five_hour_percent"] = five_hour_percent
    if five_hour_window_key is not None:
        p["five_hour_window_key"] = five_hour_window_key
    return p


# ==========================================================================
# #769 S11 (#824) — the held-provenance column. `weekly_observation_held`
# separates a weekly value that was genuinely observed on this tick from one
# carried forward so a weekly-clamped tick can still persist its five-hour
# evidence.
# ==========================================================================

def test_weekly_usage_snapshots_carries_the_held_provenance_column(ns):
    conn = ns["open_db"]()
    try:
        cols = {row[1]: row for row in conn.execute(
            "PRAGMA table_info(weekly_usage_snapshots)")}
    finally:
        conn.close()
    assert "weekly_observation_held" in cols
    # notnull flag is index 3, default value index 4
    assert cols["weekly_observation_held"][3] == 1
    assert str(cols["weekly_observation_held"][4]) == "0"


def test_weekly_observation_held_rejects_a_value_outside_the_flag_domain(ns):
    import sqlite3

    conn = ns["open_db"]()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _seed_snapshot(
                conn, captured_at_utc="2026-01-04T08:00:00Z",
                week_start_date="2026-01-01", weekly_percent=10.0,
                weekly_observation_held=2,
            )
    finally:
        conn.rollback()
        conn.close()


# ==========================================================================
# _usage_snapshot_fold_decision (gate P2 pickup a): clamp-skip, 5h-adjust-up-
# never-gate, dedup-skip, accept — all over seeded rows.
# ==========================================================================

def test_fold_decision_accept_on_empty_week(ns):
    jr = _jr()
    conn = ns["open_db"]()
    try:
        result = jr._usage_snapshot_fold_decision(conn, _payload(10.0))
    finally:
        conn.close()
    assert result.snapshot_action == jr.SNAPSHOT_WRITE_OBSERVED
    assert result.weekly.disposition == jr.WEEKLY_OBSERVED
    assert result.weekly.raw_pct == 10.0
    assert result.weekly.effective_pct == 10.0
    assert result.weekly.basis is None
    assert result.five_hour.disposition == jr.FIVE_HOUR_MISSING
    assert result.five_hour.raw_pct is None
    assert result.five_hour.effective_pct is None


def test_fold_decision_clamp_holds_the_weekly_axis(ns):
    jr = _jr()
    conn = ns["open_db"]()
    try:
        # A higher 7d MAX already sits in this week; a lower incoming % is
        # below the reset-aware HWM → the weekly axis is HELD at the MAX.
        # With no five-hour evidence on either side there is nothing to
        # persist, so the row is still not written.
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date="2026-01-01", weekly_percent=50.0,
        )
        result = jr._usage_snapshot_fold_decision(conn, _payload(40.0))
    finally:
        conn.close()
    assert result.weekly.disposition == jr.WEEKLY_HELD_CLAMP
    assert result.weekly.raw_pct == 40.0
    assert result.weekly.effective_pct == 50.0
    assert result.snapshot_action == jr.SNAPSHOT_SKIP_NO_CHANGE


def test_fold_decision_5h_adjusts_up_never_gates(ns):
    jr = _jr()
    conn = ns["open_db"]()
    try:
        # 7d rises (15 > 10, no 7d clamp) but the incoming 5h (5.0) sits below
        # the in-window 5h MAX (20.0): the 5h leg adjusts the value UP to 20.0
        # and NEVER gates the row.
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date="2026-01-01", weekly_percent=10.0,
            five_hour_percent=20.0, five_hour_window_key=7770,
        )
        result = jr._usage_snapshot_fold_decision(
            conn, _payload(15.0, five_hour_percent=5.0, five_hour_window_key=7770)
        )
    finally:
        conn.close()
    assert result.snapshot_action == jr.SNAPSHOT_WRITE_OBSERVED
    assert result.weekly.disposition == jr.WEEKLY_OBSERVED
    assert result.five_hour.disposition == jr.FIVE_HOUR_CLAMPED
    assert result.five_hour.raw_pct == 5.0
    assert result.five_hour.effective_pct == 20.0


def test_fold_decision_dedup_skip(ns):
    jr = _jr()
    conn = ns["open_db"]()
    try:
        # Latest snapshot has identical weekly AND 5h → nothing changed.
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date="2026-01-01", weekly_percent=30.0,
            five_hour_percent=8.0, five_hour_window_key=7771,
        )
        result = jr._usage_snapshot_fold_decision(
            conn, _payload(30.0, five_hour_percent=8.0, five_hour_window_key=7771)
        )
    finally:
        conn.close()
    assert result.snapshot_action == jr.SNAPSHOT_SKIP_NO_CHANGE
    assert result.weekly.disposition == jr.WEEKLY_OBSERVED
    assert result.five_hour.effective_pct == 8.0


# ==========================================================================
# #769 S11 (#824) — the fold's axis-aware truth table, driven row by row.
#
# Every row of the specification's eleven-row table appears once here, named
# for the case it pins. The decisive rows are the four `WEEKLY_HELD_CLAMP`
# ones: before this change the fold returned on the weekly clamp, so the
# five-hour clamp below it never ran and the tick's genuine five-hour reading
# was returned unclamped and then discarded by the pipeline.
# ==========================================================================

#: (name, seeded rows, payload kwargs, expected fold fields). Each seed is the
#: kwargs of one `_seed_snapshot` call, applied in order.
_TRUTH_TABLE = [
    (
        # Row 1. Weekly changed and genuinely observed; no five-hour input.
        "weekly_changed_five_hour_missing",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=10.0)],
        dict(weekly_percent=20.0),
        dict(weekly_disposition="WEEKLY_OBSERVED",
             weekly_raw=20.0, weekly_effective=20.0,
             five_hour_disposition="FIVE_HOUR_MISSING",
             five_hour_raw=None, five_hour_effective=None,
             action="SNAPSHOT_WRITE_OBSERVED", rollover_heal=False),
    ),
    (
        # Row 2. Weekly changed; the five-hour value rose in the same window.
        "weekly_changed_five_hour_as_observed",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=10.0,
              five_hour_percent=5.0, five_hour_window_key=7770)],
        dict(weekly_percent=20.0, five_hour_percent=8.0,
             five_hour_window_key=7770),
        dict(weekly_disposition="WEEKLY_OBSERVED",
             weekly_raw=20.0, weekly_effective=20.0,
             five_hour_disposition="FIVE_HOUR_AS_OBSERVED",
             five_hour_raw=8.0, five_hour_effective=8.0,
             action="SNAPSHOT_WRITE_OBSERVED", rollover_heal=False),
    ),
    (
        # Row 3. Weekly changed; the five-hour reading is BELOW the in-window
        # maximum, so it clamps UP and still never gates the row.
        "weekly_changed_five_hour_clamped",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=10.0,
              five_hour_percent=20.0, five_hour_window_key=7770)],
        dict(weekly_percent=15.0, five_hour_percent=5.0,
             five_hour_window_key=7770),
        dict(weekly_disposition="WEEKLY_OBSERVED",
             weekly_raw=15.0, weekly_effective=15.0,
             five_hour_disposition="FIVE_HOUR_CLAMPED",
             five_hour_raw=5.0, five_hour_effective=20.0,
             action="SNAPSHOT_WRITE_OBSERVED", rollover_heal=False),
    ),
    (
        # Row 4. Weekly unchanged and genuine; the five-hour value moved.
        "weekly_unchanged_five_hour_moved",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=30.0,
              five_hour_percent=8.0, five_hour_window_key=7771)],
        dict(weekly_percent=30.0, five_hour_percent=12.0,
             five_hour_window_key=7771),
        dict(weekly_disposition="WEEKLY_OBSERVED",
             weekly_raw=30.0, weekly_effective=30.0,
             five_hour_disposition="FIVE_HOUR_AS_OBSERVED",
             five_hour_raw=12.0, five_hour_effective=12.0,
             action="SNAPSHOT_WRITE_OBSERVED", rollover_heal=False),
    ),
    (
        # Row 5a. Weekly unchanged and genuine; the five-hour pair is identical.
        "weekly_unchanged_five_hour_identical",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=30.0,
              five_hour_percent=8.0, five_hour_window_key=7771)],
        dict(weekly_percent=30.0, five_hour_percent=8.0,
             five_hour_window_key=7771),
        dict(weekly_disposition="WEEKLY_OBSERVED",
             weekly_raw=30.0, weekly_effective=30.0,
             five_hour_disposition="FIVE_HOUR_AS_OBSERVED",
             five_hour_raw=8.0, five_hour_effective=8.0,
             action="SNAPSHOT_SKIP_NO_CHANGE", rollover_heal=False),
    ),
    (
        # Row 5b. Weekly unchanged and genuine; no five-hour input at all.
        "weekly_unchanged_five_hour_missing",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=30.0)],
        dict(weekly_percent=30.0),
        dict(weekly_disposition="WEEKLY_OBSERVED",
             weekly_raw=30.0, weekly_effective=30.0,
             five_hour_disposition="FIVE_HOUR_MISSING",
             five_hour_raw=None, five_hour_effective=None,
             action="SNAPSHOT_SKIP_NO_CHANGE", rollover_heal=False),
    ),
    (
        # Row 6. Weekly unchanged and genuine; the five-hour value is the same
        # but the physical window rolled over and no block anchors the new one.
        # The row stays unwritten and the heal flag is what the pipeline acts
        # on, which is why `rollover_heal` is orthogonal to `snapshot_action`.
        "weekly_unchanged_rollover_heal",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=30.0,
              five_hour_percent=8.0, five_hour_window_key=7771)],
        dict(weekly_percent=30.0, five_hour_percent=8.0,
             five_hour_window_key=7772),
        dict(weekly_disposition="WEEKLY_OBSERVED",
             weekly_raw=30.0, weekly_effective=30.0,
             five_hour_disposition="FIVE_HOUR_AS_OBSERVED",
             five_hour_raw=8.0, five_hour_effective=8.0,
             action="SNAPSHOT_SKIP_NO_CHANGE", rollover_heal=True),
    ),
    (
        # Row 7. THE DECISIVE ROW. The weekly axis clamps against the stored
        # 63 while the five-hour axis carries genuine growth to 25, so the row
        # is written with the weekly value HELD.
        "weekly_held_five_hour_grew",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=63.0,
              five_hour_percent=10.0, five_hour_window_key=7772)],
        dict(weekly_percent=60.0, five_hour_percent=25.0,
             five_hour_window_key=7772),
        dict(weekly_disposition="WEEKLY_HELD_CLAMP",
             weekly_raw=60.0, weekly_effective=63.0,
             five_hour_disposition="FIVE_HOUR_AS_OBSERVED",
             five_hour_raw=25.0, five_hour_effective=25.0,
             action="SNAPSHOT_WRITE_HELD_5H", rollover_heal=False),
    ),
    (
        # Row 8. Weekly held; the five-hour reading is an exact duplicate in
        # the same window, so neither axis carries new evidence.
        "weekly_held_five_hour_exact_duplicate",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=63.0,
              five_hour_percent=10.0, five_hour_window_key=7772)],
        dict(weekly_percent=60.0, five_hour_percent=10.0,
             five_hour_window_key=7772),
        dict(weekly_disposition="WEEKLY_HELD_CLAMP",
             weekly_raw=60.0, weekly_effective=63.0,
             five_hour_disposition="FIVE_HOUR_AS_OBSERVED",
             five_hour_raw=10.0, five_hour_effective=10.0,
             action="SNAPSHOT_SKIP_NO_CHANGE", rollover_heal=False),
    ),
    (
        # Row 9. Weekly held; the raw five-hour reading clamps UP onto the
        # stored value, so the effective pair is a duplicate after the clamp
        # the old early return never reached.
        "weekly_held_five_hour_clamps_to_duplicate",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=63.0,
              five_hour_percent=10.0, five_hour_window_key=7772)],
        dict(weekly_percent=60.0, five_hour_percent=5.0,
             five_hour_window_key=7772),
        dict(weekly_disposition="WEEKLY_HELD_CLAMP",
             weekly_raw=60.0, weekly_effective=63.0,
             five_hour_disposition="FIVE_HOUR_CLAMPED",
             five_hour_raw=5.0, five_hour_effective=10.0,
             action="SNAPSHOT_SKIP_NO_CHANGE", rollover_heal=False),
    ),
    (
        # Row 10. Weekly held and no five-hour input: nothing to persist.
        "weekly_held_five_hour_missing",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=63.0)],
        dict(weekly_percent=60.0),
        dict(weekly_disposition="WEEKLY_HELD_CLAMP",
             weekly_raw=60.0, weekly_effective=63.0,
             five_hour_disposition="FIVE_HOUR_MISSING",
             five_hour_raw=None, five_hour_effective=None,
             action="SNAPSHOT_SKIP_NO_CHANGE", rollover_heal=False),
    ),
    (
        # Row 11. Weekly held; the five-hour value is unchanged but the window
        # rolled over, so the heal is owed without a row being written.
        "weekly_held_rollover_heal",
        [dict(captured_at_utc="2026-01-04T08:00:00Z",
              week_start_date="2026-01-01", weekly_percent=63.0,
              five_hour_percent=10.0, five_hour_window_key=7772)],
        dict(weekly_percent=60.0, five_hour_percent=10.0,
             five_hour_window_key=7773),
        dict(weekly_disposition="WEEKLY_HELD_CLAMP",
             weekly_raw=60.0, weekly_effective=63.0,
             five_hour_disposition="FIVE_HOUR_AS_OBSERVED",
             five_hour_raw=10.0, five_hour_effective=10.0,
             action="SNAPSHOT_SKIP_NO_CHANGE", rollover_heal=True),
    ),
]


@pytest.mark.parametrize(
    "name,seeds,payload_kwargs,expected",
    _TRUTH_TABLE,
    ids=[row[0] for row in _TRUTH_TABLE],
)
def test_fold_truth_table(ns, name, seeds, payload_kwargs, expected):
    jr = _jr()
    conn = ns["open_db"]()
    try:
        for seed in seeds:
            _seed_snapshot(conn, **seed)
        weekly_percent = payload_kwargs.pop("weekly_percent")
        result = jr._usage_snapshot_fold_decision(
            conn, _payload(weekly_percent, **payload_kwargs))
    finally:
        conn.close()

    assert result.weekly.disposition == getattr(
        jr, expected["weekly_disposition"])
    assert result.weekly.raw_pct == expected["weekly_raw"]
    assert result.weekly.effective_pct == expected["weekly_effective"]
    assert result.five_hour.disposition == getattr(
        jr, expected["five_hour_disposition"])
    assert result.five_hour.raw_pct == expected["five_hour_raw"]
    assert result.five_hour.effective_pct == expected["five_hour_effective"]
    assert result.snapshot_action == getattr(jr, expected["action"])
    assert result.five_hour.rollover_heal is expected["rollover_heal"]


def test_the_truth_table_covers_every_declared_outcome():
    """Non-vacuity for the table above. Coverage by counting rows is not
    coverage: the point is that each declared weekly disposition, five-hour
    disposition and snapshot action is actually exercised."""
    jr = _jr()
    weeklies = {row[3]["weekly_disposition"] for row in _TRUTH_TABLE}
    five_hours = {row[3]["five_hour_disposition"] for row in _TRUTH_TABLE}
    actions = {row[3]["action"] for row in _TRUTH_TABLE}
    assert weeklies == {"WEEKLY_OBSERVED", "WEEKLY_HELD_CLAMP"}
    assert five_hours == {
        "FIVE_HOUR_MISSING", "FIVE_HOUR_AS_OBSERVED", "FIVE_HOUR_CLAMPED"}
    assert actions == {
        "SNAPSHOT_WRITE_OBSERVED", "SNAPSHOT_WRITE_HELD_5H",
        "SNAPSHOT_SKIP_NO_CHANGE"}
    assert any(row[3]["rollover_heal"] for row in _TRUTH_TABLE)
    # And the constants are distinct strings, so a copy-paste that collapsed
    # two of them would fail here rather than silently reclassify a tick.
    assert len({
        jr.WEEKLY_OBSERVED, jr.WEEKLY_HELD_CLAMP,
        jr.FIVE_HOUR_MISSING, jr.FIVE_HOUR_AS_OBSERVED, jr.FIVE_HOUR_CLAMPED,
        jr.SNAPSHOT_WRITE_OBSERVED, jr.SNAPSHOT_WRITE_HELD_5H,
        jr.SNAPSHOT_SKIP_NO_CHANGE,
    }) == 8


def test_the_weekly_basis_is_the_latest_NON_held_row(ns):
    """A held row's weekly value and boundary are carried forward, so a later
    held row that took its basis from another held row would compound the
    carry. The basis is therefore selected from non-held rows only.

    Non-vacuous by construction: the seeded held row carries a DIFFERENT
    boundary from the genuine one, so selecting it would be visible.
    """
    jr = _jr()
    conn = ns["open_db"]()
    try:
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date="2026-01-01", weekly_percent=63.0,
            week_end_at="2026-01-08T00:00:00+00:00",
            five_hour_percent=10.0, five_hour_window_key=7772)
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T08:30:00Z",
            week_start_date="2026-01-01", weekly_percent=63.0,
            week_end_at="2026-01-09T00:00:00+00:00",
            five_hour_percent=15.0, five_hour_window_key=7772,
            weekly_observation_held=1)
        result = jr._usage_snapshot_fold_decision(
            conn, _payload(60.0, five_hour_percent=25.0,
                           five_hour_window_key=7772))
    finally:
        conn.close()
    assert result.snapshot_action == jr.SNAPSHOT_WRITE_HELD_5H
    assert result.weekly.basis is not None
    assert result.weekly.basis.captured_at_utc == "2026-01-04T08:00:00Z"
    assert result.weekly.basis.week_end_at == "2026-01-08T00:00:00+00:00"
    assert result.weekly.basis.weekly_percent == 63.0


def test_a_held_row_is_excluded_from_the_weekly_high_water_max(ns):
    """Review finding 7, the half a `= 0` predicate on the predecessor query
    alone does not cover. A credit moves the reset-aware clamp floor forward,
    and the MAX is taken over rows at or after it. A held row written after the
    floor carries a weekly value from BEFORE it, so counting it would clamp a
    genuine post-credit reading against pre-credit evidence — the exact
    contamination that makes a fresh low look stale."""
    jr = _jr()
    conn = ns["open_db"]()
    try:
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date="2026-01-01", weekly_percent=63.0)
        conn.execute(
            "INSERT INTO weekly_credit_floors "
            "(week_start_date, effective_at_utc, observed_pre_credit_pct, "
            " applied_at_utc, account_key) VALUES (?,?,?,?,?)",
            ("2026-01-01", "2026-01-04T09:00:00+00:00", 63.0,
             "2026-01-04T09:00:00Z", "unattributed"))
        # The held row lands AFTER the credit floor and carries the pre-credit
        # 63 forward, because that is what a held row is.
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T10:00:00Z",
            week_start_date="2026-01-01", weekly_percent=63.0,
            five_hour_percent=10.0, five_hour_window_key=7772,
            weekly_observation_held=1)
        conn.commit()
        result = jr._usage_snapshot_fold_decision(
            conn, _payload(40.0, five_hour_percent=12.0,
                           five_hour_window_key=7772))
    finally:
        conn.close()
    assert result.weekly.disposition == jr.WEEKLY_OBSERVED, (
        "the held row's carried-forward weekly value entered the post-credit "
        "high-water MAX and clamped a genuine reading")
    assert result.weekly.effective_pct == 40.0
    assert result.snapshot_action == jr.SNAPSHOT_WRITE_OBSERVED


def test_the_fold_result_is_frozen(ns):
    """The pipeline threads one result through the milestone gate, the block
    derivation and both high-water writers. A mutable result would let any of
    them rewrite a decision the journal has already recorded."""
    import dataclasses

    jr = _jr()
    conn = ns["open_db"]()
    try:
        result = jr._usage_snapshot_fold_decision(conn, _payload(10.0))
    finally:
        conn.close()
    for obj in (result, result.weekly, result.five_hour):
        assert dataclasses.is_dataclass(obj)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, "disposition" if obj is not result else
                    "snapshot_action", "tampered")


# ==========================================================================
# #769 S11 (#824) — a held row is invisible to every WEEKLY read and visible to
# every five-hour one. The reads a held row can actually change are the ones
# whose answer depends on a TIME filter, because a held row's capture time is
# the tick's while its weekly value is an older row's: a filter can admit the
# held row and exclude the basis it copied from, which carries a value from
# outside the window into a window it does not belong to.
# ==========================================================================

def _seed_credit_floor(conn, *, week_start_date="2026-01-01",
                       effective_at_utc, observed_pre_credit_pct):
    conn.execute(
        "INSERT INTO weekly_credit_floors "
        "(week_start_date, effective_at_utc, observed_pre_credit_pct, "
        " applied_at_utc, account_key) VALUES (?,?,?,?,?)",
        (week_start_date, effective_at_utc, observed_pre_credit_pct,
         effective_at_utc, "unattributed"))
    conn.commit()


def test_the_reset_aware_weekly_hwm_excludes_held_rows(ns):
    """`_resolve_reset_aware_hwm` is the `--from` default and the record-credit
    tests' source of truth for "what the week reached". A held row written after
    a credit floor carries the PRE-credit value forward, so counting it would
    report a high-water mark the credit already retired."""
    conn = ns["open_db"]()
    try:
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date="2026-01-01", weekly_percent=63.0)
        _seed_credit_floor(
            conn, effective_at_utc="2026-01-04T09:00:00+00:00",
            observed_pre_credit_pct=63.0)
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T10:00:00Z",
            week_start_date="2026-01-01", weekly_percent=63.0,
            five_hour_percent=25.0, five_hour_window_key=7772,
            weekly_observation_held=1)
        hwm = ns["_resolve_reset_aware_hwm"](
            conn, "2026-01-01", "2026-01-01T00:00:00+00:00",
            "2026-01-08T00:00:00+00:00", account_key="unattributed")
    finally:
        conn.close()
    assert hwm is None, (
        "the held row's carried-forward weekly value survived the credit floor")


def test_the_weekly_predecessor_for_reset_detection_skips_held_rows(ns):
    """The query `detect_reset_and_credit` reads its previous boundary from.

    The pipeline copies a held row's boundary from its basis, so on a row this
    binary wrote the two agree and excluding held rows changes nothing. That is
    the point: the exclusion is what makes the copy's correctness NOT
    load-bearing. A row whose boundary diverges is seeded directly here — a
    hand-written row, or one an older shape produced — and the predecessor must
    still be the basis.
    """
    conn = ns["open_db"]()
    try:
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date="2026-01-01", weekly_percent=63.0,
            week_end_at="2026-01-08T00:00:00+00:00")
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T10:00:00Z",
            week_start_date="2026-01-01", weekly_percent=41.0,
            week_end_at="2026-01-11T00:00:00+00:00",
            weekly_observation_held=1)
        prior = conn.execute(
            "SELECT week_end_at, weekly_percent FROM weekly_usage_snapshots "
            "WHERE week_end_at IS NOT NULL AND account_key = ? "
            "  AND weekly_observation_held = 0 "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
            ("unattributed",),
        ).fetchone()
        shipped = ns["detect_reset_and_credit"]
    finally:
        conn.close()
    assert prior["week_end_at"] == "2026-01-08T00:00:00+00:00"
    assert prior["weekly_percent"] == 63.0
    # And the shipped query carries the predicate, so the assertion above is
    # about the code rather than about a query this test wrote for itself.
    import inspect
    source = inspect.getsource(shipped)
    assert "SELECT week_end_at, weekly_percent FROM weekly_usage_snapshots" in source
    assert source.count("weekly_observation_held = 0") >= 1


def test_a_held_predecessor_does_not_become_the_reset_baseline(ns):
    """The behavioural half of the weekly exclusion, which the two tests around
    it assert only against the text of the shipped queries.

    `detect_reset_and_credit` derives a week rollover by comparing the incoming
    boundary against the previous accepted row's, and records that row's
    boundary as the week the reset moved away from. A held row can carry a
    boundary that diverges from its basis, so which row is selected is directly
    observable in `old_week_end_at`: the basis gives 2026-01-08 and the held row
    gives 2026-01-11.

    `observed_pre_credit_pct` stays NULL here and is not asserted, because a
    plain rollover is not an in-place credit and only the credit path fills it.
    """
    conn = ns["open_db"]()
    try:
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date="2026-01-01", weekly_percent=63.0,
            week_end_at="2026-01-08T00:00:00+00:00")
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T10:00:00Z",
            week_start_date="2026-01-01", weekly_percent=41.0,
            week_end_at="2026-01-11T00:00:00+00:00",
            weekly_observation_held=1)
        moment = "2026-01-04T11:00:00Z"
        ns["detect_reset_and_credit"](
            conn,
            week_start_date="2026-01-08",
            week_end_at="2026-01-15T00:00:00+00:00",
            weekly_percent=5.0,
            five_hour_window_key=None,
            five_hour_percent=None,
            as_of=moment,
            commit=False,
            capture_at=moment,
        )
        events = conn.execute(
            "SELECT old_week_end_at, observed_pre_credit_pct "
            "FROM week_reset_events ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    assert events, "non-vacuity: the boundary change derived no reset at all"
    assert events[0]["old_week_end_at"] == "2026-01-08T00:00:00+00:00", (
        "the rollover was measured from the held row's divergent boundary")


def test_the_reset_event_backfill_scan_skips_held_rows(ns):
    """`_backfill_week_reset_events` walks each account's snapshots in capture
    order and derives a reset from a consecutive boundary change plus a drop. A
    held row repeats its basis's boundary and value, so it can only ever
    contribute a no-change pair — but a row whose boundary diverges would derive
    a phantom reset, and the exclusion is what makes that impossible rather than
    improbable."""
    import inspect
    import _cctally_weekrefs
    source = inspect.getsource(_cctally_weekrefs._backfill_week_reset_events)
    assert "FROM weekly_usage_snapshots" in source
    assert "weekly_observation_held = 0" in source


def test_the_five_hour_reads_stay_held_inclusive(ns):
    """The other half of the rule, at the level of stored state: the latest
    five-hour window and value must come from the held row, because that row is
    where a weekly-clamped tick's five-hour evidence now lives.

    This asserts the index the pipeline wrote, using test-local queries; it
    drives no production five-hour read of its own. The production reads are
    exercised by the fold's own clamp in the truth-table cases above and by the
    rematerialization tests below.
    """
    jr = _jr()
    J = _jlib()
    _held_sequence(jr, J)

    conn = ns["open_db"]()
    try:
        latest = conn.execute(
            "SELECT five_hour_percent, five_hour_window_key "
            "FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1").fetchone()
        max_5h = conn.execute(
            "SELECT MAX(five_hour_percent) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    finally:
        conn.close()
    assert latest["five_hour_percent"] == 25.0
    assert max_5h == 25.0


# ==========================================================================
# #769 S11 (#824) — replay. `db rebuild` is apply-only and reconstructs open
# blocks from snapshot history; `db rederive` reruns the raw pipeline. A
# five-hour effect that existed only as a block write and never as a journalled
# snapshot decision would survive one and vanish in the other, which is why the
# held tick is persisted as an ordinary `snapshot_accept` decision.
# ==========================================================================

def _held_convergence_oracle(conn, app_dir):
    """The logical comparison the specification names — row ids excluded,
    because two builds legitimately disagree on them."""
    snap = conn.execute(
        "SELECT weekly_percent, weekly_observation_held, five_hour_percent, "
        "       five_hour_window_key, captured_at_utc, week_start_date, "
        "       week_end_date, week_start_at, week_end_at, source "
        "FROM weekly_usage_snapshots "
        "ORDER BY captured_at_utc, weekly_observation_held").fetchall()
    blocks = conn.execute(
        "SELECT five_hour_window_key, seven_day_pct_at_block_start, "
        "       seven_day_pct_at_block_end, final_five_hour_percent "
        "FROM five_hour_blocks ORDER BY five_hour_window_key").fetchall()
    five_hour_milestones = conn.execute(
        "SELECT five_hour_window_key, percent_threshold, captured_at_utc, "
        "       seven_day_pct_at_crossing, reset_event_id "
        "FROM five_hour_milestones "
        "ORDER BY five_hour_window_key, percent_threshold").fetchall()
    weekly_milestones = conn.execute(
        "SELECT week_start_date, percent_threshold, reset_event_id "
        "FROM percent_milestones "
        "ORDER BY week_start_date, percent_threshold").fetchall()
    return {
        "snapshots": [tuple(r) for r in snap],
        "blocks": [tuple(r) for r in blocks],
        "fiveHourMilestones": [tuple(r) for r in five_hour_milestones],
        "weeklyMilestones": [tuple(r) for r in weekly_milestones],
        "hwm5h": _read_projection(app_dir, "hwm-5h"),
        "hwm7d": _read_projection(app_dir, "hwm-7d"),
    }


def _read_projection(app_dir, name):
    try:
        return app_dir.joinpath(name).read_text().strip()
    except OSError:
        return None


def test_a_rebuild_reproduces_the_held_row_and_its_five_hour_effects(ns):
    """`db rebuild`'s whole contract for this change: the held tick was
    journalled as a `snapshot_accept` decision, so an apply-only replay into a
    fresh index reproduces the row, its block and its milestone without
    rerunning the pipeline."""
    jr = _jr()
    J = _jlib()
    _held_sequence(jr, J)
    app_dir = pathlib.Path(str(ns["APP_DIR"]))

    conn = ns["open_db"]()
    try:
        before = _held_convergence_oracle(conn, app_dir)
    finally:
        conn.close()
    assert before["snapshots"][-1][1] == 1, "non-vacuity: a held row exists"

    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))
    assert result is not None

    conn = ns["open_db"]()
    try:
        after = _held_convergence_oracle(conn, app_dir)
    finally:
        conn.close()

    assert after["snapshots"] == before["snapshots"]
    assert after["blocks"] == before["blocks"]
    assert after["fiveHourMilestones"] == before["fiveHourMilestones"]
    assert after["weeklyMilestones"] == before["weeklyMilestones"]
    assert after["hwm5h"] == before["hwm5h"]
    assert after["hwm7d"] == before["hwm7d"]


def test_a_rebuild_rematerializes_hwm_5h_and_never_touches_hwm_7d(ns):
    """The projection half. A rebuild does not rerun the live pipeline, so
    without a rematerialization pass `hwm-5h` would keep whatever it happened to
    hold — and `db rederive`, whose scratch derivation disables projection
    writes, would leave it somewhere else again."""
    jr = _jr()
    J = _jlib()
    _held_sequence(jr, J)
    app_dir = pathlib.Path(str(ns["APP_DIR"]))

    # Corrupt both projections, then rebuild.
    app_dir.joinpath("hwm-5h").write_text("1 1.0\n")
    app_dir.joinpath("hwm-7d").write_text("2026-01-01 5.0\n")
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    assert _read_projection(app_dir, "hwm-5h").split()[1] == "25.0", (
        "hwm-5h was not rematerialized from the published index")
    assert _read_projection(app_dir, "hwm-7d") == "2026-01-01 5.0", (
        "the rebuild wrote the weekly projection")


def _seed_five_hour_index(path, *, window_key, account_key, rows, reset_at=None):
    """Build a minimal read-only stand-in for a published stats index.

    `_rematerialize_five_hour_high_water` opens its destination read-only and
    touches exactly two tables, so a hand-built database exercises the real
    query. Hand-built rows would be worthless in a test that then rebuilds —
    a rebuild reconstructs the index from the journal and would discard them —
    but nothing here rebuilds: the subject is the SQL this pass runs against an
    index that has already been published.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE weekly_usage_snapshots ("
            " id INTEGER PRIMARY KEY, captured_at_utc TEXT, "
            " five_hour_percent REAL, five_hour_window_key INTEGER, "
            " account_key TEXT NOT NULL DEFAULT 'unattributed')")
        conn.execute(
            "CREATE TABLE five_hour_reset_events ("
            " id INTEGER PRIMARY KEY, five_hour_window_key INTEGER, "
            " account_key TEXT, effective_reset_at_utc TEXT)")
        for captured_at, pct in rows:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, five_hour_percent, five_hour_window_key, "
                " account_key) VALUES (?, ?, ?, ?)",
                (captured_at, pct, window_key, account_key))
        if reset_at is not None:
            conn.execute(
                "INSERT INTO five_hour_reset_events "
                "(five_hour_window_key, account_key, effective_reset_at_utc) "
                "VALUES (?, ?, ?)", (window_key, account_key, reset_at))
        conn.commit()
    finally:
        conn.close()


def test_the_rematerialization_publishes_the_reset_aware_maximum(ns, tmp_path):
    """The pass publishes the EFFECTIVE five-hour value, which is the
    reset-aware in-window maximum the fold's own clamp computes — not the raw
    maximum.

    An in-place five-hour credit retires the pre-credit peak without deleting
    every row that carries it: the stale-replica DELETE bands only at or after
    the credit instant, so the earlier climb stays in the index. A pass that
    took an unfloored MAX would republish that retired peak and push the status
    line's no-regression floor back above a level the meter has already moved
    past, which is the outcome the credit's own force-write exists to prevent.

    The expectation is a literal rather than a recomputation from the index,
    because an oracle that re-runs production's own query agrees with it whether
    or not that query is correct.
    """
    jr = _jr()
    app_dir = pathlib.Path(str(ns["APP_DIR"]))
    destination = tmp_path / "published-stats.db"
    _seed_five_hour_index(
        destination, window_key=7772, account_key="unattributed",
        rows=[("2026-01-04T08:00:00Z", 30.0),
              ("2026-01-04T08:30:00Z", 5.0)],
        reset_at="2026-01-04T08:20:00Z")

    conn = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        raw_max = conn.execute(
            "SELECT MAX(five_hour_percent) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    finally:
        conn.close()
    assert raw_max == 30.0, (
        "non-vacuity: the retired pre-credit peak is still in the index, so an "
        "unfloored MAX would find it")

    app_dir.joinpath("hwm-5h").write_text("1 1.0\n")
    jr._rematerialize_five_hour_high_water(str(destination))

    assert _read_projection(app_dir, "hwm-5h") == "7772 5.0", (
        "the pass republished a five-hour level the credit retired")


def test_the_rematerialization_without_a_credit_uses_the_whole_window(ns,
                                                                     tmp_path):
    """The floor is a floor, not a filter: with no reset event in the window
    the pass still publishes the window's full maximum. Without this, a change
    that over-restricted the floor would pass the test above by publishing a
    value that is merely lower."""
    jr = _jr()
    app_dir = pathlib.Path(str(ns["APP_DIR"]))
    destination = tmp_path / "published-stats.db"
    _seed_five_hour_index(
        destination, window_key=7772, account_key="unattributed",
        rows=[("2026-01-04T08:00:00Z", 30.0),
              ("2026-01-04T08:30:00Z", 12.0)])

    app_dir.joinpath("hwm-5h").write_text("1 1.0\n")
    jr._rematerialize_five_hour_high_water(str(destination))

    assert _read_projection(app_dir, "hwm-5h") == "7772 30.0"


def test_a_scratch_target_rebuild_mutates_no_projection_file(ns, tmp_path):
    """Preview and scratch-target operations mutate no high-water file. A
    preview that moved what the status line renders would be a preview with a
    side effect."""
    jr = _jr()
    J = _jlib()
    _held_sequence(jr, J)
    app_dir = pathlib.Path(str(ns["APP_DIR"]))
    app_dir.joinpath("hwm-5h").write_text("1 1.0\n")

    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(tmp_path / "scratch-stats.db"))

    assert _read_projection(app_dir, "hwm-5h") == "1 1.0"


# ==========================================================================
# alert_sink threading (item 2): the passed-conn milestone chokepoint appends
# its new-crossing payloads to the sink (for the ingester's post-commit
# dispatch) instead of dropping them — and never dispatches itself.
# ==========================================================================

def test_alert_sink_receives_milestone_payload(ns, monkeypatch):
    calls = []
    monkeypatch.setitem(
        ns, "_dispatch_alert_notification",
        lambda *a, **k: calls.append((a, k)),
    )
    monkeypatch.setitem(
        ns, "load_config",
        lambda *a, **k: {"alerts": {"enabled": True, "weekly_thresholds": [5]}},
    )
    week_start_date = "2026-01-01"
    week_end_at = "2026-01-07T23:59:59+00:00"
    conn = ns["open_db"]()
    sink = []
    try:
        cur = conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, source, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-01-04T08:00:00Z", week_start_date, "2026-01-07",
             "2026-01-01T00:00:00+00:00", week_end_at, 5.0, "test", "{}"),
        )
        conn.commit()
        snap_id = int(cur.lastrowid)
        saved = {
            "id": snap_id,
            "weeklyPercent": 5.0,
            "weekStartDate": week_start_date,
            "weekEndDate": "2026-01-07",
            "weekStartAt": "2026-01-01T00:00:00+00:00",
            "weekEndAt": week_end_at,
            "capturedAt": "2026-01-04T08:00:00Z",
        }
        conn.execute("BEGIN IMMEDIATE")
        ns["maybe_record_milestone"](
            saved, conn=conn, as_of=_AS_OF, alert_sink=sink
        )
        # The crossing stamped alerted_at IN the caller's txn (survives rebuild).
        row = conn.execute(
            "SELECT alerted_at FROM percent_milestones "
            "WHERE week_start_date = ? AND percent_threshold = 5",
            (week_start_date,),
        ).fetchone()
        assert row is not None and row[0] == _AS_OF
        conn.commit()
    finally:
        conn.close()
    # The payload went to the SINK, not the dispatcher.
    assert len(sink) == 1
    assert sink[0]["threshold"] == 5
    assert calls == [], "passed-conn chokepoint must not dispatch alerts itself"


# ==========================================================================
# harvest early-out (gate P2 pickup b): a cycle whose pipeline writes nothing
# skips the 8 journal_id-IS-NULL harvest scans; a cycle that inserts a natural-
# keyed row still harvests + stamps it.
# ==========================================================================

FIXED = dt.datetime(2026, 1, 4, 12, 0, 0, tzinfo=dt.timezone.utc)


def _obs(J, at="2026-01-04T12:00:00Z"):
    return J.make_obs(
        at=at, src="statusline", provider="claude",
        payload={"week_start_date": "2026-01-01", "weekly_percent": 1.0,
                 "source": "statusline", "payload_json": "{}"},
    )


def test_harvest_early_out_when_pipeline_writes_nothing(ns, monkeypatch):
    jr = _jr()
    J = _jlib()

    def noop_hook(ctx, rec):
        return None
    jr.PIPELINE.append(noop_hook)

    harvest_calls = []
    real_harvest = jr._harvest
    monkeypatch.setattr(
        jr, "_harvest",
        lambda ctx: harvest_calls.append(1) or real_harvest(ctx),
    )

    jr.append_record(_obs(J), now_utc=FIXED)
    res = jr.run_stats_ingest(mode="authoritative")
    assert res.ran is True
    assert res.consumed == 1
    assert harvest_calls == [], "harvest must be skipped when pipeline wrote nothing"


def test_harvest_runs_and_stamps_when_pipeline_inserts_natural_keyed(ns, monkeypatch):
    jr = _jr()
    J = _jlib()

    def reset_hook(ctx, rec):
        if rec.get("t") != "obs":
            return
        # Insert a natural-keyed row with journal_id NULL — the harvest family.
        ctx.conn.execute(
            "INSERT OR IGNORE INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
            (rec["at"], "2026-01-01T00:00:00+00:00",
             "2026-01-08T00:00:00+00:00", "2026-01-04T00:00:00+00:00"),
        )
    jr.PIPELINE.append(reset_hook)

    harvest_calls = []
    real_harvest = jr._harvest
    monkeypatch.setattr(
        jr, "_harvest",
        lambda ctx: harvest_calls.append(1) or real_harvest(ctx),
    )

    jr.append_record(_obs(J), now_utc=FIXED)
    res = jr.run_stats_ingest(mode="authoritative")
    assert res.ran is True
    assert harvest_calls == [1], "harvest must run when the pipeline inserted a row"
    assert res.events_emitted == 1, "one week_reset harvest evt journaled"

    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT journal_id FROM week_reset_events LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] is not None, "harvested row must be stamped"
    assert row[0].startswith("wr:")


# ==========================================================================
# Design A — insert_cost_snapshot(journal=(ctx, id_base)) routes through
# emit_model_a: the computed cost rides in the journaled columns; the default
# (journal=None) bare insert NEVER appends an evt; fold-replay reads the cost
# back verbatim (never recomputes from provider JSONL).
# ==========================================================================

def _decode_last_journal_line(jr, J):
    seg = jr.list_segments()[-1]
    path = __import__("_cctally_core").JOURNAL_DIR / seg
    raw = path.read_bytes().splitlines()[-1]
    return J.decode_line(raw)


def _all_journal_lines(jr, J):
    """Every decoded journal line across all segments in canonical order."""
    import _cctally_core
    out = []
    for seg in jr.list_segments():
        path = _cctally_core.JOURNAL_DIR / seg
        for raw in path.read_bytes().splitlines():
            d = J.decode_line(raw)
            if d is not None:
                out.append(d)
    return out


def test_cost_snapshot_journal_none_appends_no_evt(ns):
    jr = _jr()
    conn = ns["open_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rowid = ns["insert_cost_snapshot"](
            conn, dt.date(2026, 1, 1), dt.date(2026, 1, 8),
            "2026-01-01T00:00:00+00:00", "2026-01-08T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00", "2026-01-04T12:00:00+00:00",
            12.5, "auto", None, commit=False,
        )
        conn.commit()
    finally:
        conn.close()
    assert isinstance(rowid, int) and rowid > 0
    # The default path must never touch the journal.
    assert jr.list_segments() == [], "bare insert must NOT append an evt line"


def test_cost_snapshot_journal_routes_through_model_a(ns):
    jr = _jr()
    J = _jlib()
    ctx_conn = ns["open_db"]()
    try:
        ctx = jr.IngestContext(conn=ctx_conn, batch=[])
        ctx_conn.execute("BEGIN IMMEDIATE")
        rowid = ns["insert_cost_snapshot"](
            ctx_conn, dt.date(2026, 1, 1), dt.date(2026, 1, 8),
            "2026-01-01T00:00:00+00:00", "2026-01-08T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00", "2026-01-04T12:00:00+00:00",
            7.25, "auto", None, as_of=_AS_OF, journal=(ctx, "o:deadbeef"),
        )
        ctx_conn.commit()
        row = ctx_conn.execute(
            "SELECT id, journal_id, cost_usd, captured_at_utc "
            "FROM weekly_cost_snapshots WHERE id = ?", (rowid,)
        ).fetchone()
    finally:
        ctx_conn.close()
    assert row is not None
    assert int(row[0]) == rowid
    assert row[1] == "wcs:o:deadbeef:2026-01-01"
    assert float(row[2]) == 7.25
    assert row[3] == _AS_OF, "as_of must stamp captured_at_utc"
    assert ctx.events_emitted == 1

    # The evt line carries the computed cost verbatim.
    evt = _decode_last_journal_line(jr, J)
    assert evt["t"] == "evt"
    assert evt["payload"]["kind"] == "weekly_cost_snapshot"
    assert float(evt["payload"]["cost_usd"]) == 7.25

    # Fold-replay into a FRESH DB reproduces the row from the journal alone —
    # the cost is read back, never recomputed.
    import _cctally_core
    fresh = _cctally_core.open_db()
    try:
        jr._apply_evt(fresh, evt)
        fresh.commit()
        got = fresh.execute(
            "SELECT cost_usd, journal_id FROM weekly_cost_snapshots "
            "WHERE journal_id = ?", ("wcs:o:deadbeef:2026-01-01",)
        ).fetchone()
    finally:
        fresh.close()
    assert got is not None and float(got[0]) == 7.25


# ==========================================================================
# Design B — reset/credit suppression. The harvest attaches the per-cycle
# suppression list (captured by the pipeline hook BEFORE the DELETE) to the
# wr/fhc evt payload; the bespoke event+effects fold applier inserts the reset
# row THEN replays the destructive stale-replica DELETE (idempotent).
# ==========================================================================

def test_harvest_attaches_suppression_list_to_reset_evt(ns):
    jr = _jr()
    J = _jlib()
    conn = ns["open_db"]()
    try:
        # A pipeline-like sequence: a week_reset row inserted this cycle, plus a
        # suppression map keyed on its natural-key parts.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
            ("2026-01-04T12:00:00Z", "2026-01-04T09:00:00+00:00",
             "2026-01-08T00:00:00+00:00", "2026-01-04T09:00:00+00:00"),
        )
        ctx = jr.IngestContext(conn=conn, batch=[])
        # #341: the wr harvest id_parts (and thus the suppression_map key) now
        # lead with account_key — the seeded row defaults to the sentinel.
        ctx.suppression_map[
            ("unattributed", "2026-01-04T09:00:00+00:00",
             "2026-01-08T00:00:00+00:00")
        ] = ["b:weekly_usage_snapshots:5", "b:weekly_usage_snapshots:6"]
        jr._harvest(ctx)
        conn.commit()
    finally:
        conn.close()
    evt = _decode_last_journal_line(jr, J)
    assert evt["payload"]["kind"] == "week_reset"
    assert evt["payload"]["suppression"] == [
        "b:weekly_usage_snapshots:5", "b:weekly_usage_snapshots:6"
    ]
    # id stays the pure natural key — suppression is NOT an id component. #341:
    # the natural key now leads with account_key (unstamped row -> sentinel).
    assert evt["id"] == (
        "wr:unattributed:2026-01-04T09:00:00+00:00:2026-01-08T00:00:00+00:00")


def test_reset_fold_applier_inserts_row_and_replays_suppression(ns):
    jr = _jr()
    J = _jlib()
    conn = ns["open_db"]()
    try:
        # Seed a stale-replica snapshot the suppression will delete.
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T09:30:00Z",
            week_start_date="2026-01-01", weekly_percent=94.0,
        )
        conn.execute(
            "UPDATE weekly_usage_snapshots SET journal_id = ? WHERE weekly_percent = 94.0",
            ("b:weekly_usage_snapshots:5",),
        )
        conn.commit()
        evt = J.make_evt(
            kind="week_reset",
            id="wr:2026-01-04T09:00:00+00:00:2026-01-08T00:00:00+00:00",
            at="2026-01-04T12:00:00Z",
            payload={
                "kind": "week_reset",
                "detected_at_utc": "2026-01-04T12:00:00Z",
                "old_week_end_at": "2026-01-04T09:00:00+00:00",
                "new_week_end_at": "2026-01-08T00:00:00+00:00",
                "effective_reset_at_utc": "2026-01-04T09:00:00+00:00",
                "suppression": ["b:weekly_usage_snapshots:5"],
            },
        )
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_evt(conn, evt)  # bespoke event+effects applier
        conn.commit()
        reset_row = conn.execute(
            "SELECT journal_id FROM week_reset_events WHERE journal_id = ?",
            (evt["id"],),
        ).fetchone()
        supp_row = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE journal_id = ?",
            ("b:weekly_usage_snapshots:5",),
        ).fetchone()
        # Re-apply: idempotent — the row is already inserted and the suppressed
        # id is already absent (a clean no-op).
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_evt(conn, evt)
        conn.commit()
        count_after = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events WHERE journal_id = ?",
            (evt["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert reset_row is not None, "reset row inserted by the event+effects applier"
    assert int(supp_row[0]) == 0, "suppressed snapshot deleted by the effect"
    assert int(count_after[0]) == 1, "re-apply is idempotent (no duplicate reset row)"


# ==========================================================================
# Design C — run_stats_ingest(reconcile_config=...) runs the live-only budget
# reconcile INSIDE the cycle txn after the pipeline and BEFORE harvest, so a
# newly-latched crossing row is journaled by the budget harvest.
# ==========================================================================

def test_reconcile_config_latched_crossing_journaled_via_harvest(ns, monkeypatch):
    jr = _jr()
    J = _jlib()
    seen = {}

    def fake_budget_reconcile(vb, *, conn=None, as_of=None):
        seen["conn"] = conn
        conn.execute(
            "INSERT OR IGNORE INTO budget_milestones "
            "(vendor, period_start_at, period, threshold, budget_usd, "
            " spent_usd, consumption_pct, crossed_at_utc) "
            "VALUES ('claude', '2026-01-01T00:00:00+00:00', 'subscription-week', "
            "        50, 10.0, 6.0, 60.0, '2026-01-04T12:00:00Z')"
        )
    monkeypatch.setitem(ns, "_reconcile_budget_on_config_write", fake_budget_reconcile)
    monkeypatch.setitem(ns, "_reconcile_codex_budget_on_config_write",
                        lambda *a, **k: None)
    monkeypatch.setitem(ns, "_reconcile_project_budget_milestones_on_write",
                        lambda *a, **k: None)

    jr.append_record(_obs(J), now_utc=FIXED)
    res = jr.run_stats_ingest(
        mode="authoritative",
        reconcile_config={"budget": {"weekly_usd": 50.0}, "touched_projects": None},
    )
    assert res.ran is True
    assert seen.get("conn") is not None, "reconcile ran on the cycle connection"

    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT journal_id FROM budget_milestones LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] is not None, "latched crossing stamped"
    assert row[0].startswith("bm:"), "latched crossing harvested as a budget evt"


def test_reconcile_config_none_runs_no_reconcile(ns, monkeypatch):
    jr = _jr()
    J = _jlib()
    called = []
    monkeypatch.setitem(ns, "_reconcile_budget_on_config_write",
                        lambda *a, **k: called.append(1))
    jr.append_record(_obs(J), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")
    assert called == [], "no reconcile without reconcile_config"


def test_reconcile_config_threads_touched_projects(ns, monkeypatch):
    """6c widening: `reconcile_config["touched_projects"]` threads into the
    per-project reconcile as its 2nd positional, so a scoped `budget
    set/unset --project` write reconciles ONLY the touched project (and never
    latches a sibling's crossed-but-undispatched threshold)."""
    jr = _jr()
    J = _jlib()
    seen = {}

    def fake_project_reconcile(vb, touched=None, *, conn=None, as_of=None):
        seen["vb"] = vb
        seen["touched"] = touched
        seen["conn"] = conn

    monkeypatch.setitem(ns, "_reconcile_budget_on_config_write",
                        lambda *a, **k: None)
    monkeypatch.setitem(ns, "_reconcile_codex_budget_on_config_write",
                        lambda *a, **k: None)
    monkeypatch.setitem(ns, "_reconcile_project_budget_milestones_on_write",
                        fake_project_reconcile)

    jr.append_record(_obs(J), now_utc=FIXED)
    jr.run_stats_ingest(
        mode="authoritative",
        reconcile_config={
            "budget": {"weekly_usd": 50.0,
                       "projects": {"/proj/a": 10.0}},
            "touched_projects": {"/proj/a"},
        },
    )
    assert seen.get("touched") == {"/proj/a"}, (
        "scoped touched_projects must thread through to the per-project reconcile"
    )
    assert seen.get("conn") is not None, "reconcile ran on the cycle connection"
    assert seen.get("vb", {}).get("weekly_usd") == 50.0, "budget forwarded"


# ==========================================================================
# Exception discipline (6b-gate P2) — a pipeline-hook exception ABORTS the
# cycle: opportunistic logs + returns ran=True/error, cursor unmoved, next
# cycle converges; authoritative propagates.
# ==========================================================================

def _boom_hook(ctx, rec):
    if rec.get("t") == "obs":
        raise RuntimeError("hook boom")


def test_cycle_abort_opportunistic_returns_error_cursor_unmoved(ns):
    jr = _jr()
    J = _jlib()
    jr.PIPELINE.append(_boom_hook)
    try:
        jr.append_record(_obs(J), now_utc=FIXED)
        res = jr.run_stats_ingest(mode="opportunistic")
        assert res.ran is True
        assert isinstance(res.error, RuntimeError)

        conn = ns["open_db"]()
        try:
            assert jr._read_cursor(conn) is None, "cursor must not advance on abort"
        finally:
            conn.close()

        # Remove the failing hook → the next cycle converges over the same line.
        jr.PIPELINE.remove(_boom_hook)
        res2 = jr.run_stats_ingest(mode="opportunistic")
        assert res2.ran is True and res2.error is None and res2.consumed == 1
        conn = ns["open_db"]()
        try:
            assert jr._read_cursor(conn) is not None, (
                "cursor advances on the clean cycle")
        finally:
            conn.close()
    finally:
        # PIPELINE is a module-level list; a mid-test failure before the inline
        # remove above would otherwise leak _boom_hook into sibling tests
        # (6c P3 tidy-up).
        if _boom_hook in jr.PIPELINE:
            jr.PIPELINE.remove(_boom_hook)


def test_cycle_abort_authoritative_propagates(ns):
    jr = _jr()
    J = _jlib()
    jr.PIPELINE.append(_boom_hook)
    try:
        jr.append_record(_obs(J), now_utc=FIXED)
        with pytest.raises(RuntimeError):
            jr.run_stats_ingest(mode="authoritative")
    finally:
        # pytest.raises leaves the hook installed; restore PIPELINE so it never
        # leaks into a sibling test (6c P3 tidy-up).
        if _boom_hook in jr.PIPELINE:
            jr.PIPELINE.remove(_boom_hook)


# ==========================================================================
# 6e obs/op pipeline hooks — the composition of the reviewed machinery. These
# drive the hooks through a real `run_stats_ingest` cycle from synthetic
# obs/op journal lines (the CLI call-site reroute is exercised separately by
# the harness net). `_pipeline_claude_usage` / `_pipeline_record_credit` /
# `_pipeline_sync_week` are registered into PIPELINE at module load.
# ==========================================================================

_WEEK_END_EPOCH = int(dt.datetime(2026, 1, 8, tzinfo=dt.timezone.utc).timestamp())


def _claude_obs(J, *, at, weekly_percent, resets_at=_WEEK_END_EPOCH,
                src="record-usage", five_hour_percent=None,
                five_hour_resets_at=None, source="statusline"):
    payload = {"weekly_percent": weekly_percent, "resets_at": resets_at,
               "source": source}
    if five_hour_percent is not None:
        payload["five_hour_percent"] = five_hour_percent
    if five_hour_resets_at is not None:
        payload["five_hour_resets_at"] = five_hour_resets_at
    return J.make_obs(at=at, src=src, provider="claude", payload=payload)


# (a) record-usage end-to-end via the obs hook: an accepted obs materializes a
# snapshot_accept-keyed row + a crossing percent_milestone + the pm evt with
# logical FK refs (usage_snapshot_ref = the sa id, cost_snapshot_ref = the wcs
# id, reset_event_ref = "0").
def test_obs_hook_accept_snapshot_milestone_and_logical_refs(ns):
    jr = _jr()
    J = _jlib()
    obs = _claude_obs(J, at="2026-01-04T09:00:00Z", weekly_percent=5.0)
    jr.append_record(obs, now_utc=FIXED)
    res = jr.run_stats_ingest(mode="authoritative")
    assert res.ran is True and res.consumed == 1

    conn = ns["open_db"]()
    try:
        snap = conn.execute(
            "SELECT id, journal_id, weekly_percent, week_start_date "
            "FROM weekly_usage_snapshots"
        ).fetchall()
        pm = conn.execute(
            "SELECT percent_threshold, reset_event_id, journal_id, "
            " usage_snapshot_id, cost_snapshot_id FROM percent_milestones"
        ).fetchall()
        wcs = conn.execute(
            "SELECT journal_id FROM weekly_cost_snapshots"
        ).fetchall()
    finally:
        conn.close()

    assert len(snap) == 1
    assert snap[0][1] == f"sa:{obs['id']}", "snapshot row keyed by snapshot_accept id"
    assert float(snap[0][2]) == 5.0 and snap[0][3] == "2026-01-01"
    assert len(pm) == 1
    assert pm[0][0] == 5 and pm[0][1] == 0, "milestone 5, pre-credit segment"
    assert pm[0][2].startswith("pm:"), "milestone harvested + stamped"
    assert len(wcs) == 1 and wcs[0][0].startswith("wcs:"), (
        "milestone cost sync journaled a weekly_cost_snapshot evt")

    lines = _all_journal_lines(jr, J)
    pm_evt = [e for e in lines
              if e.get("payload", {}).get("kind") == "percent_milestone"]
    assert len(pm_evt) == 1
    p = pm_evt[0]["payload"]
    assert p["usage_snapshot_ref"] == f"sa:{obs['id']}", "logical FK to the snapshot"
    assert p["cost_snapshot_ref"] == wcs[0][0], "logical FK to the cost snapshot"
    assert p["reset_event_ref"] == "0", "no-reset sentinel"


# (c) a dedup tick appends an obs but writes NO second snapshot, yet still runs
# the dollar axes (spec §4.5).
def test_obs_hook_dedup_skip_runs_dollar_axes_no_second_snapshot(ns, monkeypatch):
    jr = _jr()
    J = _jlib()
    dollar_calls = []
    monkeypatch.setitem(
        ns, "maybe_record_budget_milestone",
        lambda saved, **k: dollar_calls.append(saved.get("weeklyPercent")),
    )

    jr.append_record(_claude_obs(J, at="2026-01-04T09:00:00Z", weekly_percent=3.0),
                     now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")
    # second identical tick -> dedup skip. (consumed > 1: cycle 1's emitted evts
    # land past its own HW, so this cycle idempotently REPLAYS them + processes
    # the new obs — the journal-first replay contract, spec §5.2 step 4a.)
    jr.append_record(_claude_obs(J, at="2026-01-04T09:05:00Z", weekly_percent=3.0),
                     now_utc=FIXED)
    res2 = jr.run_stats_ingest(mode="authoritative")
    assert res2.ran is True

    conn = ns["open_db"]()
    try:
        n = conn.execute("SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()[0]
    finally:
        conn.close()
    assert n == 1, "dedup tick appended an obs but wrote no second snapshot"
    # The budget axis fired on BOTH the accept tick and the dedup-skip tick.
    assert dollar_calls == [3.0, 3.0], "dollar axes run on the dedup-skip tick too"


# ==========================================================================
# #769 S11 (#824) — the held write path, end to end through the obs hook.
#
# The acceptance sequence from the specification: t0 records a genuine weekly
# 63 with five-hour 20, then t1 arrives in the SAME physical weekly and
# five-hour windows carrying a raw weekly of 60, which clamps, and a genuine
# five-hour rise to 25. Before this change the tick wrote no row at all, so the
# 25 was lost from durable state entirely.
# ==========================================================================

#: One physical five-hour window, shared by both ticks of the held sequence.
_HELD_5H_RESETS_AT = "2026-01-04T13:00:00+00:00"


def _held_sequence(jr, J, *, second_five_hour_percent=25.0,
                   second_five_hour_resets_at=_HELD_5H_RESETS_AT):
    """Drive the two-tick held sequence through real ingest cycles."""
    jr.append_record(
        _claude_obs(J, at="2026-01-04T09:00:00Z", weekly_percent=63.0,
                    five_hour_percent=20.0,
                    five_hour_resets_at=_HELD_5H_RESETS_AT),
        now_utc=FIXED)
    assert jr.run_stats_ingest(mode="authoritative").ran is True
    jr.append_record(
        _claude_obs(J, at="2026-01-04T09:05:00Z", weekly_percent=60.0,
                    five_hour_percent=second_five_hour_percent,
                    five_hour_resets_at=second_five_hour_resets_at),
        now_utc=FIXED)
    assert jr.run_stats_ingest(mode="authoritative").ran is True


def _latest_usage_row(conn):
    return conn.execute(
        "SELECT * FROM weekly_usage_snapshots "
        "ORDER BY captured_at_utc DESC, id DESC LIMIT 1").fetchone()


def test_held_tick_persists_five_hour_and_leaves_weekly_untouched(ns):
    jr = _jr()
    J = _jlib()
    _held_sequence(jr, J)

    conn = ns["open_db"]()
    try:
        latest = _latest_usage_row(conn)
        rows = conn.execute(
            "SELECT weekly_percent, five_hour_percent, weekly_observation_held "
            "FROM weekly_usage_snapshots ORDER BY id").fetchall()
    finally:
        conn.close()

    assert len(rows) == 2, (
        "the weekly-clamped tick wrote no row, so its five-hour reading is gone")
    assert latest["weekly_percent"] == 63.0, (
        "the held row carries the effective weekly value, not the raw 60")
    assert latest["five_hour_percent"] == 25.0
    assert latest["weekly_observation_held"] == 1
    assert latest["captured_at_utc"] == "2026-01-04T09:05:00Z", (
        "the capture time is the TICK's, because the five-hour evidence is")
    assert rows[0][2] == 0, "the genuine t0 row is not held"

    app_dir = pathlib.Path(str(ns["APP_DIR"]))
    assert app_dir.joinpath("hwm-7d").read_text().split() == [
        "2026-01-01", "63.0"]
    assert app_dir.joinpath("hwm-5h").read_text().split()[1] == "25.0"


def test_the_held_row_retains_the_raw_weekly_reading_in_its_payload(ns):
    """The stored weekly value is the carried-forward 63, so the tick's own 60
    exists nowhere in the row's columns. It is retained in `payload_json` for
    audit and for `db rederive`, which reruns the raw observation."""
    import json as _json

    jr = _jr()
    J = _jlib()
    _held_sequence(jr, J)

    conn = ns["open_db"]()
    try:
        latest = _latest_usage_row(conn)
    finally:
        conn.close()
    payload = _json.loads(latest["payload_json"])
    assert payload["weeklyPercent"] == 63.0, (
        "the column and the payload's own weekly value must agree")
    assert payload["rawWeeklyPercent"] == 60.0
    assert payload["weeklyObservationHeld"] is True


def test_the_held_row_copies_its_boundary_from_the_non_held_basis(ns):
    """Review finding 7. A held row whose boundary came from the tick could
    reclassify a later reset as an in-place credit, because the reset detector
    compares the incoming boundary against the latest stored one."""
    jr = _jr()
    J = _jlib()
    _held_sequence(jr, J)

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT week_start_date, week_end_date, week_start_at, week_end_at "
            "FROM weekly_usage_snapshots ORDER BY id").fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    assert tuple(rows[1]) == tuple(rows[0]), (
        "the held row's four boundary columns must equal the basis's")


def test_a_held_tick_never_advances_the_weekly_high_water_file(ns):
    """Structural, not defensive. The monotonic `hwm_file_next` guard happens
    to suppress the weekly write here because 60 is below 63, but if `hwm-7d`
    ever trailed the stored value the HELD weekly 63 would be written into the
    file the status line renders from. The weekly writer is simply not called
    on a held tick, so the guard is not what is being relied on."""
    jr = _jr()
    J = _jlib()
    app_dir = pathlib.Path(str(ns["APP_DIR"]))
    app_dir.mkdir(parents=True, exist_ok=True)

    jr.append_record(
        _claude_obs(J, at="2026-01-04T09:00:00Z", weekly_percent=63.0,
                    five_hour_percent=20.0,
                    five_hour_resets_at=_HELD_5H_RESETS_AT),
        now_utc=FIXED)
    assert jr.run_stats_ingest(mode="authoritative").ran is True

    # Make the projection file TRAIL the stored value, which is the state the
    # monotonic guard cannot save us from.
    app_dir.joinpath("hwm-7d").write_text("2026-01-01 5.0\n")

    jr.append_record(
        _claude_obs(J, at="2026-01-04T09:05:00Z", weekly_percent=60.0,
                    five_hour_percent=25.0,
                    five_hour_resets_at=_HELD_5H_RESETS_AT),
        now_utc=FIXED)
    assert jr.run_stats_ingest(mode="authoritative").ran is True

    assert app_dir.joinpath("hwm-7d").read_text().split() == ["2026-01-01", "5.0"], (
        "the held weekly value reached hwm-7d")
    assert app_dir.joinpath("hwm-5h").read_text().split()[1] == "25.0", (
        "the five-hour writer must still run on a held tick")


def test_a_held_tick_derives_no_weekly_milestone(ns, monkeypatch):
    """Weekly milestone suppression on a clamp is preserved. A weekly milestone
    derived from the stale stored row fabricated a 13% milestone on 2026-09-01,
    and milestones are forward-only within an epoch, so that row forecloses
    every genuine crossing below it."""
    jr = _jr()
    J = _jlib()
    weekly_calls = []
    real = ns["maybe_record_milestone"]
    monkeypatch.setitem(
        ns, "maybe_record_milestone",
        lambda saved, **k: (weekly_calls.append(saved.get("weeklyPercent")),
                            real(saved, **k))[1])

    _held_sequence(jr, J)
    assert weekly_calls == [63.0], (
        "the weekly milestone chokepoint ran on the held tick")


def test_a_held_tick_with_no_five_hour_change_still_writes_nothing(ns):
    """Non-vacuity for the write. The held row exists because the five-hour
    axis carried evidence; an identical five-hour reading must still write no
    row, or every clamped tick would grow the table."""
    jr = _jr()
    J = _jlib()
    _held_sequence(jr, J, second_five_hour_percent=20.0)

    conn = ns["open_db"]()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# ==========================================================================
# #769 S11 (#824) — one `weeklyPercent` used to serve three distinct concepts:
# the block's weekly start, the block's weekly end, and a five-hour milestone's
# crossing metadata. On a held tick they are NOT the same number. The block
# takes the EFFECTIVE weekly value, because a block whose weekly start and end
# straddle a raw and an effective reading reports a weekly delta nobody
# observed (review finding 6). The crossing takes the RAW reading, because it
# is contemporaneous observation metadata.
# ==========================================================================

#: A SECOND physical five-hour window, so the held tick opens a new block whose
#: weekly start it writes itself.
_HELD_5H_RESETS_AT_NEXT = "2026-01-04T18:00:00+00:00"


def _latest_five_hour_block(conn):
    return conn.execute(
        "SELECT * FROM five_hour_blocks ORDER BY id DESC LIMIT 1").fetchone()


def _latest_five_hour_milestone(conn):
    return conn.execute(
        "SELECT * FROM five_hour_milestones ORDER BY id DESC LIMIT 1").fetchone()


def test_held_tick_block_uses_effective_weekly_and_crossing_uses_raw(ns):
    jr = _jr()
    J = _jlib()
    _held_sequence(
        jr, J, second_five_hour_percent=25.0,
        second_five_hour_resets_at=_HELD_5H_RESETS_AT_NEXT)

    conn = ns["open_db"]()
    try:
        block = _latest_five_hour_block(conn)
        milestone = _latest_five_hour_milestone(conn)
        block_count = conn.execute(
            "SELECT COUNT(*) FROM five_hour_blocks").fetchone()[0]
    finally:
        conn.close()

    assert block_count == 2, "the held tick opened a block for the new window"
    assert block["final_five_hour_percent"] == 25.0
    assert block["seven_day_pct_at_block_start"] == 63.0
    assert block["seven_day_pct_at_block_end"] == 63.0
    assert milestone["percent_threshold"] == 25
    assert milestone["seven_day_pct_at_crossing"] == 60.0


def test_a_held_rollover_heal_also_uses_the_effective_weekly_value(ns):
    """The no-row-written half of the same rule. When the five-hour value is
    unchanged but the window rolled over, the tick writes no row and step 4'
    materializes the block alone — and that path had the raw reading wired
    straight into the block's weekly start."""
    jr = _jr()
    J = _jlib()
    _held_sequence(
        jr, J, second_five_hour_percent=20.0,
        second_five_hour_resets_at=_HELD_5H_RESETS_AT_NEXT)

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()[0]
        block = _latest_five_hour_block(conn)
        block_count = conn.execute(
            "SELECT COUNT(*) FROM five_hour_blocks").fetchone()[0]
    finally:
        conn.close()

    assert rows == 1, "an unchanged five-hour value writes no row"
    assert block_count == 2, "the rollover heal still anchored the new window"
    assert block["seven_day_pct_at_block_start"] == 63.0, (
        "the block-only heal fabricated a weekly delta from the raw reading")
    assert block["seven_day_pct_at_block_end"] == 63.0


def test_an_ordinary_tick_binds_both_concepts_to_the_same_reading(ns):
    """Non-vacuity in the other direction. On a tick whose weekly axis is
    observed, the raw and effective values ARE the same number, and the split
    must not change what such a tick records."""
    jr = _jr()
    J = _jlib()
    jr.append_record(
        _claude_obs(J, at="2026-01-04T09:00:00Z", weekly_percent=63.0,
                    five_hour_percent=25.0,
                    five_hour_resets_at=_HELD_5H_RESETS_AT),
        now_utc=FIXED)
    assert jr.run_stats_ingest(mode="authoritative").ran is True

    conn = ns["open_db"]()
    try:
        block = _latest_five_hour_block(conn)
        milestone = _latest_five_hour_milestone(conn)
    finally:
        conn.close()
    assert block["seven_day_pct_at_block_start"] == 63.0
    assert block["seven_day_pct_at_block_end"] == 63.0
    assert milestone["seven_day_pct_at_crossing"] == 63.0


def test_the_builder_names_the_two_concepts_separately(ns):
    """A unit assertion on the builder itself, because the whole point of it is
    that a caller cannot pick the two values by hand. Given one fold result it
    returns both, and they differ exactly when the weekly axis is held."""
    jr = _jr()
    conn = ns["open_db"]()
    try:
        _seed_snapshot(
            conn, captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date="2026-01-01", weekly_percent=63.0,
            five_hour_percent=10.0, five_hour_window_key=7772)
        held = jr._usage_snapshot_fold_decision(
            conn, _payload(60.0, five_hour_percent=25.0,
                           five_hour_window_key=7772))
        observed = jr._usage_snapshot_fold_decision(
            conn, _payload(70.0, five_hour_percent=25.0,
                           five_hour_window_key=7772))
    finally:
        conn.close()

    build = ns["_five_hour_saved_from_fold"]
    held_saved = build(held, snapshot_id=7, capture_at="2026-01-04T09:05:00Z",
                       five_hour_resets_at="2026-01-04T13:00:00+00:00")
    assert held_saved["blockWeeklyPercent"] == 63.0
    assert held_saved["sevenDayPercentAtCrossing"] == 60.0
    assert held_saved["fiveHourPercent"] == 25.0
    assert held_saved["fiveHourWindowKey"] == 7772
    assert held_saved["fiveHourResetsAt"] == "2026-01-04T13:00:00+00:00"
    assert held_saved["id"] == 7
    assert held_saved["capturedAt"] == "2026-01-04T09:05:00Z"

    observed_saved = build(
        observed, snapshot_id=8, capture_at="2026-01-04T09:05:00Z",
        five_hour_resets_at="2026-01-04T13:00:00+00:00")
    assert observed_saved["blockWeeklyPercent"] == 70.0
    assert observed_saved["sevenDayPercentAtCrossing"] == 70.0


# (g)/(h) live derivation dispatches a crossed alert exactly once, from the
# post-commit sink; replaying the journal never dispatches (apply-only).
def test_live_dispatches_once_replay_never(ns, monkeypatch):
    jr = _jr()
    J = _jlib()
    dispatched = []
    monkeypatch.setitem(ns, "_dispatch_alert_notification",
                        lambda p, **k: dispatched.append(p))
    monkeypatch.setitem(
        ns, "load_config",
        lambda *a, **k: {"alerts": {"enabled": True, "weekly_thresholds": [5]}},
    )
    obs = _claude_obs(J, at="2026-01-04T09:00:00Z", weekly_percent=5.0)
    jr.append_record(obs, now_utc=FIXED)
    res = jr.run_stats_ingest(mode="authoritative")
    assert len(res.alerts) == 1, "one crossing alert dispatched post-commit"
    assert len(dispatched) == 1

    # Replay every journal evt into a FRESH DB — apply-only, no ctx, no sink.
    import _cctally_core
    fresh = _cctally_core.open_db()
    try:
        fresh.execute("BEGIN IMMEDIATE")
        for line in _all_journal_lines(jr, J):
            if line.get("t") == "evt":
                jr._apply_evt(fresh, line)
        fresh.commit()
    finally:
        fresh.close()
    assert len(dispatched) == 1, "replay must never dispatch an alert"


# (b) record-credit end-to-end via the op hook + fold-replay: the op fold owns
# the floor row (journal_id = op id, Option (i)); _apply_credit journals the
# wce (suppression) + synthetic snapshot_accept and skips its own floor INSERT;
# a fresh-DB replay reproduces the floor + synthetic + suppression.
def _credit_op(J, op_at="2026-01-04T09:00:05Z", *, from_pct=60.0, to_pct=40.0,
               plan_effective="2026-01-04T08:00:00+00:00",
               op_effective="2026-01-04T09:00:00+00:00", forced=False):
    # P3 hardening: `plan.effective_iso` (what `_apply_credit` uses for the wce
    # suppression predicate + synthetic) is DIVERGED from the op's top-level
    # `effective_at_utc` (what the op fold stamps on the floor row) — proving the
    # op fold owns the floor (Option (i)) so exactly ONE floor row lands with a
    # non-NULL journal_id even when the two effectives differ.
    plan = {
        "week_start_date": "2026-01-01",
        "week_start_at": "2026-01-01T00:00:00+00:00",
        "week_end_at": "2026-01-07T23:59:59+00:00",
        "cur_end_canon": "2026-01-07T23:59:59+00:00",
        "from_pct": from_pct,
        "from_source": "hwm",
        "to_pct": to_pct,
        "effective_iso": plan_effective,
        "captured_iso": op_at,
    }
    return J.make_op(at=op_at, src="record-credit", payload={
        "kind": "weekly_credit_floor",
        "week_start_date": "2026-01-01",
        "effective_at_utc": op_effective,
        "observed_pre_credit_pct": from_pct,
        "applied_at_utc": op_at,
        "plan": plan,
        "five_hour": [None, None, None],
        "forced": forced,
    })


def _seed_doomed_pre_credit(conn):
    _seed_snapshot(conn, captured_at_utc="2026-01-04T09:00:03Z",
                   week_start_date="2026-01-01", weekly_percent=60.0,
                   week_end_at="2026-01-07T23:59:59+00:00")
    conn.execute(
        "UPDATE weekly_usage_snapshots SET journal_id = 'sa:pre' "
        "WHERE weekly_percent = 60.0")
    conn.commit()


def test_record_credit_op_hook_end_to_end_and_replay(ns):
    jr = _jr()
    J = _jlib()

    conn = ns["open_db"]()
    try:
        _seed_doomed_pre_credit(conn)
    finally:
        conn.close()

    op = _credit_op(J)
    jr.append_record(op, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    conn = ns["open_db"]()
    try:
        floor = conn.execute(
            "SELECT week_start_date, effective_at_utc, observed_pre_credit_pct, "
            " journal_id FROM weekly_credit_floors").fetchall()
        doomed = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:pre'").fetchone()[0]
        syn = conn.execute(
            "SELECT weekly_percent, source, journal_id FROM weekly_usage_snapshots "
            "WHERE journal_id = ?", (f"sa:{op['id']}:syn:0",)).fetchall()
    finally:
        conn.close()

    # Option (i) proof: exactly ONE floor row, journal_id == the op line id
    # (written by the built-in op fold, NOT by _apply_credit).
    assert len(floor) == 1
    assert floor[0][3] == op["id"], "floor journal_id is the op line id (op fold owns it)"
    assert floor[0][0] == "2026-01-01" and float(floor[0][2]) == 60.0
    # Destructive effect applied: doomed pre-credit row gone; synthetic present.
    assert doomed == 0, "stale-replica pre-credit snapshot suppressed"
    assert len(syn) == 1 and float(syn[0][0]) == 40.0 and syn[0][1] == "record-credit"

    lines = _all_journal_lines(jr, J)
    wce = [e for e in lines if e["id"] == f"wce:{op['id']}"]
    sa_syn = [e for e in lines if e["id"] == f"sa:{op['id']}:syn:0"]
    assert len(wce) == 1 and wce[0]["payload"]["suppression"] == ["sa:pre"]
    assert len(sa_syn) == 1

    # Fold-replay into a FRESH DB (seed the doomed row, then apply the op fold +
    # the evt lines in canonical order) reproduces floor + synthetic + suppression.
    import _cctally_core
    fresh = _cctally_core.open_db()
    try:
        _seed_doomed_pre_credit(fresh)
        fresh.execute("BEGIN IMMEDIATE")
        for line in lines:
            if line.get("t") == "op":
                jr._pipeline_op_fold(jr.IngestContext(conn=fresh, batch=[]), line)
            elif line.get("t") == "evt":
                jr._apply_evt(fresh, line)
        fresh.commit()
        f_floor = fresh.execute(
            "SELECT journal_id FROM weekly_credit_floors").fetchall()
        f_doomed = fresh.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:pre'").fetchone()[0]
        f_syn = fresh.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE journal_id = ?",
            (f"sa:{op['id']}:syn:0",)).fetchone()[0]
    finally:
        fresh.close()
    assert len(f_floor) == 1 and f_floor[0][0] == op["id"], "replay floor journal_id matches"
    assert f_doomed == 0, "replay re-applies the suppression"
    assert f_syn == 1, "replay reinserts the synthetic snapshot"


def test_force_clear_wce_suppression_pure_function_of_op_under_replay(ns):
    """6f P1 (6g): the ``--force`` re-record's ``wce`` suppression MUST be a pure
    function of the op — identical whether or not ``sa:<id_base>:syn:0`` has
    already been replayed. Under crash-replay (crash between evt fsync and
    COMMIT) the next cycle replays the NEW synthetic at fold order 10 BEFORE
    step 4b re-runs ``_apply_credit(forced=True)``; a timing-only exclusion would
    then re-capture that new synthetic into a SECOND ``wce`` whose suppression
    deletes the very row it must preserve when a rebuild folds all snapshot_accept
    before all wce. Clean week (no OLD synthetics / doomed replicas) so the ONLY
    thing that could pollute the suppression is the new synthetic's own id.
    """
    jr = _jr()
    J = _jlib()

    # Cycle 1: a forced record-credit over a clean week — emits sa:<op>:syn:0 +
    # wce:<op> (suppression == [] : nothing to suppress).
    op = _credit_op(J, forced=True)
    jr.append_record(op, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    lines1 = _all_journal_lines(jr, J)
    wce1 = [e for e in lines1 if e.get("id") == f"wce:{op['id']}"]
    assert len(wce1) == 1
    first_wce = wce1[0]["payload"]
    assert (first_wce.get("suppression") or []) == [], \
        "clean-state cycle-1 wce must suppress nothing"

    # Reconstruct the crash-replay condition: reset the cursor so the op line is
    # re-consumed while sa:<op>:syn:0 is already materialized (in a real crash it
    # is re-inserted by step-4a replay just before step-4b re-runs _apply_credit;
    # here it survived cycle 1's commit — the same "new synthetic present when the
    # old-synthetic query runs" condition).
    conn = ns["open_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE journal_id = ?",
            (f"sa:{op['id']}:syn:0",)).fetchone()[0] == 1
        conn.execute("DELETE FROM journal_cursor")
        conn.commit()
    finally:
        conn.close()

    jr.run_stats_ingest(mode="authoritative")  # the replay cycle

    lines2 = _all_journal_lines(jr, J)
    wce_all = [e for e in lines2 if e.get("id") == f"wce:{op['id']}"]
    assert len(wce_all) >= 2, "the replay cycle re-emitted a second wce line"
    second_wce = wce_all[-1]["payload"]

    # (a) pure function of the op: the second wce payload is byte-equal to the
    #     first — the new synthetic's own id is excluded from the capture.
    assert f"sa:{op['id']}:syn:0" not in (second_wce.get("suppression") or []), (
        "the re-run folded the new synthetic into its own wce suppression")
    assert second_wce == first_wce, (
        "wce suppression is NOT a pure function of the op: "
        f"{second_wce!r} != {first_wce!r}")

    # (b) adversarial rebuild fold — a FRESH index folding ALL snapshot_accept
    #     (@10) BEFORE ALL wce (@50). The new synthetic must SURVIVE.
    import _cctally_core
    import os
    db_path = _cctally_core.DB_PATH
    for p in (str(db_path), str(db_path) + "-wal", str(db_path) + "-shm"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    fresh = _cctally_core.open_db()
    try:
        evts = [e for e in lines2 if e.get("t") == "evt"]
        fresh.execute("BEGIN IMMEDIATE")
        for line in sorted(evts, key=jr._fold_order):
            jr._apply_evt(fresh, line)
        fresh.commit()
        syn = fresh.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE journal_id = ?",
            (f"sa:{op['id']}:syn:0",)).fetchone()[0]
    finally:
        fresh.close()
    assert syn == 1, (
        "adversarial all-snapshot_accept-before-all-wce fold deleted the new "
        "synthetic the rebuild must preserve")


def test_credit_wce_suppression_pure_function_of_op_sub1pp_replay(ns):
    """6g P2 (Task 7 Item 0): the NON-force ``doomed`` stale-replica capture in
    ``_apply_credit`` must also exclude the op's OWN synthetic id, so a sub-1.0pp
    credit's ``wce`` suppression is a pure function of the op under crash-replay.

    When the credit magnitude is < 1.0pp the synthetic post-credit snapshot (at
    ``to_pct``) falls INSIDE the doomed band ``ABS(weekly_percent - from_pct) <
    1.0``. Under crash-replay (evts fsync'd, COMMIT lost) the next cycle replays
    ``sa:<id>:syn:0`` at fold order 10 BEFORE step 4b re-runs ``_apply_credit``;
    without the ``NOT LIKE 'sa:<id>:syn:%'`` exclusion the re-capture folds the
    just-replayed synthetic into a SECOND ``wce`` whose suppression deletes it
    when a rebuild folds all snapshot_accept before all wce. This is the sibling
    of the ``--force`` old_syn fix, on the plain (non-force) ``supp = doomed``
    path — the existing from=60->to=40 band can't reach the synthetic.
    """
    jr = _jr()
    J = _jlib()

    # Cycle 1: a NON-forced sub-1.0pp credit (60 -> 59.5) over a CLEAN week — no
    # doomed pre-credit replicas, so wce1 suppression == [] (the synthetic does
    # not exist yet when doomed is captured).
    op = _credit_op(J, from_pct=60.0, to_pct=59.5, forced=False)
    jr.append_record(op, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    lines1 = _all_journal_lines(jr, J)
    wce1 = [e for e in lines1 if e.get("id") == f"wce:{op['id']}"]
    assert len(wce1) == 1
    first_wce = wce1[0]["payload"]
    assert (first_wce.get("suppression") or []) == [], \
        "clean-state cycle-1 wce must suppress nothing"

    # Guard the fixture: the synthetic must have landed INSIDE the doomed band
    # (|59.5 - 60| = 0.5 < 1.0), else the test would be vacuous.
    conn = ns["open_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE journal_id = ?",
            (f"sa:{op['id']}:syn:0",)).fetchone()[0] == 1
        conn.execute("DELETE FROM journal_cursor")
        conn.commit()
    finally:
        conn.close()

    jr.run_stats_ingest(mode="authoritative")  # the replay cycle

    lines2 = _all_journal_lines(jr, J)
    wce_all = [e for e in lines2 if e.get("id") == f"wce:{op['id']}"]
    assert len(wce_all) >= 2, "the replay cycle re-emitted a second wce line"
    second_wce = wce_all[-1]["payload"]

    # (a) pure function of the op: the second wce payload is byte-equal to the
    #     first — the new synthetic's own id is excluded from the capture.
    assert f"sa:{op['id']}:syn:0" not in (second_wce.get("suppression") or []), (
        "the re-run folded the new synthetic into its own wce suppression")
    assert second_wce == first_wce, (
        "wce suppression is NOT a pure function of the op: "
        f"{second_wce!r} != {first_wce!r}")

    # (b) adversarial rebuild fold — all snapshot_accept (@10) BEFORE all wce
    #     (@50). The new synthetic must SURVIVE.
    import _cctally_core
    import os
    db_path = _cctally_core.DB_PATH
    for p in (str(db_path), str(db_path) + "-wal", str(db_path) + "-shm"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    fresh = _cctally_core.open_db()
    try:
        evts = [e for e in lines2 if e.get("t") == "evt"]
        fresh.execute("BEGIN IMMEDIATE")
        for line in sorted(evts, key=jr._fold_order):
            jr._apply_evt(fresh, line)
        fresh.commit()
        syn = fresh.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE journal_id = ?",
            (f"sa:{op['id']}:syn:0",)).fetchone()[0]
    finally:
        fresh.close()
    assert syn == 1, (
        "adversarial all-snapshot_accept-before-all-wce fold deleted the new "
        "synthetic the rebuild must preserve")


# ==========================================================================
# 6f item 3 — axes gate on `_run_config_reconcile` (`reconcile_config["axes"]`
# runs ONLY the named axes) + the empty-journal reconcile-only cycle (a config
# write appends NO journal line, so the reconcile must still run) + the
# `reconcile_budget_config` opportunistic+wrapped router.
# ==========================================================================

def _wire_reconcile_spies(ns, monkeypatch):
    fired = {"budget": 0, "codex_budget": 0, "project_budget": 0}
    monkeypatch.setitem(
        ns, "_reconcile_budget_on_config_write",
        lambda vb, **k: fired.__setitem__("budget", fired["budget"] + 1))
    monkeypatch.setitem(
        ns, "_reconcile_codex_budget_on_config_write",
        lambda vb, **k: fired.__setitem__("codex_budget", fired["codex_budget"] + 1))
    monkeypatch.setitem(
        ns, "_reconcile_project_budget_milestones_on_write",
        lambda vb, touched=None, **k: fired.__setitem__(
            "project_budget", fired["project_budget"] + 1))
    return fired


def test_axes_gate_runs_only_named_axis(ns, monkeypatch):
    """`reconcile_config["axes"]={"budget"}` runs ONLY the budget reconcile —
    the codex + project axes stay untouched (a `budget set` write must not latch
    a Codex/project crossing it never touched). Runs on an EMPTY journal (no obs
    appended) — the reconcile-only cycle path (the config-write common case)."""
    jr = _jr()
    fired = _wire_reconcile_spies(ns, monkeypatch)
    res = jr.run_stats_ingest(
        mode="authoritative",
        reconcile_config={"budget": {"weekly_usd": 50.0}, "axes": {"budget"},
                          "touched_projects": None},
    )
    assert res.ran is True
    assert fired == {"budget": 1, "codex_budget": 0, "project_budget": 0}


def test_axes_gate_union_runs_two_axes(ns, monkeypatch):
    """A leaf feeding two axes (e.g. `alert_thresholds`) reconciles both in ONE
    cycle via the axes union; the un-named axis stays untouched."""
    jr = _jr()
    fired = _wire_reconcile_spies(ns, monkeypatch)
    jr.run_stats_ingest(
        mode="authoritative",
        reconcile_config={"budget": {"weekly_usd": 50.0},
                          "axes": {"codex_budget", "project_budget"},
                          "touched_projects": None},
    )
    assert fired == {"budget": 0, "codex_budget": 1, "project_budget": 1}


def test_axes_none_runs_all_axes_back_compat(ns, monkeypatch):
    """`axes` absent (None) preserves the pre-6f behavior — all three axes run
    (the existing 6c tests rely on this)."""
    jr = _jr()
    fired = _wire_reconcile_spies(ns, monkeypatch)
    jr.run_stats_ingest(
        mode="authoritative",
        reconcile_config={"budget": {"weekly_usd": 50.0}, "touched_projects": None},
    )
    assert fired == {"budget": 1, "codex_budget": 1, "project_budget": 1}


def test_empty_journal_reconcile_latches_and_journals(ns, monkeypatch):
    """The reconcile-only cycle over a still-EMPTY journal (no obs/op line) both
    RUNS the reconcile and HARVESTS its latched crossing into a budget evt — the
    critical fix, since a config write appends no journal line of its own."""
    jr = _jr()
    J = _jlib()

    def fake_budget_reconcile(vb, *, conn=None, as_of=None):
        conn.execute(
            "INSERT OR IGNORE INTO budget_milestones "
            "(vendor, period_start_at, period, threshold, budget_usd, "
            " spent_usd, consumption_pct, crossed_at_utc) "
            "VALUES ('claude', '2026-01-01T00:00:00+00:00', 'subscription-week', "
            "        90, 50.0, 46.0, 92.0, '2026-01-04T12:00:00Z')"
        )
    monkeypatch.setitem(ns, "_reconcile_budget_on_config_write", fake_budget_reconcile)
    monkeypatch.setitem(ns, "_reconcile_codex_budget_on_config_write", lambda *a, **k: None)
    monkeypatch.setitem(ns, "_reconcile_project_budget_milestones_on_write", lambda *a, **k: None)

    # No append: the journal is empty (hw is None). The reconcile must still run.
    assert jr.list_segments() == []
    res = jr.run_stats_ingest(
        mode="authoritative",
        reconcile_config={"budget": {"weekly_usd": 50.0}, "axes": {"budget"},
                          "touched_projects": None},
    )
    assert res.ran is True
    assert res.events_emitted == 1, "the latched crossing was harvested into a budget evt"

    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT threshold, journal_id FROM budget_milestones"
        ).fetchall()
    finally:
        conn.close()
    assert len(row) == 1 and row[0][0] == 90
    assert row[0][1] is not None and row[0][1].startswith("bm:"), "latched crossing journaled"
    # The harvest created the first segment; a bm evt line is present.
    evts = [l for l in _all_journal_lines(jr, J)
            if (l.get("payload") or {}).get("kind") == "budget"]
    assert len(evts) == 1


def test_reconcile_budget_config_wrapper_opportunistic_and_wrapped(ns, monkeypatch):
    """`reconcile_budget_config` routes opportunistic + exception-wrapped: it
    fires the named axis, and a RAISING reconcile never propagates out (a config
    write must never fail because a forward-only reconcile errored)."""
    jr = _jr()
    fired = {"n": 0}
    monkeypatch.setitem(
        ns, "_reconcile_budget_on_config_write",
        lambda vb, **k: fired.__setitem__("n", fired["n"] + 1))
    jr.reconcile_budget_config({"weekly_usd": 50.0}, axes={"budget"})
    assert fired["n"] == 1

    def boom(vb, **k):
        raise RuntimeError("reconcile boom")
    monkeypatch.setitem(ns, "_reconcile_budget_on_config_write", boom)
    # Must NOT raise — the wrapper swallows + logs.
    jr.reconcile_budget_config({"weekly_usd": 50.0}, axes={"budget"})

    # Empty axes / falsy budget = no-op (never touches the ingest lock).
    calls = {"n": 0}
    monkeypatch.setitem(ns, "_reconcile_budget_on_config_write",
                        lambda vb, **k: calls.__setitem__("n", calls["n"] + 1))
    jr.reconcile_budget_config({"weekly_usd": 50.0}, axes=set())
    jr.reconcile_budget_config(None, axes={"budget"})
    assert calls["n"] == 0


# ==========================================================================
# 6f item 1 — record-credit `--force` clear journaling. A forced re-record's
# wce evt carries the destructive clear (OLD synthetics + OLD floors by logical
# journal_id); replay reproduces exactly ONE floor + ONE synthetic.
# ==========================================================================

def test_force_clear_rerecord_journals_clear_and_replays(ns):
    jr = _jr()
    J = _jlib()

    # op1: a first credit 60 -> 40 (not forced). Creates floor1 + synthetic1(40).
    op1 = _credit_op(J, op_at="2026-01-04T09:00:05Z", from_pct=60.0, to_pct=40.0,
                     op_effective="2026-01-04T09:00:00+00:00")
    jr.append_record(op1, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    # op2: a --force re-record 50 -> 30 at a NEW effective. Clears op1's floor +
    # synthetic, installs floor2 + synthetic2(30). from_pct=50 so the stale-replay
    # doomed set does NOT overlap synthetic1(40) — the clear is the ONLY thing
    # removing synthetic1.
    op2 = _credit_op(J, op_at="2026-01-04T10:00:05Z", from_pct=50.0, to_pct=30.0,
                     plan_effective="2026-01-04T10:00:00+00:00",
                     op_effective="2026-01-04T10:00:00+00:00", forced=True)
    jr.append_record(op2, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    conn = ns["open_db"]()
    try:
        floors = conn.execute(
            "SELECT journal_id FROM weekly_credit_floors ORDER BY id").fetchall()
        syn = conn.execute(
            "SELECT weekly_percent, journal_id FROM weekly_usage_snapshots "
            "WHERE source = 'record-credit' ORDER BY id").fetchall()
        old_syn = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE journal_id = ?", (f"sa:{op1['id']}:syn:0",)).fetchone()[0]
        old_floor = conn.execute(
            "SELECT COUNT(*) FROM weekly_credit_floors WHERE journal_id = ?",
            (op1["id"],)).fetchone()[0]
    finally:
        conn.close()

    # Exactly ONE floor (op2's, non-NULL journal_id) and ONE synthetic (30).
    assert len(floors) == 1 and floors[0][0] == op2["id"]
    assert len(syn) == 1 and float(syn[0][0]) == 30.0
    assert syn[0][1] == f"sa:{op2['id']}:syn:0"
    # op1's floor + synthetic were cleared.
    assert old_syn == 0, "--force cleared the OLD synthetic snapshot"
    assert old_floor == 0, "--force cleared the OLD credit floor"

    # The wce2 evt carries the clear: floor_suppression = [op1 floor id],
    # suppression contains the old synthetic id.
    lines = _all_journal_lines(jr, J)
    wce2 = [e for e in lines if e["id"] == f"wce:{op2['id']}"]
    assert len(wce2) == 1
    assert wce2[0]["payload"]["floor_suppression"] == [op1["id"]]
    assert f"sa:{op1['id']}:syn:0" in wce2[0]["payload"]["suppression"]

    # Fold-replay into a FRESH DB (op fold + evt lines, canonical order)
    # reproduces the SAME end state — exactly one floor + one synthetic.
    import _cctally_core
    fresh = _cctally_core.open_db()
    try:
        fresh.execute("BEGIN IMMEDIATE")
        for line in lines:
            if line.get("t") == "op":
                jr._pipeline_op_fold(jr.IngestContext(conn=fresh, batch=[]), line)
            elif line.get("t") == "evt":
                jr._apply_evt(fresh, line)
        fresh.commit()
        f_floors = fresh.execute(
            "SELECT journal_id FROM weekly_credit_floors").fetchall()
        f_syn = fresh.execute(
            "SELECT weekly_percent, journal_id FROM weekly_usage_snapshots "
            "WHERE source = 'record-credit'").fetchall()
    finally:
        fresh.close()
    assert len(f_floors) == 1 and f_floors[0][0] == op2["id"], "replay: one floor (op2)"
    assert len(f_syn) == 1 and float(f_syn[0][0]) == 30.0, "replay: one synthetic (30)"


# ==========================================================================
# 6f item 2 — sync-week CLI reroute. The CLI path (conn=None, journal=None)
# appends a `sync_week` op + authoritative ingest, then reads the journaled
# `weekly_cost_snapshots` row back (keyed `wcs:<op id>:%`) for its output.
# ==========================================================================

def test_sync_week_cli_reroute_appends_op_and_reads_back(ns, capsys):
    import argparse
    import json as _json
    jr = _jr()
    J = _jlib()
    args = argparse.Namespace(
        week_start=None, week_end=None, week_start_name=None,
        mode="auto", offline=True, project=None, json=True, quiet=False,
    )
    rc = ns["cmd_sync_week"](args)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["id"] is not None and "costUSD" in payload

    lines = _all_journal_lines(jr, J)
    ops = [l for l in lines
           if l.get("t") == "op" and (l.get("payload") or {}).get("kind") == "sync_week"]
    assert len(ops) == 1, "exactly one sync_week op appended"
    op_id = ops[0]["id"]

    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT id, journal_id, cost_usd FROM weekly_cost_snapshots "
            "WHERE journal_id LIKE ?", (f"wcs:{op_id}:%",)).fetchone()
    finally:
        conn.close()
    assert row is not None, "wcs row journaled + read back by the wcs:<op id>:% key"
    assert row[1].startswith(f"wcs:{op_id}:")
    assert payload["id"] == row[0], "the CLI output id is the read-back row's rowid"


# ==========================================================================
# 6f item 5 scenario (e) — the statusline AUTHORITATIVE path observes its own
# write: the stable-projection read returns the just-recorded value.
# ==========================================================================

def test_authoritative_record_observed_by_stable_projection(ns):
    import argparse
    import time
    # resets_at within the record-usage plausibility band [now-30d, now+8d]
    # (the runner uses real wall clock, so a fixed 2026-01 epoch is rejected).
    resets_at = int(time.time()) + 3 * 86400
    args = argparse.Namespace(
        percent=42.0, resets_at=str(resets_at),
        five_hour_percent=None, five_hour_resets_at=None, source="statusline",
    )
    rc = ns["cmd_record_usage"](args, ingest_mode="authoritative")
    assert rc == 0
    # The statusline's stable-projection read (the authoritative-publication
    # observe step) reflects the just-recorded 7d value synchronously.
    projection = ns["_read_db_projection_stable"]()
    assert projection.seven_day is not None
    assert projection.seven_day.percent == 42.0


# ==========================================================================
# 6f item 5 scenario (d) — concurrency storm. N spawn processes append + ingest
# (opportunistic + authoritative) against a SHARED data dir; every appended id
# materializes exactly once, <=1 concurrent ingester, zero `database is locked`.
# Workers are module-level (spawn-picklable); isolation is CCTALLY_DATA_DIR +
# HOME, NOT the in-process redirect_paths fixture.
# ==========================================================================

def _storm_worker(bin_dir, home_dir, data_dir, worker_id, mode, q):
    """Spawn-picklable storm worker: isolate to the shared tmp data dir, load a
    fresh cctally (registers the pipeline hooks), append ONE distinct-week obs,
    run one ingest cycle in `mode`, and report its outcome."""
    import os
    import sys
    import datetime as _dt
    os.environ["CCTALLY_DATA_DIR"] = data_dir
    os.environ["HOME"] = home_dir
    os.environ["TZ"] = "Etc/UTC"
    sys.path.insert(0, bin_dir)
    try:
        import importlib.util
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("cctally", os.path.join(bin_dir, "cctally"))
        spec = importlib.util.spec_from_loader("cctally", loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        import _lib_journal as lj
        import _cctally_journal as _cjr
        base = int(_dt.datetime(2026, 1, 8, tzinfo=_dt.timezone.utc).timestamp())
        week_end = base + worker_id * 7 * 86400  # distinct week per worker
        at = f"2026-01-04T09:{worker_id:02d}:00Z"
        obs = lj.make_obs(
            at=at, src="record-usage", provider="claude",
            payload={"weekly_percent": 10.0, "resets_at": week_end,
                     "source": "statusline", "captured_at": at})
        _cjr.append_record(obs)
        res = _cjr.run_stats_ingest(mode=mode)
        q.put((worker_id, "ok", obs["id"], bool(res.ran),
               None if res.error is None else str(res.error)))
    except Exception as exc:  # a `database is locked` propagates here
        q.put((worker_id, f"ERR:{type(exc).__name__}:{exc}", None, None, None))


def _storm_drain_count(bin_dir, home_dir, data_dir, q):
    """Drain any leftover un-ingested obs (a final authoritative cycle) and count
    the materialized snapshot_accept rows."""
    import os
    import sys
    os.environ["CCTALLY_DATA_DIR"] = data_dir
    os.environ["HOME"] = home_dir
    os.environ["TZ"] = "Etc/UTC"
    sys.path.insert(0, bin_dir)
    try:
        import importlib.util
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("cctally", os.path.join(bin_dir, "cctally"))
        spec = importlib.util.spec_from_loader("cctally", loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        import _cctally_core
        import _cctally_journal as _cjr
        # A couple of authoritative drains consume every remaining line.
        for _ in range(3):
            _cjr.run_stats_ingest(mode="authoritative")
        conn = _cctally_core.open_db()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM weekly_usage_snapshots "
                "WHERE journal_id LIKE 'sa:%'").fetchone()[0]
            distinct = conn.execute(
                "SELECT COUNT(DISTINCT journal_id) FROM weekly_usage_snapshots "
                "WHERE journal_id LIKE 'sa:%'").fetchone()[0]
        finally:
            conn.close()
        q.put(("count", n, distinct))
    except Exception as exc:
        q.put(("ERR", str(exc), None))


def test_concurrency_storm_every_id_materialized_once(tmp_path):
    data_dir = tmp_path / "share"
    home_dir = tmp_path / "home"
    (home_dir / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    n_workers = 8  # 6 opportunistic + 2 authoritative
    modes = ["opportunistic"] * 6 + ["authoritative"] * 2
    procs = []
    for i in range(n_workers):
        p = ctx.Process(
            target=_storm_worker,
            args=(_BIN_DIR, str(home_dir), str(data_dir), i, modes[i], q))
        p.start()
        procs.append(p)
    results = [q.get(timeout=120) for _ in range(n_workers)]
    for p in procs:
        p.join(timeout=60)

    # Every worker completed WITHOUT a propagated exception (a `database is
    # locked` would surface as an ERR status), and no opportunistic cycle aborted
    # with an error (the ingest flock serializes writers, so no SQLITE_BUSY).
    for wid, status, _obs_id, _ran, err in results:
        assert status == "ok", f"worker {wid} failed: {status}"
        assert err is None or "locked" not in err.lower(), (
            f"worker {wid} hit a lock error: {err}")
    appended_ids = {r[2] for r in results}
    assert len(appended_ids) == n_workers, "each worker appended a distinct obs"

    # Drain + count in a fresh process: every appended obs materialized EXACTLY
    # once (distinct-week obs -> one snapshot_accept row each, no dup, none lost).
    dp = ctx.Process(
        target=_storm_drain_count,
        args=(_BIN_DIR, str(home_dir), str(data_dir), q))
    dp.start()
    tag, n, distinct = q.get(timeout=120)
    dp.join(timeout=60)
    assert tag == "count", f"drain failed: {n}"
    assert n == n_workers, f"expected {n_workers} materialized snapshots, got {n}"
    assert distinct == n_workers, "every snapshot_accept id materialized exactly once"


# ==========================================================================
# #834 S1 (#835) Gate A R5 — the writer reroute half. Specification line 124
# requires this module to be EXTENDED, not merely run: a held row that the
# stale-replica removal now preserves has to survive the production ingest
# path, where nothing is deleted inline and the removal happens through a
# journalled suppression list instead.
# ==========================================================================

def _r5_credit_args(**over):
    args = dict(to=30.0, from_pct=63.0, at="2026-01-04T09:02:00Z",
                week="2026-01-01", dry_run=False, yes=True, json=False,
                force=False)
    args.update(over)
    return argparse.Namespace(**args)


def test_a_preserved_held_row_survives_the_writer_reroute(ns):
    """The production `record-credit` path deletes nothing inline: it captures the
    doomed rows' `journal_id`s into a `weekly_credit_effects` evt and the applier
    deletes by logical id. So the held-row preservation has to hold in the
    SUPPRESSION LIST, not only in a DELETE predicate — a list that named the held
    row would destroy on replay what the live pass kept.

    Both rows carry the same weekly 63.0 and differ only in
    `weekly_observation_held`, and the credit's effective instant precedes both, so
    the two are in the band together. The non-held one must go and the held one
    must stay, with its five-hour reading intact — it is the only carrier of that
    reading, which is why #824 wrote it."""
    jr = _jr()
    J = _jlib()
    _held_sequence(jr, J)

    conn = ns["open_db"]()
    try:
        seeded = conn.execute(
            "SELECT id, weekly_observation_held, five_hour_percent, journal_id "
            "FROM weekly_usage_snapshots ORDER BY captured_at_utc, id"
        ).fetchall()
    finally:
        conn.close()
    assert [r["weekly_observation_held"] for r in seeded] == [0, 1], (
        "non-vacuity: the sequence must leave one genuine row and one held row")
    held_journal_id = seeded[1]["journal_id"]

    assert ns["cmd_record_credit"](_r5_credit_args()) == 0

    conn = ns["open_db"]()
    try:
        after = conn.execute(
            "SELECT weekly_percent, weekly_observation_held, "
            "       five_hour_percent, journal_id "
            "FROM weekly_usage_snapshots ORDER BY captured_at_utc, id"
        ).fetchall()
    finally:
        conn.close()

    # The suppression list lives in the append-only journal, not in a table:
    # `weekly_credit_effects` is an effects-only evt whose applier deletes by
    # logical id. Reading it there is what makes the last assertion non-vacuous.
    suppressed = [
        line for line in _all_journal_lines(jr, J)
        if _r5_evt_kind(line) == "weekly_credit_effects"
    ]
    assert suppressed, (
        "non-vacuity: the credit must have journalled a weekly_credit_effects "
        "evt for there to be a suppression list at all")

    held = [r for r in after if r["weekly_observation_held"] == 1]
    assert len(held) == 1, (
        "the held row did not survive the journalled stale-replica removal")
    assert held[0]["five_hour_percent"] == 25.0, (
        "the held row survived but lost the five-hour reading it exists to hold")
    assert not [r for r in after
                if r["weekly_observation_held"] == 0
                and r["weekly_percent"] == 63.0], (
        "the NON-held stale replica must still be removed")
    named = set()
    for line in suppressed:
        named.update(_r5_evt_suppression(line))
    assert named, (
        "non-vacuity: the evt must name the non-held replica it removed")
    assert held_journal_id not in named, (
        "the suppression list names the held row, so a replay would delete "
        "what the live pass preserved")


def _r5_evt_dict(line):
    """The evt mapping inside a decoded journal line, whatever its envelope
    spelling. Written defensively rather than against one shape, because this
    test asserts the ABSENCE of an id and a wrong key would make it vacuous."""
    for candidate in (line, getattr(line, "evt", None),
                      getattr(line, "payload", None)):
        if isinstance(candidate, dict):
            if "kind" in candidate:
                return candidate
            inner = candidate.get("evt") or candidate.get("payload")
            if isinstance(inner, dict) and "kind" in inner:
                return inner
    return {}


def _r5_evt_kind(line):
    return _r5_evt_dict(line).get("kind")


def _r5_evt_suppression(line):
    evt = _r5_evt_dict(line)
    columns = evt.get("columns")
    if isinstance(columns, dict):
        return list(columns.get("suppression") or ())
    return list(evt.get("suppression") or ())
