"""Statusline usage-persistence feeder + its marker/lock primitives.

Covers the new markers and helpers introduced for statusline-fed usage
persistence (spec 2026-07-17-usage-statusline-fallback-design):

- `_statusline_observe_*` liveness marker (mtime-based, mirrors the
  hook-tick throttle marker) — represents "the statusline is alive and
  feeding usage", touched even on a dedup no-op.
- `_oauth_backoff_*` deadline marker (absolute epoch stored in the file
  CONTENT, never the mtime) — the shared 429 cooldown deadline.
- The `_statusline_persist` feeder itself (Task 3) — a guarded, throttled,
  fork-detached feeder into `cmd_record_usage`, exercised through the
  hidden `sync_for_test=True` foreground path.

All path constants are pinned under the per-test tmp APP_DIR so nothing
touches the developer's real prod data dir.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import stat
import subprocess
import sys
import time

import pytest

import _lib_statusline_candidates as candidate_lib

from conftest import load_script, redirect_paths


_CCTALLY_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin" / "cctally"


def _iso(epoch):
    return (
        dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _status_input(app, *, seven_pct, seven_resets_epoch,
                  five_pct=None, five_resets_epoch=None, with_five_key=False,
                  model_id=None, session_id=None, transcript_path=None):
    """Parse a CC-shaped stdin payload into a StatuslineInput (as production
    does), so `_statusline_persist` sees exactly the real parsed object.

    ``model_id`` (optional) sets ``model.id`` on the payload so the
    pool-identity guard (spec D1) can be exercised end-to-end through the
    real parser (which surfaces ``model.id`` onto ``StatuslineInput.model_id``).
    """
    rl = {
        "seven_day": {
            "used_percentage": seven_pct,
            "resets_at": _iso(seven_resets_epoch),
        }
    }
    if with_five_key:
        rl["five_hour"] = {
            "used_percentage": five_pct if five_pct is not None else 0,
            "resets_at": _iso(five_resets_epoch) if five_resets_epoch is not None else None,
        }
    payload = {"rate_limits": rl}
    if session_id is not None:
        payload["session_id"] = session_id
    if transcript_path is not None:
        payload["transcript_path"] = transcript_path
    if model_id is not None:
        payload["model"] = {"id": model_id}
    return app._lib_statusline.parse_statusline_stdin(
        json.dumps(payload).encode()
    )


def _count_rows(app, table):
    """Row count for ``table``, or 0 if the table doesn't exist yet."""
    conn = app.open_db()
    try:
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def _rate_limits_payload(*, seven_pct, seven_resets_epoch,
                         five_pct=None, five_resets_epoch=None, model_id=None):
    rl = {
        "seven_day": {
            "used_percentage": seven_pct,
            "resets_at": _iso(seven_resets_epoch),
        }
    }
    if five_pct is not None:
        rl["five_hour"] = {
            "used_percentage": five_pct,
            "resets_at": _iso(five_resets_epoch),
        }
    payload = {"rate_limits": rl, "session_id": "sess-herd"}
    if model_id is not None:
        payload["model"] = {"id": model_id}
    return payload


