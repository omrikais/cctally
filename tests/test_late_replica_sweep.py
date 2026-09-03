"""What `_sweep_late_replicas` runs for, and what it must leave alone (§5.4).

Two things are pinned here: that a MANUAL credit's epoch is swept at all, and
that a dip the store's own write clamp admits is not read as a contradiction.

`_sweep_late_replicas` returned early whenever the governing credit recorded no
`confirming_capture_at_utc`, and the manual fold always writes that column NULL
by design: a retroactive assertion has no confirming observation, which is why
§5.1's manual rule has no upper bracket end.

The consequence is the one §5.4 exists to prevent, and §5.4's wording does not
exclude a manual epoch. After `cctally record-credit`, a stale high reading
replayed by the status line is admitted by the write clamp — inside the epoch it
is a climb above the credited level, which is indistinguishable from a genuine
one at that instant. It then stands, becomes `prior_pct` on the next genuine
tick, and the drop back to the credited level fires a phantom automatic credit
that opens a second epoch and restarts the milestone ladder.

The bracket leg still needs both instants and is still skipped without them.
Only the contradicted-LEVEL leg runs, and its lower bound falls back to the
asserted credit instant, which is the same role `confirming_capture_at_utc`
plays on the automatic path: the instant after which a reading at the pre-credit
level can only be a replay.

The second half is the counterexample to "within one accounting epoch the weekly
counter never falls". `hwm_clamp_applies` compares at tenths granularity, so a
value up to about 0.05pp below the recorded maximum is admitted and stored. A
dip that crosses `replica_level` then supplied a false contradiction, and the
sweep deleted the genuine row just above it. The contradiction is therefore
tested at the SAME granularity the store admits at, so a value the clamp let
through can never trigger a removal.
"""
from __future__ import annotations

import argparse
import datetime as dt

import pytest

from conftest import load_script, redirect_paths

WEEK_START_DATE = "2026-06-13"
WS_AT = "2026-06-13T05:00:00+00:00"
WE_AT = "2026-06-20T05:00:00+00:00"
WEEK_END_EPOCH = int(
    dt.datetime(2026, 6, 20, 5, 0, tzinfo=dt.timezone.utc).timestamp())
CREDIT_AT = "2026-06-18T22:00:00Z"
REPLAY_AT = "2026-06-18T23:30:00Z"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _tick(ns, monkeypatch, *, at, percent):
    monkeypatch.setenv("CCTALLY_AS_OF", at)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    return ns["cmd_record_usage"](argparse.Namespace(
        percent=percent, resets_at=WEEK_END_EPOCH, five_hour_percent=None,
        five_hour_resets_at=None, week_start_name=None))


def _credit_rows(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT id, credit_key, observed_at_utc, confirming_capture_at_utc "
            "FROM week_reset_events ORDER BY id").fetchall()
    finally:
        conn.close()


def _snapshots(ns):
    conn = ns["open_db"]()
    try:
        return [
            (r["captured_at_utc"], r["weekly_percent"])
            for r in conn.execute(
                "SELECT captured_at_utc, weekly_percent "
                "FROM weekly_usage_snapshots WHERE week_start_date = ? "
                "ORDER BY captured_at_utc, id", (WEEK_START_DATE,))
        ]
    finally:
        conn.close()


def _drive_manual_credit_then_replay(ns, monkeypatch):
    """A manual credit to zero, then the status line replays the old value."""
    assert _tick(ns, monkeypatch, at="2026-06-18T21:12:00Z", percent=46.0) == 0
    monkeypatch.setenv("CCTALLY_AS_OF", CREDIT_AT)
    assert ns["cmd_record_credit"](argparse.Namespace(
        to=0.0, from_pct=None, at=None, week=None, dry_run=False, yes=True,
        json=False, force=False)) == 0
    rows = _credit_rows(ns)
    assert len(rows) == 1, rows
    assert rows[0]["confirming_capture_at_utc"] is None, (
        "the manual fold no longer writes a NULL confirming instant, so this "
        "test no longer exercises the early return it was written for")
    # The replay is above the credited level, so at that instant it is
    # indistinguishable from a genuine climb and the clamp admits it.
    assert _tick(ns, monkeypatch, at=REPLAY_AT, percent=46.0) == 0
    assert (REPLAY_AT, 46.0) in _snapshots(ns), _snapshots(ns)


