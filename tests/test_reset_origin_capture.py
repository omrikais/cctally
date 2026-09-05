"""#750 S3 Task A4 — what each write path stores, and on which clock.

Spec §1.2 and §1.4. Epoch 1013 added `week_reset_events.origin_observation_id`
and the journal learned the dual identity; this module pins what actually gets
written into the column, and pins that `effective_reset_at_utc` records the
exact UTC second rather than the hour it falls in.

Four write paths, three of them live:

* immediate in-place fire -> the triggering observation's raw journal id;
* mid-week boundary-shift insert -> the current observation's id;
* `CONFIRM_RESET` -> the FIRST-ZERO observation's id, read from the debounce
  state, not the confirming observation's. That is required for crash-replay
  safety, and it corrects the design consultation's own earlier answer;
* backfill -> the raw observation behind the snapshot's `journal_id`, or NULL.
  It never invents an identity.

The clock is `payload["captured_at"]`, NOT `rec["at"]`. The journal pipeline
distinguishes them deliberately; they are equal in production and differ under
`CCTALLY_AS_OF`, and threading the wrong one would make live detection and
backfill disagree about the same physical reset.

Hour flooring is gone. It was what back-dated the 2026-09-01 event before
observations that were still legitimately pre-credit, so a stale replica
survived the DELETE and seeded a fresh milestone epoch at a threshold the
meter said had not been crossed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

RAW_ID = "o:" + "a1b2c3d4e5f60718"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _record_usage_args(*, percent, resets_at):
    return argparse.Namespace(
        percent=percent, resets_at=resets_at,
        five_hour_percent=None, five_hour_resets_at=None,
        week_start_name=None,
    )


def _pin_as_of(monkeypatch, offset_seconds):
    """Pin the DETECTION clock away from the capture clock.

    `cmd_record_usage` stamps `payload["captured_at"]` from the wall clock and
    the obs line's `at` from `_command_as_of()`, so setting `CCTALLY_AS_OF`
    separates the two. It also gives each tick a distinct observation id,
    which the self-confirmation rule requires for a genuine confirm.
    """
    stamp = (dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
             + dt.timedelta(seconds=offset_seconds))
    monkeypatch.setenv(
        "CCTALLY_AS_OF", stamp.isoformat().replace("+00:00", "Z"))
    return stamp


def _future_week_end(days=3):
    now = dt.datetime.now(dt.timezone.utc)
    future = (now + dt.timedelta(days=days)).replace(
        minute=0, second=0, microsecond=0)
    return future.isoformat(timespec="seconds"), int(future.timestamp())


def _week_start_for(end_iso):
    return (dt.datetime.fromisoformat(end_iso)
            - dt.timedelta(days=7)).date().isoformat()


def _seed_baseline(ns, *, week_start_date, end_iso, pct):
    conn = ns["open_db"]()
    try:
        cur = conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, source, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("2026-05-14T10:00:00Z", week_start_date, end_iso[:10],
             week_start_date + "T00:00:00+00:00", end_iso, pct, "test", "{}"))
        rowid = int(cur.lastrowid)
        conn.execute(
            "UPDATE weekly_usage_snapshots SET journal_id = ? WHERE id = ?",
            (f"b:weekly_usage_snapshots:{rowid}", rowid))
        conn.commit()
    finally:
        conn.close()
    (ns["APP_DIR"] / "hwm-7d").write_text(f"{week_start_date} {pct}\n")


def _events(ns):
    conn = ns["open_db"]()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc, "
            "origin_observation_id FROM week_reset_events ORDER BY id")]
    finally:
        conn.close()


def _obs_lines():
    """Every Claude usage obs in the journal, oldest first."""
    import _cctally_journal as jr
    out = []
    for name in jr.list_segments():
        for raw in (_cctally_core.JOURNAL_DIR / name).read_bytes().splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("t") == "obs" and rec.get("provider") == "claude":
                out.append(rec)
    return out


# --------------------------------------------------------------------------
# What each path stores
# --------------------------------------------------------------------------

def test_a4_the_immediate_leg_stores_the_triggering_observation_id(
        ns, monkeypatch):
    end_iso, end_epoch = _future_week_end()
    week_start_date = _week_start_for(end_iso)
    _seed_baseline(ns, week_start_date=week_start_date, end_iso=end_iso,
                   pct=67.0)

    _pin_as_of(monkeypatch, 0)
    assert ns["cmd_record_usage"](
        _record_usage_args(percent=2.0, resets_at=end_epoch)) == 0

    obs = _obs_lines()
    assert obs, "no observation was journaled"
    events = _events(ns)
    assert len(events) == 1, events
    assert events[0]["origin_observation_id"] == obs[-1]["id"]


def test_a4_the_confirm_leg_stores_the_first_zero_not_the_confirming_one(
        ns, monkeypatch):
    """Required for crash-replay safety: a replayed confirming observation
    must reproduce the same event id, and the first zero is the only instant
    both the crashed cycle and its retry agree on."""
    end_iso, end_epoch = _future_week_end()
    week_start_date = _week_start_for(end_iso)
    _seed_baseline(ns, week_start_date=week_start_date, end_iso=end_iso,
                   pct=14.0)

    _pin_as_of(monkeypatch, 0)
    assert ns["cmd_record_usage"](
        _record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    first_zero = _obs_lines()[-1]
    assert _events(ns) == []

    _pin_as_of(monkeypatch, 60)
    assert ns["cmd_record_usage"](
        _record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    confirming = _obs_lines()[-1]
    assert confirming["id"] != first_zero["id"]

    events = _events(ns)
    assert len(events) == 1, events
    assert events[0]["origin_observation_id"] == first_zero["id"], (
        "the confirming observation was stored, so a replay of it would mint "
        "a second event id for one physical reset")


def test_a4_the_boundary_shift_insert_stores_the_current_observation_id(
        ns, monkeypatch):
    """A mid-week boundary advance: `old != effective`, so this row must not
    split the week — but it still names the observation that caused it."""
    old_end_iso, old_end_epoch = _future_week_end(days=2)
    new_end_iso, new_end_epoch = _future_week_end(days=6)
    week_start_date = _week_start_for(old_end_iso)
    _seed_baseline(ns, week_start_date=week_start_date, end_iso=old_end_iso,
                   pct=67.0)

    _pin_as_of(monkeypatch, 0)
    assert ns["cmd_record_usage"](
        _record_usage_args(percent=2.0, resets_at=new_end_epoch)) == 0

    events = _events(ns)
    assert len(events) == 1, events
    assert events[0]["old_week_end_at"] == old_end_iso
    assert events[0]["new_week_end_at"] == new_end_iso
    assert events[0]["old_week_end_at"] != events[0]["effective_reset_at_utc"]
    assert events[0]["origin_observation_id"] == _obs_lines()[-1]["id"]


@pytest.mark.parametrize("journal_id,expected", [
    (f"sa:{RAW_ID}", RAW_ID),
    ("b:weekly_usage_snapshots:7", None),
    ("sa:direct:3", None),
    (None, None),
    # The synthetic post-credit snapshot a manual `record-credit` writes. One
    # `sa:` strip leaves `o:<hex>:syn:0`, which is not an observation id, so
    # storing it would invent an identity the journal does not contain.
    (f"sa:{RAW_ID}:syn:0", None),
])
def test_a4_the_backfill_derives_an_origin_only_from_a_real_observation(
        ns, journal_id, expected):
    end_iso, _ = _future_week_end()
    week_start_date = _week_start_for(end_iso)
    conn = ns["open_db"]()
    try:
        conn.execute("DELETE FROM week_reset_events")
        for captured, pct, jid in (
            ("2026-05-14T10:00:00Z", 67.0, "b:weekly_usage_snapshots:1"),
            ("2026-05-14T11:23:45Z", 2.0, journal_id),
        ):
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, source, "
                " payload_json, journal_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (captured, week_start_date, end_iso[:10],
                 week_start_date + "T00:00:00+00:00", end_iso, pct, "test",
                 "{}", jid))
        conn.commit()
        ns["_backfill_week_reset_events"](conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT effective_reset_at_utc, origin_observation_id "
            "FROM week_reset_events")]
    finally:
        conn.close()

    assert len(rows) == 1, rows
    assert rows[0]["origin_observation_id"] == expected


# --------------------------------------------------------------------------
# The clock, and the exact second
# --------------------------------------------------------------------------

def test_a4_the_immediate_effective_instant_is_the_exact_payload_capture(
        ns, monkeypatch):
    """`rec["at"]` is the DETECTION clock and `payload["captured_at"]` is the
    capture stamp. Under `CCTALLY_AS_OF` they differ, and the effective instant
    must follow the capture — to the second, with no hour flooring."""
    end_iso, end_epoch = _future_week_end()
    week_start_date = _week_start_for(end_iso)
    _seed_baseline(ns, week_start_date=week_start_date, end_iso=end_iso,
                   pct=67.0)

    detection = _pin_as_of(monkeypatch, -300)
    assert ns["cmd_record_usage"](
        _record_usage_args(percent=2.0, resets_at=end_epoch)) == 0

    obs = _obs_lines()[-1]
    captured = obs["payload"]["captured_at"]
    assert obs["at"] != captured, (
        "the fixture failed to separate the two clocks, so this proves nothing")

    events = _events(ns)
    assert len(events) == 1
    effective = dt.datetime.fromisoformat(
        events[0]["effective_reset_at_utc"])
    assert effective == dt.datetime.fromisoformat(
        captured.replace("Z", "+00:00")).replace(microsecond=0)
    assert effective != detection.replace(microsecond=0), (
        "the effective instant followed the detection clock")
    assert effective.minute or effective.second, (
        "the instant fell exactly on an hour mark, so this cannot tell an "
        "unfloored value from a floored one — re-run")


def test_a4_the_confirm_effective_instant_is_the_first_zero_capture_second(
        ns, monkeypatch):
    end_iso, end_epoch = _future_week_end()
    week_start_date = _week_start_for(end_iso)
    _seed_baseline(ns, week_start_date=week_start_date, end_iso=end_iso,
                   pct=14.0)

    _pin_as_of(monkeypatch, 0)
    assert ns["cmd_record_usage"](
        _record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    first_zero_capture = _obs_lines()[-1]["payload"]["captured_at"]

    _pin_as_of(monkeypatch, 60)
    assert ns["cmd_record_usage"](
        _record_usage_args(percent=0.0, resets_at=end_epoch)) == 0

    events = _events(ns)
    assert len(events) == 1
    assert dt.datetime.fromisoformat(
        events[0]["effective_reset_at_utc"]) == dt.datetime.fromisoformat(
        first_zero_capture.replace("Z", "+00:00")).replace(microsecond=0)


def test_a4_the_backfill_effective_instant_is_the_exact_capture_second(ns):
    end_iso, _ = _future_week_end()
    week_start_date = _week_start_for(end_iso)
    conn = ns["open_db"]()
    try:
        conn.execute("DELETE FROM week_reset_events")
        for captured, pct, jid in (
            ("2026-05-14T10:00:00Z", 67.0, "b:weekly_usage_snapshots:1"),
            ("2026-05-14T11:23:45Z", 2.0, f"sa:{RAW_ID}"),
        ):
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, source, "
                " payload_json, journal_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (captured, week_start_date, end_iso[:10],
                 week_start_date + "T00:00:00+00:00", end_iso, pct, "test",
                 "{}", jid))
        conn.commit()
        ns["_backfill_week_reset_events"](conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT old_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events")]
    finally:
        conn.close()

    assert len(rows) == 1, rows
    assert rows[0]["effective_reset_at_utc"] == "2026-05-14T11:23:45+00:00", (
        "the backfill still floors to the hour")
    # In-place credit shape: old == effective.
    assert rows[0]["old_week_end_at"] == rows[0]["effective_reset_at_utc"]
