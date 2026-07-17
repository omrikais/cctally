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
import sqlite3
import subprocess
import sys
import time

import pytest

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
                  model_id=None):
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
    if model_id is not None:
        payload["model"] = {"id": model_id}
    return app._lib_statusline.parse_statusline_stdin(
        json.dumps(payload).encode()
    )


def _count_rows(app, table):
    """Row count for ``table``, or 0 if the table doesn't exist yet.

    Used by the pool-guard blast-radius assertions: a fired guard means the
    record kernel never ran, so these tables are either empty or never
    created."""
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


def test_same_value_render_touches_observe_marker_on_dedup(app, monkeypatch):
    """P1-1 regression: an unchanged-value render is a dedup no-op in
    cmd_record_usage (no new row), but the persist feeder MUST still touch
    the observation marker so the throttles/backfill track liveness rather
    than snapshot age."""
    import _cctally_core

    # Neutralize the throttle so both persists run their kernel step.
    monkeypatch.setattr(_cctally_core, "STATUSLINE_PERSIST_THROTTLE_SECONDS", 0.0)

    now = int(time.time())
    parsed = _status_input(app, seven_pct=42.0, seven_resets_epoch=now + 3 * 86400)

    app._statusline_persist(parsed, sync_for_test=True)  # inserts row #1
    assert _snapshot_count(app) == 1

    # Age the marker away so we can prove the SECOND persist re-touches it.
    _cctally_core.STATUSLINE_OBSERVE_MARKER_PATH.unlink()
    assert app._statusline_observe_age_seconds() == float("inf")

    app._statusline_persist(parsed, sync_for_test=True)  # dedup: no new row
    assert _snapshot_count(app) == 1  # cmd_record_usage suppressed the insert
    assert app._statusline_observe_age_seconds() < 5.0  # marker touched anyway


def test_throttle_second_render_within_window_is_noop(app):
    """Within the persist throttle window the second render is a pure no-op
    (no kernel call, no new row) — keyed off the observation marker."""
    now = int(time.time())
    p1 = _status_input(app, seven_pct=10.0, seven_resets_epoch=now + 3 * 86400)
    app._statusline_persist(p1, sync_for_test=True)
    n = _snapshot_count(app)
    assert n == 1

    # A DIFFERENT value that WOULD insert if it ran — proves the throttle,
    # not dedup, suppressed it.
    p2 = _status_input(app, seven_pct=11.0, seven_resets_epoch=now + 3 * 86400)
    app._statusline_persist(p2, sync_for_test=True)
    assert _snapshot_count(app) == n


def test_throttle_retuned_to_25_seconds_boundary(app):
    """The persist throttle is 25s (retuned from 60 for #311's 30s
    statusLine.refreshInterval timer — interval MUST exceed throttle so a
    steady tick isn't beat-frequency-throttled). A marker aged >= 25s lets a
    persist through; < 25s skips.

    Non-vacuity: the >= 25s direction (step b) is RED by construction against
    the pre-#311 60.0 constant — a 26s-old marker would still throttle there
    (26 < 60) and no row would be written."""
    now = int(time.time())
    marker = app.STATUSLINE_OBSERVE_MARKER_PATH
    p = _status_input(app, seven_pct=10.0, seven_resets_epoch=now + 3 * 86400)

    # (a) marker aged 24s (< 25) → a would-insert reading is throttled.
    app._statusline_observe_touch()
    os.utime(marker, (time.time() - 24, time.time() - 24))
    app._statusline_persist(p, sync_for_test=True)
    assert _snapshot_count(app) == 0  # throttled

    # (b) marker aged 26s (>= 25) → the same reading now persists. Would have
    #     been throttled under the old 60.0 constant.
    os.utime(marker, (time.time() - 26, time.time() - 26))
    app._statusline_persist(p, sync_for_test=True)
    assert _snapshot_count(app) == 1  # proceeded


def test_slow_record_skips_one_tick_then_self_corrects(app):
    """Cadence qualifier (spec D2 / Codex R1 F2): the observation marker is
    touched at persist COMPLETION, so a tick observes marker age =
    interval - d where d is the previous record's duration. With interval=30
    and throttle=25, a record slower than 5s (interval - throttle) makes the
    NEXT tick throttle (age 30-d < 25); the tick AFTER (age ~60-d) proceeds.
    A slow record therefore degrades cadence to skip-one-tick (~60s) and
    self-corrects — it never wedges. (Exact-30s is deliberately NOT an
    acceptance criterion; no attempt-start marker exists.)"""
    now = int(time.time())
    marker = app.STATUSLINE_OBSERVE_MARKER_PATH
    p = _status_input(app, seven_pct=15.0, seven_resets_epoch=now + 3 * 86400)

    d = 6.0  # previous record took 6s (> interval - throttle = 5s)
    # Next tick (interval=30s after the previous tick started): age 30-d=24s.
    app._statusline_observe_touch()
    os.utime(marker, (time.time() - (30 - d), time.time() - (30 - d)))
    app._statusline_persist(p, sync_for_test=True)
    assert _snapshot_count(app) == 0  # one tick skipped

    # The following tick (2*interval=60s after that start): age 60-d=54s.
    os.utime(marker, (time.time() - (60 - d), time.time() - (60 - d)))
    app._statusline_persist(p, sync_for_test=True)
    assert _snapshot_count(app) == 1  # self-corrected


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