def test_a_manual_credits_epoch_sweeps_a_late_replica(ns, monkeypatch):
    _drive_manual_credit_then_replay(ns, monkeypatch)
    # A genuine tick below the pre-credit level contradicts the replay: within
    # one accounting epoch the weekly counter never falls.
    assert _tick(ns, monkeypatch, at="2026-06-19T00:00:00Z", percent=0.5) == 0
    assert (REPLAY_AT, 46.0) not in _snapshots(ns), _snapshots(ns)


def test_a_manual_credit_is_not_followed_by_a_phantom_automatic_credit(
        ns, monkeypatch):
    """The replay left standing becomes `prior_pct`, and the drop back to the
    credited level then reads as a fresh ≥25pp reset."""
    _drive_manual_credit_then_replay(ns, monkeypatch)
    assert _tick(ns, monkeypatch, at="2026-06-19T00:00:00Z", percent=0.5) == 0
    rows = _credit_rows(ns)
    assert len(rows) == 1, (
        "a phantom automatic credit opened a second epoch: "
        f"{[dict(r) for r in rows]}")


def test_a_manual_credits_genuine_later_climb_is_kept(ns, monkeypatch):
    """The sweep must not mistake real usage for a replay.

    A credit to a NON-zero level with genuine climb above it: every stored row
    stays below the pre-credit high-water mark, so no reading contradicts it and
    nothing is removed.
    """
    assert _tick(ns, monkeypatch, at="2026-06-18T21:12:00Z", percent=46.0) == 0
    monkeypatch.setenv("CCTALLY_AS_OF", CREDIT_AT)
    assert ns["cmd_record_credit"](argparse.Namespace(
        to=31.0, from_pct=None, at=None, week=None, dry_run=False, yes=True,
        json=False, force=False)) == 0
    for at, pct in (("2026-06-18T23:00:00Z", 32.0),
                    ("2026-06-19T01:00:00Z", 34.0),
                    ("2026-06-19T03:00:00Z", 35.0)):
        assert _tick(ns, monkeypatch, at=at, percent=pct) == 0
    stored = {pct for _at, pct in _snapshots(ns)}
    assert {32.0, 34.0, 35.0} <= stored, _snapshots(ns)
    assert len(_credit_rows(ns)) == 1, _credit_rows(ns)


def test_a_dip_the_write_clamp_admits_is_not_a_contradiction(ns, monkeypatch):
    """`hwm_clamp_applies` rounds to tenths, so the counter CAN dip in-epoch.

    A reading up to about 0.05pp below the recorded maximum is admitted and
    stored, because the clamp compares `round(x, 1)` on both sides. While the
    sweep compared exactly, such a dip crossing `replica_level` fired the
    contradicted-level rule and deleted the genuine row just above it — a real
    counterexample to the invariant the rule rests on.
    """
    assert _tick(ns, monkeypatch, at="2026-06-18T21:12:00Z", percent=46.0) == 0
    monkeypatch.setenv("CCTALLY_AS_OF", CREDIT_AT)
    assert ns["cmd_record_credit"](argparse.Namespace(
        to=31.0, from_pct=None, at=None, week=None, dry_run=False, yes=True,
        json=False, force=False)) == 0
    # A legitimate in-epoch re-climb back to the pre-credit level.
    assert _tick(ns, monkeypatch, at="2026-06-19T02:00:00Z", percent=46.04) == 0
    assert 46.04 in {pct for _at, pct in _snapshots(ns)}, _snapshots(ns)
    # The dip: below `replica_level` (46.0) but not below it at the granularity
    # the clamp admits at, so the clamp stores it.
    assert _tick(ns, monkeypatch, at="2026-06-19T03:00:00Z", percent=45.96) == 0
    assert 46.04 in {pct for _at, pct in _snapshots(ns)}, (
        "a dip the write clamp admits deleted the genuine row above it: "
        f"{_snapshots(ns)}")
