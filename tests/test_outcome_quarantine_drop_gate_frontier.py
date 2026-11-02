"""#825/#832 fix round B, finding P1-2 (Codex fix-round-1 answer Q3): the
statusline drop gate proves journal coverage through a captured frontier.

The detached reducer captures the publication's required journal frontier
after its legacy-spool rescan. A drop below the database baseline is allowed
only when ONE validated stats snapshot proves the applied cursor and the
admission/quarantine state cover the journal through that frontier (and the
certificate / `authorityCommit` checks pass). An opportunistic ingest that
could not run (`ran=False`: another process holds `journal.ingest.lock`) is
never evidence by itself. The final observation append is bound to the checked
high water, so an intervening journal append defers the publication to a
recovery that keeps its identity.
"""
from __future__ import annotations

import fcntl
import os

import pytest

import _outcome_quarantine_trace as T
import test_outcome_quarantine_control_f15 as F15

WEEK = F15.WEEK
T0 = F15.T0


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


def _hold_ingest_lock():
    """A concurrent ingester: hold `journal.ingest.lock` on a separate open
    file description, so this process's opportunistic ingest is busy."""
    import _cctally_core as core
    fd = os.open(str(core.JOURNAL_INGEST_LOCK_PATH), os.O_RDWR | os.O_CREAT,
                 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _release(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _drain_and_assert_covered(app):
    """Another ingester demonstrably consumed the whole journal: the applied
    cursor equals the journal high water."""
    import _cctally_journal as jr
    for _ in range(3):
        jr.run_stats_ingest(mode="authoritative")
    conn = app.open_db()
    try:
        row = conn.execute(
            "SELECT applied_segment, applied_offset FROM journal_cursor "
            "WHERE id = 1").fetchone()
    finally:
        conn.close()
    assert tuple(row) == jr.journal_high_water()


def _publications(name):
    import _cctally_core as core
    import _cctally_journal as jr
    import _lib_journal as J
    out = []
    for segment in jr.list_segments():
        for line in (core.JOURNAL_DIR / segment).read_bytes().splitlines():
            rec = J.decode_line(line)
            payload = (rec or {}).get("payload") or {}
            if (rec and rec.get("t") == "obs" and "publication_id" in payload
                    and (rec.get("account") or "unattributed") == F15._key(name)):
                out.append(payload)
    return out


def test_busy_lock_with_a_covered_frontier_still_publishes_the_drop(
        app, monkeypatch):
    """Non-vacuity: a busy ingest lock alone never withholds. Another
    ingester covered the whole journal, so the drop publishes by consensus."""
    clock = F15.Clock(app, monkeypatch, T0)
    F15._seed(app, clock, "A", 70, week_reset=WEEK)
    _drain_and_assert_covered(app)
    fd = _hold_ingest_lock()
    try:
        F15._drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK, 6)
    finally:
        _release(fd)
    assert F15._published("A", 20)


def test_a_locally_appended_activity_op_withholds_under_a_busy_lock(
        app, monkeypatch):
    """The RED trace: this run's rescan appends `legacy_spool_activity` while
    a concurrent ingester holds the lock. The index never folded the trigger,
    so a gate that reads quarantine state without proving coverage publishes
    a drop the uniform-current reference cannot reach."""
    clock = F15.Clock(app, monkeypatch, T0)
    F15._seed(app, clock, "A", 70, week_reset=WEEK)
    _drain_and_assert_covered(app)
    F15._write_legacy_candidate()
    fd = _hold_ingest_lock()
    try:
        F15._drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK, 6)
    finally:
        _release(fd)
    assert F15._spool_activity_ops(), "the rescan journaled the legacy write"
    assert not F15._published("A", 20)


def test_an_unconsumed_excluded_record_withholds_under_a_busy_lock(
        app, monkeypatch):
    """An older process appended an (excluded) unstamped Claude reading that
    no ingester has folded yet: the frontier is not covered, so the drop is
    withheld."""
    clock = F15.Clock(app, monkeypatch, T0)
    F15._seed(app, clock, "A", 70, week_reset=WEEK)
    _drain_and_assert_covered(app)
    T.raw_append([T.legacy_record(at=T.iso(clock.value), weekly=65,
                                  week_end=T.iso(WEEK))])
    fd = _hold_ingest_lock()
    try:
        F15._drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK, 6)
    finally:
        _release(fd)
    assert not F15._published("A", 20)