def _newest_row(app):
    """The most-recent weekly_usage_snapshots row (sqlite3.Row) or None."""
    conn = app.open_db()
    try:
        return conn.execute(
            "SELECT source, weekly_percent, five_hour_percent, "
            "  five_hour_window_key, captured_at_utc "
            "FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def _snapshot_count(app):
    conn = app.open_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    finally:
        conn.close()


def _insert_snapshot(app, *, weekly_percent, weekly_resets_epoch, captured_epoch,
                     five_percent=None, five_resets_epoch=None, source="statusline"):
    """Seed one real stats row without invoking record-usage's HWM policy."""
    week_end = _iso(weekly_resets_epoch)
    week_start = _iso(weekly_resets_epoch - 7 * 86400)
    conn = app.open_db()
    try:
        five_key = (
            app._canonical_5h_window_key(five_resets_epoch)
            if five_resets_epoch is not None else None
        )
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, week_end_at, "
            " weekly_percent, page_url, source, payload_json, five_hour_percent, "
            " five_hour_resets_at, five_hour_window_key) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, '{}', ?, ?, ?)",
            (
                _iso(captured_epoch), week_start[:10], week_end[:10], week_start, week_end,
                weekly_percent, source, five_percent,
                _iso(five_resets_epoch) if five_resets_epoch is not None else None,
                five_key,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _pending_drop(app, *, percent=20):
    return candidate_lib.PendingDrop(
        canonical_key=1,
        reduced_percent=percent,
        first_seen_at=int(time.time()),
        kernel_stage="settling",
        attempts=0,
        contributors={},
        retry_signature=None,
    )


def _record_ns(*, percent=42.0, resets_in_days=3, five_percent=None,
               five_resets_in_hours=None, **extra):
    """Build a plausible cmd_record_usage Namespace.

    ``resets_at`` lands inside the weekly plausibility band [now-30d,
    now+8d]; the optional 5h reset lands inside [now-10m, now+6h].
    ``extra`` threads additional attrs (e.g. ``source="api"``)."""
    now = int(time.time())
    ns = {
        "percent": percent,
        "resets_at": str(now + resets_in_days * 86400),
        "five_hour_percent": five_percent,
        "five_hour_resets_at": (
            str(now + int(five_resets_in_hours * 3600))
            if five_resets_in_hours is not None else None
        ),
    }
    ns.update(extra)
    return argparse.Namespace(**ns)


@pytest.fixture
def app(monkeypatch, tmp_path):
    """Fresh cctally module with every runtime path pinned under tmp.

    ``redirect_paths`` pins the three new marker/lock path constants
    (STATUSLINE_OBSERVE_MARKER_PATH / STATUSLINE_PERSIST_LOCK_PATH /
    OAUTH_BACKOFF_MARKER_PATH) under the per-test tmp APP_DIR, so nothing
    here touches the developer's real prod data dir.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return sys.modules["cctally"]


# --- observation marker (liveness) -----------------------------------------


def test_observe_marker_absent_is_infinite(app):
    assert app._statusline_observe_age_seconds() == float("inf")


def test_observe_touch_then_age_small(app):
    app._statusline_observe_touch()
    assert app._statusline_observe_age_seconds() < 5.0


# --- candidate spool and selected-state split (#318 Task 2) ---------------


def _future_week():
    return int(time.time()) + 3 * 86400


def test_candidate_artifacts_stay_in_redirected_app_dir(app, tmp_path):
    parsed = _status_input(
        app,
        session_id="secret-session",
        transcript_path="/private/work/transcript.jsonl",
        seven_pct=20,
        seven_resets_epoch=_future_week(),
    )
    app._statusline_persist(parsed, sync_for_test=True)
    files = list(app.STATUSLINE_CANDIDATE_DIR.iterdir())
    assert len(files) == 1
    assert re.fullmatch(r"[0-9a-f]{64}\.json", files[0].name)
    assert stat.S_IMODE(app.STATUSLINE_CANDIDATE_DIR.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    body = files[0].read_text()
    assert "secret-session" not in body
    assert "/private/work" not in body
    assert files[0].is_relative_to(tmp_path)
    assert app.STATUSLINE_TRANSPORT_MARKER_PATH.is_relative_to(tmp_path)
    assert app.STATUSLINE_SELECTED_PATH.is_relative_to(tmp_path)


def test_reducer_ignores_incomplete_candidate_temp(app):
    app.STATUSLINE_CANDIDATE_DIR.mkdir(mode=0o700)
    temp = app.STATUSLINE_CANDIDATE_DIR / (
        "." + "a" * 64 + ".json.tmp.1." + "b" * 32
    )
    temp.write_text('{"schemaVersion":')
    assert app._load_candidate_spool(now_epoch=int(time.time())) == ()
    assert temp.exists()


def test_stale_first_fresh_second_is_order_independent(app):
    stale = _status_input(
        app, session_id="a", seven_pct=20, seven_resets_epoch=_future_week()
    )
    fresh = _status_input(
        app, session_id="b", seven_pct=24, seven_resets_epoch=_future_week()
    )
    app._statusline_persist(stale, sync_for_test=True)
    app._statusline_persist(fresh, sync_for_test=True)
    assert _newest_row(app)["weekly_percent"] == 24


def test_lock_loser_candidate_publishes_on_next_tick(app, monkeypatch):
    fresh = _status_input(
        app, session_id="fresh", seven_pct=24, seven_resets_epoch=_future_week()
    )
    real_lock = app._try_acquire_persist_lock
    monkeypatch.setattr(app, "_try_acquire_persist_lock", lambda: None)
    app._statusline_persist(fresh, sync_for_test=True)
    assert _snapshot_count(app) == 0
    monkeypatch.setattr(app, "_try_acquire_persist_lock", real_lock)
    stale = _status_input(
        app, session_id="stale", seven_pct=20, seven_resets_epoch=_future_week()
    )
    app._statusline_persist(stale, sync_for_test=True)
    assert _newest_row(app)["weekly_percent"] == 24


@pytest.mark.parametrize("first_five, second_five", [(20, 24), (24, 20)])
def test_same_raw_five_hour_candidates_preserve_each_session_percent(
        app, monkeypatch, first_five, second_five):
    """Two candidate identities may share a raw reset but not a percentage."""
    now = int(time.time())
    weekly_reset = now + 3 * 86400
    five_reset = now + 2 * 3600
    assert app.cmd_record_usage(_record_ns(
        percent=20, resets_in_days=3, five_percent=20, five_resets_in_hours=2,
    )) == 0

    real_lock = app._try_acquire_persist_lock
    monkeypatch.setattr(app, "_try_acquire_persist_lock", lambda: None)
    for session_id, five_pct in (("first", first_five), ("second", second_five)):
        app._statusline_persist(_status_input(
            app, session_id=session_id, seven_pct=20, seven_resets_epoch=weekly_reset,
            five_pct=five_pct, five_resets_epoch=five_reset, with_five_key=True,
        ), sync_for_test=True)
    monkeypatch.setattr(app, "_try_acquire_persist_lock", real_lock)
    app._statusline_reduce_and_publish()
    row = _newest_row(app)
    assert row["weekly_percent"] == 20
    assert row["five_hour_percent"] == 24


def test_weekly_advance_publishes_active_db_five_hour_companion(app):
    now = int(time.time())
    app._statusline_persist(_status_input(
        app, session_id="seed", seven_pct=20, seven_resets_epoch=now + 3 * 86400,
        five_pct=30, five_resets_epoch=now + 2 * 3600, with_five_key=True,
    ), sync_for_test=True)
    app._statusline_persist(_status_input(
        app, session_id="weekly", seven_pct=21, seven_resets_epoch=now + 3 * 86400,
    ), sync_for_test=True)
    row = _newest_row(app)
    assert row["weekly_percent"] == 21
    assert row["five_hour_percent"] == 30


def test_weekly_advance_drops_expired_db_five_hour_companion(app):
    now = int(time.time())
    app._statusline_persist(_status_input(
        app, session_id="seed", seven_pct=20, seven_resets_epoch=now + 3 * 86400,
        five_pct=30, five_resets_epoch=now, with_five_key=True,
    ), sync_for_test=True)
    app._statusline_persist(_status_input(
        app, session_id="weekly", seven_pct=21, seven_resets_epoch=now + 3 * 86400,
    ), sync_for_test=True)
    row = _newest_row(app)
    assert row["weekly_percent"] == 21
    assert row["five_hour_percent"] is None


def test_missing_control_reconciles_db_once_then_unchanged_tick_is_child_free(app, monkeypatch):
    now = int(time.time())
    assert app.cmd_record_usage(_record_ns(percent=50, resets_in_days=3)) == 0
    parsed = _status_input(
        app, session_id="equal", seven_pct=50, seven_resets_epoch=now + 3 * 86400,
    )
    app._statusline_persist(parsed, sync_for_test=True)
    assert app.STATUSLINE_SELECTED_PATH.exists()
    calls = []
    real_reduce = app._cctally_statusline._statusline_reduce_and_publish

    def counted_reduce():
        calls.append(True)
        return real_reduce()

    monkeypatch.setattr(app._cctally_statusline, "_statusline_reduce_and_publish", counted_reduce)
    app._statusline_persist(parsed, sync_for_test=True)
    assert calls == []


def test_legacy_db_write_forces_one_control_only_reconciliation_child(app, monkeypatch):
    now = int(time.time())
    assert app.cmd_record_usage(_record_ns(
        percent=50, resets_in_days=3, five_percent=10, five_resets_in_hours=2,
    )) == 0
    parsed = _status_input(
        app, session_id="equal", seven_pct=50, seven_resets_epoch=now + 3 * 86400,
    )
    app._statusline_persist(parsed, sync_for_test=True)

    # A legacy direct writer changes only the active 5h projection.  The
    # candidate remains equal to its stale 7d control view, so repair must be
    # control-only rather than calling the kernel again.
    assert app.cmd_record_usage(_record_ns(
        percent=50, resets_in_days=3, five_percent=11, five_resets_in_hours=2,
    )) == 0
    calls = []
    real_reduce = app._cctally_statusline._statusline_reduce_and_publish

    def counted_reduce():
        calls.append(True)
        return real_reduce()

    monkeypatch.setattr(app._cctally_statusline, "_statusline_reduce_and_publish", counted_reduce)
    app._statusline_persist(parsed, sync_for_test=True)
    assert calls == [True]
    control = app._read_control_state(now_epoch=int(time.time()))
    assert control is not None
    assert control.db_projection.five_hour is not None
    assert control.db_projection.five_hour.percent == 11

    calls.clear()
    app._statusline_persist(parsed, sync_for_test=True)
    assert calls == []


def test_authoritative_observation_clears_only_its_axis_pending_drop(app):
    assert app.cmd_record_usage(_record_ns(percent=50, resets_in_days=3, source="api")) == 0
    projection = app._read_db_projection_stable()
    pending = _pending_drop(app)
    app._write_control_state(candidate_lib.ControlState(
        projection, {"fiveHour": pending, "sevenDay": pending},
    ))
    result = app._authoritative_record_usage(
        _record_ns(percent=50, resets_in_days=3, source="api"), {"sevenDay"}
    )
    assert result.status == "ok"
    control = app._read_control_state(now_epoch=int(time.time()))
    assert control is not None
    assert control.pending_drops["sevenDay"] is None
    assert control.pending_drops["fiveHour"] is not None


def test_authoritative_equal_fifty_clears_an_actual_pending_twenty_generation(app):
    now = int(time.time())
    assert app.cmd_record_usage(_record_ns(percent=50, resets_in_days=3, source="api")) == 0
    lower = _status_input(
        app, session_id="lower", seven_pct=20, seven_resets_epoch=now + 3 * 86400,
    )
    app._statusline_persist(lower, sync_for_test=True)
    pending_control = app._read_control_state(now_epoch=int(time.time()))
    assert pending_control is not None
    pending = pending_control.pending_drops["sevenDay"]
    assert pending is not None and pending.reduced_percent == 20

    result = app._authoritative_record_usage(
        _record_ns(percent=50, resets_in_days=3, source="api"), {"sevenDay"}
    )
    assert result.status == "ok"
    control = app._read_control_state(now_epoch=int(time.time()))
    assert control is not None and control.pending_drops["sevenDay"] is None


def test_empty_spool_reconciles_expired_pending_drop(app):
    assert app.cmd_record_usage(_record_ns(percent=50, resets_in_days=3)) == 0
    projection = app._read_db_projection_stable()
    app._write_control_state(candidate_lib.ControlState(
        projection, {"fiveHour": None, "sevenDay": _pending_drop(app)},
    ))
    decision = app._statusline_reduce_and_publish()
    assert decision is not None and decision.action == "WRITE_CONTROL"
    control = app._read_control_state(now_epoch=int(time.time()))
    assert control is not None and control.pending_drops["sevenDay"] is None


def test_reset_zero_keeps_armed_consensus_until_the_next_revalidated_tick(
        app, monkeypatch):
    """The first zero only arms cmd_record_usage's existing debounce marker."""
    now = int(time.time())
    assert app.cmd_record_usage(_record_ns(percent=20, resets_in_days=3)) == 0
    parsed = _status_input(
        app, session_id="zero", seven_pct=0, seven_resets_epoch=now + 3 * 86400,
    )
    clock = {"value": now}
    monkeypatch.setattr(app._cctally_statusline.time, "time", lambda: clock["value"])

    app._statusline_persist(parsed, sync_for_test=True)  # settle baseline
    clock["value"] += 1
    app._statusline_persist(parsed, sync_for_test=True)  # first kernel attempt arms zero
    assert _newest_row(app)["weekly_percent"] == 20
    control = app._read_control_state(now_epoch=clock["value"])
    assert control is not None
    pending = control.pending_drops["sevenDay"]
    assert pending is not None and pending.kernel_stage == "zero_armed"

    clock["value"] += 1
    app._statusline_persist(parsed, sync_for_test=True)  # revalidated second attempt commits
    assert _newest_row(app)["weekly_percent"] == 0
    control = app._read_control_state(now_epoch=clock["value"])
    assert control is not None and control.pending_drops["sevenDay"] is None


def test_projection_keeps_active_five_hour_when_newer_weekly_only_row_exists(app):
    now = int(time.time())
    reset = now + 3 * 86400
    five_reset = now + 2 * 3600
    _insert_snapshot(
        app, weekly_percent=20, weekly_resets_epoch=reset, captured_epoch=now - 2,
        five_percent=30, five_resets_epoch=five_reset,
    )
    _insert_snapshot(
        app, weekly_percent=21, weekly_resets_epoch=reset, captured_epoch=now - 1,
    )
    projection = app._read_db_projection_once()
    assert projection.seven_day is not None and projection.seven_day.percent == 21
    assert projection.five_hour is not None and projection.five_hour.percent == 30


def test_projection_normalizes_weekly_jitter_and_uses_capture_order_not_row_id(app):
    now = int(time.time())
    reset = now + 3 * 86400
    # Insert the newer capture first, then an older capture with a later row id.
    _insert_snapshot(
        app, weekly_percent=24, weekly_resets_epoch=reset + 40, captured_epoch=now - 1,
    )
    _insert_snapshot(
        app, weekly_percent=20, weekly_resets_epoch=reset + 5, captured_epoch=now - 2,
    )
    projection = app._read_db_projection_once()
    assert projection.seven_day is not None
    assert projection.seven_day.percent == 24
    expected = int(app._normalize_week_boundary_dt(
        dt.datetime.fromtimestamp(reset + 40, tz=dt.timezone.utc)
    ).timestamp())
    assert projection.seven_day.canonical_key == expected


def test_projection_selects_latest_post_credit_weekly_and_five_hour_segments(app):
    now = int(time.time())
    reset = now + 3 * 86400
    five_reset = now + 2 * 3600
    _insert_snapshot(
        app, weekly_percent=50, weekly_resets_epoch=reset, captured_epoch=now - 20,
        five_percent=30, five_resets_epoch=five_reset,
    )
    conn = app.open_db()
    try:
        credit_at = _iso(now - 10)
        conn.execute(
            "INSERT INTO weekly_credit_floors "
            "(week_start_date, effective_at_utc, observed_pre_credit_pct, applied_at_utc) "
            "VALUES (?, ?, ?, ?)",
            (_iso(reset - 7 * 86400)[:10], credit_at, 50, credit_at),
        )
        conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, prior_percent, post_percent, effective_reset_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (credit_at, app._canonical_5h_window_key(five_reset), 30, 5, credit_at),
        )
        conn.commit()
    finally:
        conn.close()
    _insert_snapshot(
        app, weekly_percent=20, weekly_resets_epoch=reset, captured_epoch=now - 1,
        five_percent=5, five_resets_epoch=five_reset,
    )
    projection = app._read_db_projection_once()
    assert projection.seven_day is not None and projection.seven_day.percent == 20
    assert projection.seven_day.reset_generation > 0
    assert projection.five_hour is not None and projection.five_hour.percent == 5
    assert projection.five_hour.reset_generation > 0


def test_unchanged_statusline_candidate_touches_transport_not_selected(app):
    parsed = _status_input(
        app, session_id="same", seven_pct=20, seven_resets_epoch=_future_week()
    )
    app._statusline_persist(parsed, sync_for_test=True)
    selected = app.STATUSLINE_OBSERVE_MARKER_PATH
    selected.unlink()
    app._statusline_persist(parsed, sync_for_test=True)
    assert app._statusline_transport_age_seconds() < 5
    assert not selected.exists()


# --- oauth backoff deadline marker -----------------------------------------


def test_backoff_absent_is_zero(app):
    assert app._oauth_backoff_remaining_seconds() == 0.0


def test_backoff_set_and_remaining(app):
    app._oauth_backoff_set(time.time() + 120)
    assert 100 < app._oauth_backoff_remaining_seconds() <= 120


def test_backoff_never_shortens(app):
    now = time.time()
    app._oauth_backoff_set(now + 300)
    app._oauth_backoff_set(now + 30)  # shorter — must be ignored
    assert app._oauth_backoff_remaining_seconds() > 200


def test_backoff_clear(app):
    app._oauth_backoff_set(time.time() + 120)
    app._oauth_backoff_clear()
    assert app._oauth_backoff_remaining_seconds() == 0.0


# --- source labeling (Task 2) ----------------------------------------------


def test_default_record_source_is_statusline(app):
    """cmd_record_usage with no `source` attr defaults to 'statusline'
    (preserves the public CLI's current behavior)."""
    assert app.cmd_record_usage(_record_ns()) == 0
    row = _newest_row(app)
    assert row is not None
    assert row["source"] == "statusline"


def test_record_source_explicit_api(app):
    """An explicit source='api' on the args is written verbatim — proves the
    hard-coded 'statusline' is gone and OAuth rows can be labeled correctly."""
    assert app.cmd_record_usage(_record_ns(source="api")) == 0
    row = _newest_row(app)
    assert row is not None
    assert row["source"] == "api"


def test_backoff_stores_epoch_in_content_not_mtime(app):
    """The deadline is a FUTURE absolute epoch stored in the file's text
    content — NOT its mtime. Future-dating the mtime would corrupt the
    hook-tick throttle-age reading (which IS mtime-based). Guard that the
    marker file's mtime stays ~now while its content holds the future
    epoch."""
    import _cctally_core

    deadline = time.time() + 3600
    app._oauth_backoff_set(deadline)
    path = _cctally_core.OAUTH_BACKOFF_MARKER_PATH
    st = path.stat()
    # mtime is ~now, not the +1h deadline.
    assert abs(st.st_mtime - time.time()) < 60
    stored = float(path.read_text().strip())
    assert abs(stored - deadline) < 1.0


# --- statusline persist feeder (Task 3) ------------------------------------


def test_persist_writes_snapshot_and_milestone(app):
    """A fresh 7d reading persists a snapshot AND crosses a percent
    milestone through the unchanged cmd_record_usage kernel."""
    now = int(time.time())
    parsed = _status_input(app, seven_pct=42.0, seven_resets_epoch=now + 3 * 86400)
    app._statusline_persist(parsed, sync_for_test=True)

    row = _newest_row(app)
    assert row is not None
    assert row["source"] == "statusline"
    assert abs(row["weekly_percent"] - 42.0) < 1e-6

    conn = app.open_db()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM percent_milestones WHERE percent_threshold = 42"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n >= 1


def test_same_value_render_does_not_touch_selected_marker_on_dedup(app):
    """Unchanged stdin stays transport-lively but does not claim selection."""
    import _cctally_core

    now = int(time.time())
    parsed = _status_input(
        app, session_id="same-value", seven_pct=42.0,
        seven_resets_epoch=now + 3 * 86400,
    )

    app._statusline_persist(parsed, sync_for_test=True)  # inserts row #1
    assert _snapshot_count(app) == 1

    # Remove the selected marker; the same candidate must not recreate it.
    _cctally_core.STATUSLINE_OBSERVE_MARKER_PATH.unlink()
    assert app._statusline_observe_age_seconds() == float("inf")

    app._statusline_persist(parsed, sync_for_test=True)
    assert _snapshot_count(app) == 1
    assert app._statusline_observe_age_seconds() == float("inf")
    assert app._statusline_transport_age_seconds() < 5.0


def test_no_rate_limits_is_noop(app):
    """Absent rate_limits (older CC / CC not supplying them) → clean no-op."""
    parsed = app._lib_statusline.parse_statusline_stdin(b"{}")
    app._statusline_persist(parsed, sync_for_test=True)
    assert _snapshot_count(app) == 0
    # No liveness claim either.
    assert app._statusline_observe_age_seconds() == float("inf")


def test_inactive_5h_null_pair_persists_weekly_drops_5h(app):
    """An inactive 5h window arrives as {used_percentage:0, resets_at:null}.
    Pair-gate drops the whole 5h pair; the weekly snapshot still writes."""
    now = int(time.time())
    parsed = _status_input(
        app, seven_pct=33.0, seven_resets_epoch=now + 3 * 86400,
        five_pct=0, five_resets_epoch=None, with_five_key=True,
    )
    # Parser surfaces 5h pct as 0.0 but resets_at as None (null).
    assert parsed.rate_limits_5h_pct == 0.0
    assert parsed.rate_limits_5h_resets_at is None

    app._statusline_persist(parsed, sync_for_test=True)
    row = _newest_row(app)
    assert row is not None
    assert row["weekly_percent"] is not None
    assert row["five_hour_percent"] is None


def test_missing_seven_day_resets_is_noop(app):
    """A 7d percent with no 7d resets_at is not a usable reading → no-op."""
    payload = {"rate_limits": {"seven_day": {"used_percentage": 40.0}}}
    parsed = app._lib_statusline.parse_statusline_stdin(json.dumps(payload).encode())
    assert parsed.rate_limits_7d_pct == 40.0
    assert parsed.rate_limits_7d_resets_at is None
    app._statusline_persist(parsed, sync_for_test=True)
    assert _snapshot_count(app) == 0


# --- concurrent-render herd (Task 3, P1-2 regression) ----------------------


def _count_snapshots(db_path):
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        # table not created yet
        return 0
    finally:
        conn.close()


def test_concurrent_renders_yield_at_most_one_snapshot(tmp_path):
    """P1-2 regression (multi-process / REAL fork path). N simultaneous
    `cctally statusline` renders sharing one data dir must produce AT MOST
    one snapshot: the cross-process persist lock + the child's under-lock
    marker re-check serialize the detached feeders so the herd cannot each
    fork-and-insert a duplicate row.

    Uses real subprocesses (the herd is cross-process) and polls the
    detached feeders' DB to quiescence (per the hook-tick completion-
    condition pattern) so no detached child outlives this test."""
    tmp_home = tmp_path / "home"
    tmp_data = tmp_path / "data"
    (tmp_home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    tmp_data.mkdir(parents=True, exist_ok=True)
    db_path = tmp_data / "stats.db"

    env = dict(os.environ)
    env["HOME"] = str(tmp_home)
    env["CCTALLY_DATA_DIR"] = str(tmp_data)
    env["CLAUDE_CONFIG_DIR"] = str(tmp_home / ".claude")
    env.pop("CCTALLY_AS_OF", None)

    now = int(time.time())
    payload = json.dumps(_rate_limits_payload(
        seven_pct=57.0, seven_resets_epoch=now + 3 * 86400,
        five_pct=12.0, five_resets_epoch=now + 2 * 3600,
    )).encode()

    n_procs = 5
    procs = [
        subprocess.Popen(
            # No --color flag: stdout is a pipe (not a TTY) so color is
            # auto-off; passing a value to the store_true --color errors.
            [sys.executable, str(_CCTALLY_BIN), "statusline"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env,
        )
        for _ in range(n_procs)
    ]
    for p in procs:
        try:
            p.stdin.write(payload)
            p.stdin.close()
        except BrokenPipeError:
            pass
    for p in procs:
        p.wait(timeout=30)

    # The winning render's persist runs in a DETACHED child that outlives the
    # `statusline` process, so poll the DB until the count is stable (the
    # feeder has quiesced) before asserting.
    deadline = time.time() + 25.0
    last = _count_snapshots(db_path)
    stable = 0
    while time.time() < deadline:
        time.sleep(0.4)
        cur = _count_snapshots(db_path)
        if cur == last:
            stable += 1
            if stable >= 5 and cur >= 1:
                break
        else:
            stable = 0
            last = cur
    final = _count_snapshots(db_path)
    assert final <= 1, f"thundering herd produced {final} snapshots (want <= 1)"
    assert final == 1, f"expected exactly one snapshot from the herd, got {final}"


def test_bracketed_context_variant_persists_global_rate_limits(app):
    """`model.id` context metadata must not suppress the global 5h/7d axes.

    Claude Code exposes model-scoped quotas separately; the top-level
    `five_hour` and `seven_day` fields remain the account-wide usage windows.
    """
    now = int(time.time())
    parsed = _status_input(
        app, seven_pct=41.0, seven_resets_epoch=now + 3 * 86400,
        five_pct=42.0, five_resets_epoch=now + 2 * 3600, with_five_key=True,
        model_id="claude-opus-4-8[1m]",
    )
    app._statusline_persist(parsed, sync_for_test=True)

    row = _newest_row(app)
    assert row is not None
    assert row["source"] == "statusline"
    assert row["weekly_percent"] == pytest.approx(41.0)
    assert row["five_hour_percent"] == pytest.approx(42.0)
    assert app._statusline_observe_age_seconds() < 5.0


def test_normal_model_id_still_persists(app):
    """A normal model id persists exactly as before."""
    now = int(time.time())
    parsed = _status_input(
        app, seven_pct=27.0, seven_resets_epoch=now + 3 * 86400,
        model_id="claude-opus-4-8",
    )
    app._statusline_persist(parsed, sync_for_test=True)

    row = _newest_row(app)
    assert row is not None
    assert row["source"] == "statusline"
    assert abs(row["weekly_percent"] - 27.0) < 1e-6
    # Marker touched → the regular pipeline is alive.
    assert app._statusline_observe_age_seconds() < 5.0


def test_absent_model_id_still_persists(app):
    """No `model.id` at all (older CC payloads) is NOT a variant match →
    persist proceeds normally."""
    now = int(time.time())
    parsed = _status_input(app, seven_pct=27.0, seven_resets_epoch=now + 3 * 86400)
    assert parsed.model_id is None
    app._statusline_persist(parsed, sync_for_test=True)
    assert _count_rows(app, "weekly_usage_snapshots") == 1


# --- statusline-driven OAuth freshness -----------------------------------


def test_statusline_tick_fetches_authoritative_usage_within_one_cycle(
        app, monkeypatch):
    """A live statusline must not wait five minutes when its payload stalls.

    Claude can keep replaying an unchanged ``rate_limits`` object after the
    provider's authoritative 5h/7d percentages have advanced.  The periodic
    statusline tick therefore owns a bounded OAuth confirmation at the same
    cadence, while the shared hook throttle keeps it account-wide.
    """
    import _cctally_core

    interval = float(_cctally_core.STATUSLINE_OAUTH_POLL_SECONDS)
    assert interval <= 30.0
    monkeypatch.setattr(app, "_statusline_observe_age_seconds", lambda: interval + 1)
    monkeypatch.setattr(app, "_oauth_backoff_remaining_seconds", lambda: 0.0)

    calls = []

    def refresh(*, throttle_seconds):
        calls.append(throttle_seconds)
        return "ok(7d=7,5h=2)", {}

    monkeypatch.setattr(app, "_hook_tick_oauth_refresh", refresh)
    app._statusline_oauth_tick(sync_for_test=True)

    assert calls == [interval]
    assert app._hook_tick_throttle_age_seconds() < 5.0


def test_statusline_oauth_tick_is_account_wide_throttled(app, monkeypatch):
    """Concurrent/session-local 30s timers still produce one account poll."""
    import _cctally_core

    interval = float(_cctally_core.STATUSLINE_OAUTH_POLL_SECONDS)
    monkeypatch.setattr(app, "_statusline_observe_age_seconds", lambda: interval + 1)
    monkeypatch.setattr(app, "_oauth_backoff_remaining_seconds", lambda: 0.0)
    calls = []
    monkeypatch.setattr(
        app,
        "_hook_tick_oauth_refresh",
        lambda *, throttle_seconds: (
            calls.append(throttle_seconds) or "ok(7d=7,5h=2)",
            {},
        ),
    )

    app._statusline_oauth_tick(sync_for_test=True)
    app._statusline_oauth_tick(sync_for_test=True)

    assert calls == [interval]


def test_statusline_oauth_tick_honors_selected_freshness_and_backoff(
        app, monkeypatch):
    """Fresh selected data and a 429 deadline both suppress timer polling."""
    import _cctally_core

    interval = float(_cctally_core.STATUSLINE_OAUTH_POLL_SECONDS)
    calls = []
    monkeypatch.setattr(
        app,
        "_hook_tick_oauth_refresh",
        lambda *, throttle_seconds: (calls.append(throttle_seconds), None),
    )

    monkeypatch.setattr(app, "_statusline_observe_age_seconds", lambda: interval - 1)
    monkeypatch.setattr(app, "_oauth_backoff_remaining_seconds", lambda: 0.0)
    app._statusline_oauth_tick(sync_for_test=True)

    monkeypatch.setattr(app, "_statusline_observe_age_seconds", lambda: interval + 1)
    monkeypatch.setattr(app, "_oauth_backoff_remaining_seconds", lambda: 60.0)
    app._statusline_oauth_tick(sync_for_test=True)

    assert calls == []


def test_statusline_oauth_tick_never_waits_for_another_session(app, monkeypatch):
    """A concurrent account poll must not add latency to statusline output."""
    import fcntl
    import _cctally_core

    interval = float(_cctally_core.STATUSLINE_OAUTH_POLL_SECONDS)
    monkeypatch.setattr(app, "_statusline_observe_age_seconds", lambda: interval + 1)
    monkeypatch.setattr(app, "_oauth_backoff_remaining_seconds", lambda: 0.0)
    monkeypatch.setattr(app, "_hook_tick_throttle_age_seconds", lambda: interval + 1)
    monkeypatch.setattr(
        app,
        "_hook_tick_oauth_refresh",
        lambda **kwargs: pytest.fail("busy lock must suppress the refresh"),
    )

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(
        _cctally_core.HOOK_TICK_THROTTLE_LOCK_PATH,
        os.O_WRONLY | os.O_CREAT,
        0o644,
    )
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        app._statusline_oauth_tick(sync_for_test=True)
        elapsed = time.monotonic() - started
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert elapsed < 0.1
