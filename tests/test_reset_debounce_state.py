"""#750 S3 Task A3 — the reset-to-zero debounce state becomes transactional.

Spec §1.3. The debounce marker was a file in `APP_DIR`, written and unlinked
outside the stats transaction, so it left two crash windows the review found:

* ARM side. The file was written before the cycle committed. A crash between
  the write and the commit rolled the cursor back but left the marker on
  disk, so the retry saw an armed marker against the SAME physical zero and
  CONFIRMED a reset from one observation.
* CONFIRM side. The file was unlinked after `_fire_in_place_credit` returned
  but before the cycle committed. A crash there rolled the event back and
  left no marker, so the retry saw an unarmed window, classified the low
  reading as NO_ACTION, and the reset was lost with no visible symptom.

`weekly_reset_debounce_state` closes both, because ARM, CONFIRM and CLEAR are
now rows mutated inside the same transaction as the journal cursor and the
reset event. A third rule rides along: an observation can never confirm
itself, so a byte-identical replay of the first zero leaves the state armed
instead of firing.

Every case below drives the real `cmd_record_usage` path and crashes it at
`_write_cursor`, the last step before COMMIT — the same seam
`tests/test_journal_ingest.py` uses for Model-A and harvest crash
convergence.
"""
from __future__ import annotations

import argparse
import datetime as dt

import pytest

from conftest import load_script, redirect_paths


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
    """Give each tick a DISTINCT capture instant.

    An observation's journal id is a digest over `{t, at, src, provider,
    payload}`, so two zero readings recorded inside the same wall-clock second
    are literally one observation. Under §1.3's self-confirmation rule the
    second one then cannot confirm the first — correctly, because it carries
    no new information — so a test that means to exercise a genuine two-tick
    confirm has to separate the ticks.
    """
    stamp = (dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
             + dt.timedelta(seconds=offset_seconds))
    monkeypatch.setenv(
        "CCTALLY_AS_OF", stamp.isoformat().replace("+00:00", "Z"))


def _future_week_end_iso():
    now = dt.datetime.now(dt.timezone.utc)
    future = (now + dt.timedelta(days=3)).replace(
        minute=0, second=0, microsecond=0)
    return future.isoformat(timespec="seconds"), int(future.timestamp())


def _week_start_for(end_iso):
    end = dt.datetime.fromisoformat(end_iso)
    return (end - dt.timedelta(days=7)).date().isoformat()


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


def _state_rows(ns):
    conn = ns["open_db"]()
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT account_key, week_start_date, week_end_at, baseline_pct, "
            "first_zero_at_utc, first_zero_observation_id "
            "FROM weekly_reset_debounce_state ORDER BY account_key")]
    finally:
        conn.close()


def _events(ns):
    conn = ns["open_db"]()
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc, "
            "origin_observation_id FROM week_reset_events ORDER BY id")]
    finally:
        conn.close()


def _crash_at_write_cursor(jr):
    def boom(conn, segment, offset):
        raise RuntimeError("simulated crash before commit")
    return boom


def _record_crashing(ns, jr, args):
    original = jr._write_cursor
    jr._write_cursor = _crash_at_write_cursor(jr)
    try:
        with pytest.raises(RuntimeError):
            ns["cmd_record_usage"](args)
    finally:
        jr._write_cursor = original


def _journal():
    import _cctally_journal as jr
    return jr


# --------------------------------------------------------------------------
# The two crash windows
# --------------------------------------------------------------------------