# --- pool-identity guard predicate (Task 1 / spec D1) ----------------------


@pytest.mark.parametrize(
    "model_id,expected",
    [
        (None, False),               # missing / null model id → persist proceeds
        ("", False),                 # empty string → not a variant
        ("claude-opus-4-8", False),  # bare default-pool id → persist proceeds
        ("claude-sonnet-4-5", False),
        ("claude-opus-4-8[1m]", True),      # the real 1M-context variant
        ("claude-opus-4-7[1m]", True),
        ("claude-sonnet-4-5[fast]", True),  # ANY bracket suffix → skip (fail-safe)
        ("model[anything]", True),
        ("claude[", False),          # malformed: unterminated bracket
        ("claude]", False),          # malformed: stray close bracket
        ("claude][", False),         # malformed: reversed order, no trailing [..]
        ("foo[1m]bar", False),       # bracket not at END → not a variant suffix
        (123, False),                # non-string → False (never raises)
        (["claude-opus-4-8[1m]"], False),
    ],
)
def test_is_alternate_pool_model_id_predicate(app, model_id, expected):
    assert app._lib_statusline.is_alternate_pool_model_id(model_id) is expected


# --- pool-identity guard in _statusline_persist (Task 1 / spec D1) ---------


def _POOL_TABLES():
    # The full blast radius a successful record writes (spec D1 / Codex R1 F7):
    # weekly snapshot + weekly milestones + 5h block/milestone rollups.
    return (
        "weekly_usage_snapshots",
        "percent_milestones",
        "five_hour_blocks",
        "five_hour_milestones",
    )


def test_alternate_pool_payload_persists_nothing(app):
    """A `[1m]`-variant session reports a SEPARATE rate-limit pool that would
    poison the default-pool DB. The guard skips persistence entirely: no
    snapshot, no weekly milestone, no 5h block/milestone, the observation
    marker is NOT touched (a foreign-pool session is not evidence the
    regular pipeline is alive), and no persist-lock file is even created
    (the guard returns before the lock acquire)."""
    now = int(time.time())
    parsed = _status_input(
        app, seven_pct=41.0, seven_resets_epoch=now + 3 * 86400,
        five_pct=42.0, five_resets_epoch=now + 2 * 3600, with_five_key=True,
        model_id="claude-opus-4-8[1m]",
    )
    app._statusline_persist(parsed, sync_for_test=True)

    for table in _POOL_TABLES():
        assert _count_rows(app, table) == 0, f"guard leaked a row into {table}"
    # Observation marker untouched → OAuth backfill keeps aging.
    assert app._statusline_observe_age_seconds() == float("inf")
    # The guard returns BEFORE _try_acquire_persist_lock, so no lock file.
    # (redirect_paths mirrors the pinned path constant onto the ns.)
    assert not app.STATUSLINE_PERSIST_LOCK_PATH.exists()


def test_normal_model_id_still_persists(app):
    """A normal (default-pool) model id persists exactly as before — proving
    the guard call didn't AttributeError into cmd_statusline's silent
    `except: pass` and disable ALL persistence (Codex R1 F1)."""
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


def test_concurrent_normal_and_alternate_pool_only_normal_persists(tmp_path):
    """Mixed multi-process herd (real fork path, pattern of
    test_concurrent_renders_yield_at_most_one_snapshot): simultaneous normal
    and alternate-pool `cctally statusline` renders sharing one data dir. The
    alternate-pool data must reach NEITHER axis, the normal persist succeeds
    exactly once, and the surviving snapshot carries the NORMAL pool's
    weekly_percent (never the alternate pool's poisoned value)."""
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
    normal_pct = 27.0
    alt_pct = 41.0
    normal_payload = json.dumps(_rate_limits_payload(
        seven_pct=normal_pct, seven_resets_epoch=now + 3 * 86400,
        five_pct=12.0, five_resets_epoch=now + 2 * 3600,
        model_id="claude-opus-4-8",
    )).encode()
    alt_payload = json.dumps(_rate_limits_payload(
        seven_pct=alt_pct, seven_resets_epoch=now + 3 * 86400,
        five_pct=42.0, five_resets_epoch=now + 2 * 3600,
        model_id="claude-opus-4-8[1m]",
    )).encode()

    # 2 normal + 3 alternate-pool renders, launched together.
    specs = [normal_payload, alt_payload, normal_payload, alt_payload, alt_payload]
    procs = [
        (
            subprocess.Popen(
                [sys.executable, str(_CCTALLY_BIN), "statusline"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env,
            ),
            payload,
        )
        for payload in specs
    ]
    for p, payload in procs:
        try:
            p.stdin.write(payload)
            p.stdin.close()
        except BrokenPipeError:
            pass
    for p, _ in procs:
        p.wait(timeout=30)

    # Detached feeders outlive the render process — poll to quiescence.
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
    assert final == 1, f"expected exactly one snapshot (the normal pool), got {final}"

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        pct = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert abs(pct - normal_pct) < 1e-6, (
        f"surviving snapshot carries {pct}, expected the normal pool {normal_pct} "
        f"(alternate pool {alt_pct} must never reach the DB)"
    )