def test_an_append_after_the_check_defers_and_keeps_the_publication_identity(
        app, monkeypatch):
    """A journal append between the coverage check and the observation append
    defeats the expected-high-water binding: nothing is published on that
    tick, the frozen plan survives, and the next tick's recovery revalidates
    and publishes the SAME publication identity."""
    import _cctally_journal as jr
    import _cctally_statusline as sl
    import _lib_journal as J
    clock = F15.Clock(app, monkeypatch, T0)
    F15._seed(app, clock, "A", 70, week_reset=WEEK)
    real = app.cmd_record_usage
    interfered = []

    def interfere(*args, **kwargs):
        if kwargs.get("publication") is not None and not interfered:
            # An unrelated current writer lands after the gate's check.
            jr.append_record(J.make_op(
                at=T.iso(clock.value), src="test",
                payload={"kind": "test_unrelated_append"}))
            interfered.append(kwargs["publication"]["publicationId"])
        return real(*args, **kwargs)

    monkeypatch.setattr(app, "cmd_record_usage", interfere)
    for _ in range(6):
        F15._drive(app, clock, "A", (("one", 20), ("two", 20)), WEEK, 1)
        if interfered:
            break
    assert interfered, "a drop plan reached the record kernel"
    assert not F15._published("A", 20)
    pending = sl._read_transition(F15._key("A"), now_epoch=clock.value)
    assert pending.publication is not None
    assert pending.publication["publicationId"] == interfered[0]
    sl._statusline_reduce_and_publish(nudge_sink=lambda: None)
    published = [p for p in _publications("A") if p["weekly_percent"] == 20.0]
    assert [p["publication_id"] for p in published] == interfered


# --------------------------------------------------------------------------
# #948 spec 6.8: the drop gate is per axis
# --------------------------------------------------------------------------

def _spool_two_axis(app, clock, name, session, weekly, week_reset, five,
                    five_reset):
    import json
    import _cctally_statusline as sl
    T.identity(name)
    parsed = app._lib_statusline.parse_statusline_stdin(json.dumps({
        "session_id": session,
        "rate_limits": {
            "seven_day": {"used_percentage": weekly,
                          "resets_at": T.iso(week_reset)},
            "five_hour": {"used_percentage": five,
                          "resets_at": T.iso(five_reset)}},
    }).encode())
    sl._write_candidate(sl._candidate_from_input(parsed, received_at=clock.value))


def _drive_two_axis(app, clock, name, reporters, week_reset, five_reset,
                    ticks):
    import _cctally_statusline as sl
    for _ in range(ticks):
        for session, weekly, five in reporters:
            _spool_two_axis(app, clock, name, session, weekly, week_reset,
                            five, five_reset)
        sl._statusline_reduce_and_publish(nudge_sink=lambda: None)
        clock.advance(F15.TICK)


@pytest.mark.parametrize("coverage", ["weekly-only", "both"])
def test_a_released_weekly_axis_publishes_its_drop_while_five_hour_withholds(
        app, monkeypatch, coverage):
    """Detects an all-or-nothing drop gate after a partial recovery: a
    weekly-only OAuth coverage and a fresh weekly period release weekly
    alone, so a weekly drop publishes by consensus while a five-hour drop of
    the same account stays withheld (its axis has no coverage). The two-axis
    control proves the five-hour drop publishes once its axis is released."""
    import argparse
    import _cctally_statusline as sl
    clock = F15.Clock(app, monkeypatch, T0)
    F15._seed(app, clock, "A", 30, week_reset=T0 + 86400, five=10,
              five_reset=T0 + 3600)
    F15._excluded_trigger(app, clock, T0 + 86400, T0 + 3600)
    clock.advance(F15.TICK)
    T.identity("A")
    result = sl._authoritative_record_usage(
        argparse.Namespace(
            percent=31, resets_at=str(T0 + 86400),
            five_hour_percent=None if coverage == "weekly-only" else 11,
            five_hour_resets_at=(None if coverage == "weekly-only"
                                 else str(T0 + 3600)),
            source="api"),
        ("sevenDay",) if coverage == "weekly-only" else ("sevenDay", "fiveHour"),
        binding=sl._capture_binding(credential="token-A"),
        nudge_dashboard=False)
    assert result.status == "ok", result
    axes = (("sevenDay",) if coverage == "weekly-only"
            else ("sevenDay", "fiveHour"))
    assert [tuple(op["payload"]["axes"])
            for op in T.ops_of_kind("outcome_authority_completion")] == [axes]
    clock.advance(86400 + 3600)
    new_week = clock.value + 7 * 86400
    five_reset = clock.value + 3 * 3600
    F15._seed(app, clock, "A", 70, week_reset=new_week, five=10,
              five_reset=five_reset, source="statusline")
    assert T.axis_allowed(app, "A") == {
        "sevenDay": True, "fiveHour": coverage == "both"}
    clock.advance(F15.TICK)
    # A five-hour-only drop: withheld unless its axis is released.
    _drive_two_axis(app, clock, "A", (("one", 70, 2), ("two", 70, 2)),
                    new_week, five_reset, 1 + 2 * F15.DEADLINE // F15.TICK)
    five_drops = [p for p in _publications("A")
                  if p.get("five_hour_percent") == 2.0]
    assert bool(five_drops) == (coverage == "both"), _publications("A")
    # A weekly drop with the five-hour value unchanged: published.
    _drive_two_axis(app, clock, "A", (("one", 20, 10), ("two", 20, 10)),
                    new_week, five_reset, 1 + 2 * F15.DEADLINE // F15.TICK)
    assert F15._published("A", 20)