def test_a3_an_arm_side_crash_rolls_the_state_back_and_only_re_arms(
        ns, monkeypatch):
    """The file marker survived the rollback and made the retry confirm a
    reset from ONE physical zero. The row rolls back with the cursor, so the
    retry re-arms and still fires nothing."""
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date = _week_start_for(end_iso)
    _seed_baseline(ns, week_start_date=week_start_date, end_iso=end_iso,
                   pct=14.0)
    jr = _journal()

    # The crash and retry replay one physical observation. Pin its capture
    # instant so a loaded runner crossing a wall-clock second cannot turn the
    # retry into a genuinely later zero that correctly confirms the reset.
    _pin_as_of(monkeypatch, 0)
    _record_crashing(ns, jr, _record_usage_args(percent=0.0,
                                                resets_at=end_epoch))
    assert _state_rows(ns) == [], (
        "the arm was not rolled back with the cursor, so the retry would see "
        "it armed against the very observation that armed it")
    assert _events(ns) == []

    # Retry: the same zero is re-read from the journal (the cursor never
    # advanced) and must ARM, not CONFIRM.
    assert ns["cmd_record_usage"](
        _record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    rows = _state_rows(ns)
    assert len(rows) == 1, rows
    assert rows[0][1] == week_start_date
    assert rows[0][3] == 14.0
    assert _events(ns) == [], (
        "the retry confirmed a reset from a single physical zero")


def test_a3_a_confirm_side_crash_yields_one_event_with_the_first_zero_origin(
        ns, monkeypatch):
    """The file marker was unlinked before the commit, so a crash there lost
    the reset entirely: the retry found no armed window and classified the low
    reading as NO_ACTION. The row rolls back with the event, so the retry
    re-confirms and produces exactly one event."""
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date = _week_start_for(end_iso)
    _seed_baseline(ns, week_start_date=week_start_date, end_iso=end_iso,
                   pct=14.0)
    jr = _journal()

    # Tick 1: arm, committed.
    _pin_as_of(monkeypatch, 0)
    assert ns["cmd_record_usage"](
        _record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    armed = _state_rows(ns)
    assert len(armed) == 1
    first_zero_at, first_zero_origin = armed[0][4], armed[0][5]
    assert _events(ns) == []

    # Tick 2: a genuinely later zero, which would confirm — crash before the
    # commit.
    _pin_as_of(monkeypatch, 60)
    _record_crashing(ns, jr, _record_usage_args(percent=0.0,
                                                resets_at=end_epoch))
    assert _events(ns) == [], "the event survived a rolled-back cycle"
    after_crash = _state_rows(ns)
    assert len(after_crash) == 1, (
        "the state deletion outlived the transaction that owned it, so the "
        "reset is now unrecoverable")
    assert after_crash[0][4] == first_zero_at
    assert after_crash[0][5] == first_zero_origin

    # Retry: the crashed cycle's line is already fsynced and the cursor never
    # advanced, so replaying it is the whole retry. Exactly one event.
    jr.run_stats_ingest(mode="authoritative")
    events = _events(ns)
    assert len(events) == 1, events
    assert events[0][1] == end_iso
    assert _state_rows(ns) == [], "the state was not cleared after the fire"


# --------------------------------------------------------------------------
# An observation cannot confirm itself
# --------------------------------------------------------------------------

def test_a3_a_byte_identical_first_zero_replay_cannot_confirm_itself(ns):
    """§1.3. Replaying the observation that armed the state must leave it
    armed. Without the rule, one physical zero re-read from the journal
    reaches the confirm branch and mints a reset nobody observed twice."""
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date = _week_start_for(end_iso)
    _seed_baseline(ns, week_start_date=week_start_date, end_iso=end_iso,
                   pct=14.0)
    jr = _journal()

    assert ns["cmd_record_usage"](
        _record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    armed = _state_rows(ns)
    assert len(armed) == 1
    origin = armed[0][5]
    assert origin, "the armed state did not record its first-zero observation"

    # Rewind the cursor so the SAME journal line is consumed again, which is
    # exactly what a crash-replay does.
    conn = ns["open_db"]()
    try:
        conn.execute(
            "UPDATE journal_cursor SET offset = 0, applied_offset = 0 "
            "WHERE id = 1")
        conn.commit()
    finally:
        conn.close()

    jr.run_stats_ingest(mode="authoritative")

    assert _events(ns) == [], (
        "the first-zero observation confirmed itself")
    rows = _state_rows(ns)
    assert len(rows) == 1 and rows[0][5] == origin, (
        "the state must stay armed, unchanged, after a self-replay")


def test_a3_the_state_is_scoped_to_one_account(ns):
    """The row is keyed on `account_key`, so one account's armed window can
    never be read as another's."""
    import _cctally_record as rec
    conn = ns["open_db"]()
    try:
        rec._arm_reset_debounce_state(
            conn, "acct-a", week_start_date="2026-09-05",
            week_end_at="2026-09-12T15:00:00+00:00", baseline_pct=14.0,
            first_zero_at_utc="2026-09-06T10:00:00+00:00",
            first_zero_observation_id="o:" + "a" * 16)
        conn.commit()
        assert rec._read_reset_debounce_state(conn, "acct-b") is None
        got = rec._read_reset_debounce_state(conn, "acct-a")
        assert got == ("2026-09-05", "2026-09-12T15:00:00+00:00", 14.0,
                       "2026-09-06T10:00:00+00:00", "o:" + "a" * 16)
        # ARM is an upsert: re-arming the same account replaces the row.
        rec._arm_reset_debounce_state(
            conn, "acct-a", week_start_date="2026-09-12",
            week_end_at="2026-09-19T15:00:00+00:00", baseline_pct=9.0,
            first_zero_at_utc="2026-09-13T10:00:00+00:00",
            first_zero_observation_id="o:" + "b" * 16)
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_reset_debounce_state"
        ).fetchone()[0] == 1
        rec._clear_reset_debounce_state(conn, "acct-a")
        conn.commit()
        assert rec._read_reset_debounce_state(conn, "acct-a") is None
    finally:
        conn.close()


def test_a3_the_filesystem_marker_is_retired(ns):
    """No compatibility read remains. An older binary's marker file is simply
    ignored, which is safe because the state is disposable operational state
    rather than journal truth."""
    import _cctally_record as rec
    for name in ("_read_reset_zero_marker", "_arm_reset_zero_marker",
                 "_clear_reset_zero_marker", "_reset_zero_marker_path",
                 "_projection_read_reset_zero_marker",
                 "_projection_arm_reset_zero_marker",
                 "_projection_clear_reset_zero_marker"):
        assert not hasattr(rec, name), (
            f"{name} is still present, so the filesystem marker survived")

    # A stale marker file left by an older binary must not arm anything.
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date = _week_start_for(end_iso)
    _seed_baseline(ns, week_start_date=week_start_date, end_iso=end_iso,
                   pct=14.0)
    (ns["APP_DIR"] / "pending-reset-zero-7d").write_text(
        f"{week_start_date} {end_iso} 14.0 2026-05-14T10:00:00+00:00\n")

    assert ns["cmd_record_usage"](
        _record_usage_args(percent=0.0, resets_at=end_epoch)) == 0
    assert _events(ns) == [], (
        "a stale marker file confirmed a reset, so a compatibility read "
        "survived")
    assert len(_state_rows(ns)) == 1
