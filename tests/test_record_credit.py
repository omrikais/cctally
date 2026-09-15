"""record-credit: pure helpers + cmd_record_credit integration."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


NOW = dt.datetime(2026, 6, 19, 14, 37, tzinfo=dt.timezone.utc)
WS_AT = "2026-06-13T05:00:00+00:00"
WE_AT = "2026-06-20T05:00:00+00:00"


def _plan(ns, **over):
    kw = dict(
        week_start_date="2026-06-13",
        week_start_at=WS_AT,
        week_end_at=WE_AT,
        from_pct=46.0,
        from_source="hwm",
        to_pct=31.0,
        at_dt=NOW,
        now=NOW,
    )
    kw.update(over)
    return ns["_build_credit_plan"](**kw)


# ── R0: weekly_credit_floors schema-init (no migration) ────────────────


def test_weekly_credit_floors_table_created_no_migration(ns):
    """open_db() creates weekly_credit_floors via CREATE TABLE IF NOT EXISTS
    (schema-init, NOT a migration): the table exists on a fresh DB AND opening
    leaves user_version unchanged from the existing-schema head."""
    conn = ns["open_db"]()
    try:
        # Table exists with the spec'd columns.
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(weekly_credit_floors)").fetchall()}
        assert {"id", "week_start_date", "effective_at_utc",
                "observed_pre_credit_pct", "applied_at_utc"} <= cols
        uv1 = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    # Re-open: the IF NOT EXISTS path is a no-op and user_version is stable
    # (no migration was registered for this table).
    conn = ns["open_db"]()
    try:
        uv2 = conn.execute("PRAGMA user_version").fetchone()[0]
        # The table must NOT be tracked by the migration framework.
        names = {r[0] for r in conn.execute(
            "SELECT name FROM schema_migrations").fetchall()}
    finally:
        conn.close()
    assert uv1 == uv2
    assert not any("credit_floor" in n for n in names)


# ── R1: _reset_aware_floor (union of both floor sources) ───────────────


def test_reset_aware_floor_empty_is_none(ns):
    conn = ns["open_db"]()
    try:
        assert ns["_reset_aware_floor"](conn, "2026-06-13", WS_AT, WE_AT, account_key=None) is None
    finally:
        conn.close()


def test_reset_aware_floor_credit_floor_only(ns):
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_credit_floors (week_start_date, effective_at_utc,"
            " observed_pre_credit_pct, applied_at_utc) VALUES (?,?,?,?)",
            ("2026-06-13", "2026-06-19T14:00:00+00:00", 46.0,
             "2026-06-19T14:37:00Z"))
        conn.commit()
        got = ns["_reset_aware_floor"](conn, "2026-06-13", WS_AT, WE_AT, account_key=None)
        assert got == "2026-06-19T14:00:00+00:00"
    finally:
        conn.close()


def test_reset_aware_floor_latest_wins_mixed_offsets(ns):
    """A row in EACH table with mixed Z / +00:00 spellings: the latest instant
    wins via unixepoch() ordering (NOT a textual MAX, which would mis-order
    'Z' vs '+00:00')."""
    conn = ns["open_db"]()
    try:
        # week_reset_events leg: earlier, 'Z' spelling.
        conn.execute(
            "INSERT INTO week_reset_events (detected_at_utc, old_week_end_at,"
            " new_week_end_at, effective_reset_at_utc, observed_pre_credit_pct)"
            " VALUES (?,?,?,?,?)",
            ("2026-06-15T00:00:00Z", "2026-06-15T10:00:00Z",
             "2026-06-20T05:00:00+00:00", "2026-06-15T10:00:00Z", 50.0))
        # weekly_credit_floors leg: LATER, '+00:00' spelling.
        conn.execute(
            "INSERT INTO weekly_credit_floors (week_start_date, effective_at_utc,"
            " observed_pre_credit_pct, applied_at_utc) VALUES (?,?,?,?)",
            ("2026-06-13", "2026-06-19T14:00:00+00:00", 46.0,
             "2026-06-19T14:37:00Z"))
        conn.commit()
        got = ns["_reset_aware_floor"](conn, "2026-06-13", WS_AT, WE_AT, account_key=None)
        assert got == "2026-06-19T14:00:00+00:00"   # the later credit floor
    finally:
        conn.close()


def test_reset_aware_floor_reset_event_out_of_window_ignored(ns):
    """A week_reset_events row whose effective falls OUTSIDE [ws, we) is not a
    floor for this week."""
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO week_reset_events (detected_at_utc, old_week_end_at,"
            " new_week_end_at, effective_reset_at_utc, observed_pre_credit_pct)"
            " VALUES (?,?,?,?,?)",
            ("2026-06-01T00:00:00Z", "2026-06-01T00:00:00Z",
             "2026-06-06T05:00:00+00:00", "2026-06-01T00:00:00Z", 50.0))
        conn.commit()
        assert ns["_reset_aware_floor"](conn, "2026-06-13", WS_AT, WE_AT, account_key=None) is None
    finally:
        conn.close()


def test_parse_at_naive_is_utc(ns):
    got = ns["_parse_credit_at"]("2026-06-19T14:00", NOW)
    assert got == dt.datetime(2026, 6, 19, 14, 0, tzinfo=dt.timezone.utc)


def test_parse_at_default_is_now(ns):
    assert ns["_parse_credit_at"](None, NOW) == NOW


def test_build_plan_happy(ns):
    p = _plan(ns)
    assert p.to_pct == 31.0 and p.from_pct == 46.0
    assert p.effective_iso == "2026-06-19T14:00:00+00:00"   # floored to hour
    assert p.captured_iso == "2026-06-19T14:37:00Z"          # un-floored now, Z
    assert p.cur_end_canon == "2026-06-20T05:00:00+00:00"
    assert p.from_source == "hwm"


def test_build_plan_rejects_to_ge_from(ns):
    with pytest.raises(ValueError, match="not a credit"):
        _plan(ns, to_pct=46.0)


def test_build_plan_rejects_out_of_range(ns):
    with pytest.raises(ValueError):
        _plan(ns, to_pct=-1.0)
    with pytest.raises(ValueError):
        _plan(ns, from_pct=120.0)


def test_build_plan_rejects_none_pct(ns):
    """Defensive None-guard (#212 N3): a None --to/--from raises a clear
    ValueError (caller -> exit 2), NOT a TypeError from the `0.0 <= None`
    range compare. Unreachable via the CLI (--to required+float; --from
    resolves to a float first) but reachable by this pure helper's direct
    callers."""
    with pytest.raises(ValueError, match="numeric"):
        _plan(ns, to_pct=None)
    with pytest.raises(ValueError, match="numeric"):
        _plan(ns, from_pct=None)


def test_build_plan_rejects_future_at(ns):
    with pytest.raises(ValueError, match="future"):
        _plan(ns, at_dt=NOW + dt.timedelta(hours=1))


def test_build_plan_rejects_at_outside_window(ns):
    with pytest.raises(ValueError, match="window"):
        _plan(ns, at_dt=dt.datetime(2026, 6, 12, 0, 0, tzinfo=dt.timezone.utc),
              now=dt.datetime(2026, 6, 12, 0, 0, tzinfo=dt.timezone.utc))


# ── integration: cmd_record_credit ────────────────────────────────────


def _seed_week(ns, conn, *, pct=46.0, captured="2026-06-18T21:12:00Z"):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, page_url, source, payload_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (captured, "2026-06-13", "2026-06-20", WS_AT, WE_AT, pct,
         None, "userscript", "{}"),
    )
    conn.commit()


def _rc_args(**over):
    a = dict(to=31.0, from_pct=None, at=None, week=None,
             dry_run=True, yes=False, json=False, force=False)
    a.update(over)
    return argparse.Namespace(**a)


def _authorized_credit_args(**over):
    """Use explicit requested facts so authority tests do not depend on the
    unrelated current-week resolver fixture clock."""
    args = dict(
        dry_run=False,
        yes=True,
        from_pct=46.0,
        at="2026-06-19T14:37:00Z",
        week="2026-06-13",
    )
    args.update(over)
    return _rc_args(**args)


def test_resolves_current_week_and_hwm_from(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"]()
    _seed_week(ns, conn)
    conn.close()
    rc = ns["cmd_record_credit"](_rc_args())   # dry-run
    assert rc == 0


# ── apply: happy path (S1) + non-vacuity (S7) ──────────────────────────


def _weekly_reads(ns):
    """Run `weekly` and return the current week's rendered integer percent.
    Use the reset-aware HWM helper as the source of truth for the assertion."""
    conn = ns["open_db"]()
    try:
        return ns["_resolve_reset_aware_hwm"](
            conn, "2026-06-13", WS_AT, WE_AT, account_key=None)
    finally:
        conn.close()


def test_apply_happy_path_s1(ns, monkeypatch):
    """S1 (M2): --to 31 --yes writes a weekly_credit_floors row, NO
    week_reset_events row, forces hwm-7d, inserts a source='record-credit'
    snapshot, and the reset-aware HWM reads 31."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))
    assert rc == 0
    conn = ns["open_db"]()
    # M2: a weekly_credit_floors row, NOT a week_reset_events row.
    fl = conn.execute(
        "SELECT effective_at_utc, observed_pre_credit_pct "
        "FROM weekly_credit_floors WHERE week_start_date=?",
        ("2026-06-13",)).fetchone()
    assert fl is not None and float(fl[1]) == 46.0
    assert fl[0] == "2026-06-19T14:00:00+00:00"   # floored to hour, UTC spelling
    n_events = conn.execute(
        "SELECT COUNT(*) FROM week_reset_events").fetchone()[0]
    assert n_events == 0, "record-credit must NOT write a week_reset_events row (M2)"
    snap = conn.execute("SELECT weekly_percent, source FROM weekly_usage_snapshots "
                        "WHERE source='record-credit'").fetchone()
    assert snap is not None and float(snap[0]) == 31.0
    conn.close()
    assert _weekly_reads(ns) == 31.0     # reset-aware HWM now reads 31
    assert (ns["_cctally_core"].APP_DIR / "hwm-7d").read_text().split()[1] == "31.0"


def test_apply_commits_only_weekly_authoritative_tombstone(ns, monkeypatch):
    """An authorized same-week credit invalidates stale 7d candidates but
    must leave the independent 5h authority file untouched."""
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()

    assert ns["cmd_record_credit"](_authorized_credit_args()) == 0

    import json
    tombstone = json.loads(ns["STATUSLINE_AUTHORITATIVE_7D_PATH"].read_text())
    assert tombstone["axis"] == "sevenDay"
    assert tombstone["state"] == "committed"
    assert not ns["STATUSLINE_AUTHORITATIVE_5H_PATH"].exists()


def test_plan_drift_after_credit_authorization_aborts_before_tombstone(
        ns, monkeypatch):
    """The locked revalidation must reject a changed plan before it writes an
    inflight tombstone or mutates the credit tables."""
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    monkeypatch.setitem(ns, "_revalidate_credit_plan", lambda *args, **kwargs: None)

    assert ns["cmd_record_credit"](_authorized_credit_args()) == 2
    assert not ns["STATUSLINE_AUTHORITATIVE_7D_PATH"].exists()
    assert not ns["STATUSLINE_SELECTED_PATH"].exists()
    assert not ns["STATUSLINE_OBSERVE_MARKER_PATH"].exists()


@pytest.mark.parametrize(
    "args",
    [
        _authorized_credit_args(dry_run=True, yes=False),
        _authorized_credit_args(json=True, yes=False),
        _authorized_credit_args(yes=False),
    ],
)
def test_record_credit_non_mutating_exits_create_no_pipeline_artifacts(
        ns, monkeypatch, args):
    """Preview and rejection paths are entirely outside the selected writer
    critical section: a request that did not authorize mutation cannot make
    stale spool input fail closed or advertise selected freshness."""
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert ns["cmd_record_credit"](args) in (0, 2)
    assert not ns["STATUSLINE_AUTHORITATIVE_7D_PATH"].exists()
    assert not ns["STATUSLINE_SELECTED_PATH"].exists()
    assert not ns["STATUSLINE_OBSERVE_MARKER_PATH"].exists()


def test_apply_stores_effective_in_utc_on_non_utc_host(ns, monkeypatch):
    """Under a non-UTC host TZ, the credit floor's effective_at_utc MUST be
    stored with a +00:00 spelling, not the host offset — and the instant must
    still be the expected floored hour (2026-06-19T14:00 UTC).

    Non-vacuity: drop the .astimezone(dt.timezone.utc) in _apply_credit and
    this fails — the stored value carries -04:00/-05:00. (Existing tests run
    TZ=Etc/UTC, which is exactly why they were blind to this.)"""
    import time
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))
    assert rc == 0
    conn = ns["open_db"]()
    fl = conn.execute(
        "SELECT effective_at_utc, unixepoch(effective_at_utc) "
        "FROM weekly_credit_floors WHERE week_start_date=?",
        ("2026-06-13",)).fetchone()
    conn.close()
    assert fl is not None
    assert fl[0].endswith("+00:00"), f"stored host offset, not UTC: {fl[0]!r}"
    assert "-04:00" not in fl[0] and "-05:00" not in fl[0]
    expected = int(dt.datetime(2026, 6, 19, 14, 0,
                               tzinfo=dt.timezone.utc).timestamp())
    assert fl[1] == expected
    assert fl[0] == "2026-06-19T14:00:00+00:00"


def test_s12_no_reanchor(ns, monkeypatch):
    """S12 (M2-defining): after a credit, NO week_reset_events row exists AND
    the current-week window start stays the ORIGINAL week_start_at, not the
    credit moment — proves "same week" (no re-anchor)."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))
    assert rc == 0
    conn = ns["open_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 0
        # The window start the forecast/weekly current-week resolver returns
        # must be the original 2026-06-13 anchor, NOT the credit moment.
        fetched = ns["_fetch_current_week_snapshots"](
            conn, dt.datetime(2026, 6, 19, 14, 37, tzinfo=dt.timezone.utc))
        assert fetched is not None
        ws_at = fetched[0]
        ws_iso = ws_at if isinstance(ws_at, str) else ws_at.isoformat()
        ws_dt = dt.datetime.fromisoformat(str(ws_iso).replace("Z", "+00:00"))
        assert ws_dt == dt.datetime(2026, 6, 13, 5, 0, tzinfo=dt.timezone.utc), (
            f"window re-anchored to {ws_dt!r} instead of the original 2026-06-13")
    finally:
        conn.close()


def _statusline_seven_token(ns, monkeypatch, *, reported_7d, seven_resets_epoch):
    """Drive the REAL `cmd_statusline` end-to-end and return its rendered 7d
    integer percent. Feeds stdin a CC-hook JSON whose 7d used_percentage is
    `reported_7d`; the closure-resident `_hwm_clamp` clamps the displayed value
    UP to the reset-aware HWM, so a reported value below the post-credit HWM
    surfaces the HWM. This exercises the actual statusline clamp (NOT a re-
    implemented SQL), so reverting the _hwm_clamp floor change makes S14 RED."""
    import io
    import json as _j
    payload = {
        "session_id": "s14",
        "model": {"id": "claude-sonnet-4-5", "display_name": "Sonnet 4.5"},
        "workspace": {"current_dir": "/tmp"},
        "transcript_path": "/nonexistent/s14.jsonl",
        "rate_limits": {
            "seven_day": {"used_percentage": reported_7d,
                          "resets_at": seven_resets_epoch},
        },
        "cost": {"total_cost_usd": 0.0},
    }
    raw = _j.dumps(payload).encode("utf-8")

    class _Stdin:
        buffer = io.BytesIO(raw)
    monkeypatch.setattr(sys, "stdin", _Stdin())
    args = ns["build_parser"]().parse_args(["statusline", "--no-color"])
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ns["cmd_statusline"](args)
    assert rc == 0, buf.getvalue()
    line = buf.getvalue()
    import re
    m = re.search(r"7d (\d+)%", line)
    assert m is not None, f"no 7d token in statusline output: {line!r}"
    return int(m.group(1))


def test_s13_write_clamp_stores_post_credit_tick(ns, monkeypatch):
    """S13 (M2 linchpin): after a credit (floor in place), a record-usage tick
    at 37 (below the pre-credit peak 46) is STORED, not suppressed by the
    monotonic clamp, and the reset-aware HWM then reads 37.

    Non-vacuity (RED proof): without the _reset_aware_floor change at the
    write-site clamp, 37 < pre-credit MAX 46 -> should_insert=False -> the 37
    tick is never stored (n37==0) and the HWM stays at 31.

    Anchored to REAL now: `cmd_record_usage` stamps the inserted row's
    capturedAt via wall-clock `now_utc_iso()` (NOT _command_as_of), so we build
    a current week whose window contains real now, credit at now-2h, and tick at
    real now — keeping the tick's capture at/after the floor and inside the
    window without a hardcoded clock (memory: record-usage test time-bomb)."""
    real_now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    ws_dt = (real_now - dt.timedelta(days=3)).replace(
        hour=5, minute=0, second=0)
    we_dt = ws_dt + dt.timedelta(days=7)
    wsd = ws_dt.date().isoformat()
    ws_at = ws_dt.isoformat()
    we_at = we_dt.isoformat()
    at_credit = real_now - dt.timedelta(hours=2)

    def hwm(conn=None):
        owned = conn is None
        if owned:
            conn = ns["open_db"]()
        try:
            return ns["_resolve_reset_aware_hwm"](conn, wsd, ws_at, we_at,
                                                  account_key=None)
        finally:
            if owned:
                conn.close()

    conn = ns["open_db"]()
    conn.execute(
        "INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date,"
        " week_end_date, week_start_at, week_end_at, weekly_percent, page_url,"
        " source, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
        ((ws_dt + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
         wsd, we_dt.date().isoformat(), ws_at, we_at, 46.0,
         None, "userscript", "{}"))
    conn.commit(); conn.close()
    monkeypatch.setenv("CCTALLY_AS_OF", at_credit.isoformat().replace("+00:00", "Z"))
    assert ns["cmd_record_credit"](_rc_args(
        to=31.0, dry_run=False, yes=True, week=wsd)) == 0
    assert hwm() == 31.0
    # Real post-credit tick at 37 (below the pre-credit peak 46). Capture lands
    # at wall-clock now (>= the now-2h floor), inside the window.
    monkeypatch.delenv("CCTALLY_AS_OF", raising=False)
    resets_epoch = int(we_dt.timestamp())
    rc = ns["cmd_record_usage"](argparse.Namespace(
        percent=37.0, resets_at=resets_epoch,
        five_hour_percent=None, five_hour_resets_at=None,
        page_url=None, week_start_name=None))
    assert rc == 0
    conn = ns["open_db"]()
    try:
        n37 = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date=? AND weekly_percent=37.0",
            (wsd,)).fetchone()[0]
        post = hwm(conn)
    finally:
        conn.close()
    assert n37 == 1, "post-credit 37 tick was suppressed by the monotonic clamp"
    assert post == 37.0


def test_s14_statusline_floored_to_post_credit(ns, monkeypatch):
    """S14: the statusline 7d clamp surfaces the post-credit value (31), not the
    stale pre-credit 46. A reported 20% (below both) makes the clamp expose the
    reset-aware HWM, which is floored to the credit (31).

    Non-vacuity (RED proof): revert the _hwm_clamp _reset_aware_floor change and
    the bucket-wide MAX clamps to 46."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    assert ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True)) == 0
    seven_resets = int(dt.datetime(2026, 6, 20, 5, 0,
                                   tzinfo=dt.timezone.utc).timestamp())
    got = _statusline_seven_token(
        ns, monkeypatch, reported_7d=20.0, seven_resets_epoch=seven_resets)
    assert got == 31, f"statusline 7d not floored to post-credit: {got}"


def test_s15_project_floored_to_post_credit(ns, monkeypatch):
    """S15: `project`'s `_load_week_snapshots` reports the credited week's
    percentage as the post-credit value (31), not the stale 46.

    `record-credit` writes a same-window floor, which does NOT re-anchor the
    week and therefore creates no segment. #750 S4 §2.1 kept that manual leg
    for exactly this case: the floor lands inside the one segment and raises
    its lower bound, so the pre-credit 46 is out of range and the 31 is what
    the cycle reports.

    Non-vacuity (RED proof): drop the manual leg from
    `segment_capture_bounds` and the segment reports 46."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    assert ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True)) == 0
    since = dt.datetime(2026, 6, 13, 0, 0, tzinfo=dt.timezone.utc)
    until = dt.datetime(2026, 6, 20, 0, 0, tzinfo=dt.timezone.utc)
    conn = ns["open_db"]()
    try:
        segments = ns["_compute_subscription_weeks"](
            conn, since, until, account_key=None)
    finally:
        conn.close()
    snaps = ns["_load_week_snapshots"](segments)
    key = dt.datetime(2026, 6, 13, 5, 0, tzinfo=dt.timezone.utc)
    assert snaps.get(key) == 31.0, f"credited cycle not floored: {snaps!r}"


def test_s7_non_vacuity_snapshot_is_load_bearing(ns, monkeypatch):
    """Stash the synthetic-snapshot insert -> weekly no longer reads 31."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    monkeypatch.setitem(ns, "_insert_credit_snapshot", lambda *a, **k: 0)
    ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))
    assert _weekly_reads(ns) != 31.0     # empty post-credit segment


# ── 5h preservation (S10) ──────────────────────────────────────────────


def test_s10_copies_active_5h(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"]()
    _seed_week(ns, conn)
    conn.execute("UPDATE weekly_usage_snapshots SET five_hour_percent=22.0, "
                 "five_hour_resets_at=?, five_hour_window_key=? ",
                 ("2026-06-19T18:00:00+00:00", 1750356000))
    conn.commit(); conn.close()
    ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))
    conn = ns["open_db"]()
    snap = conn.execute("SELECT five_hour_percent, five_hour_window_key "
                        "FROM weekly_usage_snapshots WHERE source='record-credit'").fetchone()
    conn.close()
    assert float(snap[0]) == 22.0 and int(snap[1]) == 1750356000


def test_s10_expired_5h_is_null(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"]()
    _seed_week(ns, conn)
    conn.execute("UPDATE weekly_usage_snapshots SET five_hour_percent=22.0, "
                 "five_hour_resets_at=? ", ("2026-06-19T10:00:00+00:00",))  # past
    conn.commit(); conn.close()
    ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))
    conn = ns["open_db"]()
    snap = conn.execute("SELECT five_hour_percent FROM weekly_usage_snapshots "
                        "WHERE source='record-credit'").fetchone()
    conn.close()
    assert snap[0] is None


# ── existing-event handling (S4, S8, S9) + marker clear ─────────────────


def _apply_once(ns):
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    return ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))


def test_s8_completion_path_after_half_apply(ns, monkeypatch):
    """S8 (M2): floor row present (effective 14:00), NO command-owned snapshot
    -> a plain rerun at a LATER time (15:00) finishes it, REUSING the existing
    14:00 effective (not a fresh floor_to_hour(15:00)=15:00), so no stale
    [14:00,15:00) pre-credit replay leaks into the floored MAX."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn)
    # Simulate crash between 4a and 4d: floor row only, NO synthetic snapshot.
    conn.execute(
        "INSERT INTO weekly_credit_floors (week_start_date, effective_at_utc,"
        " observed_pre_credit_pct, applied_at_utc) VALUES (?,?,?,?)",
        ("2026-06-13", "2026-06-19T14:00:00+00:00", 46.0,
         "2026-06-19T14:00:00Z"))
    conn.commit(); conn.close()
    assert _weekly_reads(ns) != 31.0                  # half-applied (no snapshot)
    # Rerun an HOUR later — no --force; default --from reads the floor's
    # observed_pre_credit_pct (46).
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T15:07:00Z")
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))  # no --force
    assert rc == 0 and _weekly_reads(ns) == 31.0      # completed
    # The floor's effective is STILL 14:00 (reused, not moved to 15:00), and
    # there is exactly one floor row (the INSERT OR IGNORE deduped).
    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT effective_at_utc FROM weekly_credit_floors "
            "WHERE week_start_date=?", ("2026-06-13",)).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "2026-06-19T14:00:00+00:00", (
        f"effective moved forward instead of being reused: {rows[0][0]!r}")


def test_s4_fully_applied_refused(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    assert _apply_once(ns) == 0
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))  # again, no force
    assert rc == 2                                    # refused


def test_s4_fully_applied_refused_before_prompt(ns, monkeypatch, capsys):
    """#212 N2: an interactive (TTY) rerun on a fully-applied week is refused
    (exit 2) WITHOUT first printing the preview or invoking the confirm prompt.
    `input` is stubbed to return "y" — so were the refuse still ordered AFTER
    the prompt, this would proceed/apply instead of refusing. The refuse fires
    first, input() is never reached, and stdout stays empty."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    assert _apply_once(ns) == 0
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompted = {"hit": False}

    def _fake_input(*a, **k):
        prompted["hit"] = True
        return "y"

    monkeypatch.setattr("builtins.input", _fake_input)
    capsys.readouterr()                       # drain _apply_once's "applied" line
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=False))
    cap = capsys.readouterr()
    assert rc == 2
    assert prompted["hit"] is False           # never prompted
    assert "already recorded" in cap.err
    assert cap.out.strip() == ""              # no preview printed


def test_s9_force_scope_keeps_real_history(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    assert _apply_once(ns) == 0
    conn = ns["open_db"]()
    conn.execute("INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date,"
                 " week_end_date, week_start_at, week_end_at, weekly_percent, page_url,"
                 " source, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("2026-06-19T15:00:00Z", "2026-06-13", "2026-06-20", WS_AT, WE_AT,
                  33.0, None, "userscript", "{}"))
    conn.commit(); conn.close()
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True, force=True))
    assert rc == 0
    conn = ns["open_db"]()
    kept = conn.execute("SELECT COUNT(*) FROM weekly_usage_snapshots "
                        "WHERE source='userscript' AND weekly_percent=33.0").fetchone()[0]
    owned = conn.execute("SELECT COUNT(*) FROM weekly_usage_snapshots "
                         "WHERE source='record-credit'").fetchone()[0]
    conn.close()
    assert kept == 1 and owned == 1                   # real row kept, single re-do'd synthetic


def test_apply_clears_reset_debounce_state(ns, monkeypatch):
    """#750 S3: the debounce state is a stats.db row, so the manual credit's
    stale-state clear folds into the op's own transaction instead of unlinking
    a file beside it."""
    import _cctally_record as rec
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"]()
    _seed_week(ns, conn)
    rec._arm_reset_debounce_state(
        conn, "unattributed", week_start_date="2026-06-13",
        week_end_at="2026-06-20T05:00:00+00:00", baseline_pct=46.0,
        first_zero_at_utc="2026-06-19T14:00:00+00:00",
        first_zero_observation_id=None)
    conn.commit()
    assert rec._read_reset_debounce_state(conn, "unattributed") is not None
    conn.close()

    ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))

    conn = ns["open_db"]()
    try:
        assert rec._read_reset_debounce_state(conn, "unattributed") is None
    finally:
        conn.close()


# ── output: preview / confirm matrix / --json / dry-run (S2,S3,S5,S6) ───


import json as _json


def test_json_yes_envelope(ns, monkeypatch, capsys):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True, json=True))
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["schemaVersion"] == 1
    assert out["applied"] is True and out["dryRun"] is False and out["forced"] is False
    assert out["week"]["weekStartDate"] == "2026-06-13"
    assert out["credit"]["fromPct"] == 46.0 and out["credit"]["toPct"] == 31.0
    assert out["credit"]["fromSource"] == "hwm"
    assert out["credit"]["effectiveAtUtc"].endswith("Z")
    assert out["actions"]["hwm7dBefore"] == 46.0 and out["actions"]["hwm7dAfter"] == 31.0
    assert out["actions"]["creditFloorInserted"] is True
    assert out["actions"]["postCreditSnapshotInserted"] is True


def test_json_dryrun_envelope(ns, monkeypatch, capsys):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    rc = ns["cmd_record_credit"](_rc_args(dry_run=True, json=True))
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["applied"] is False and out["dryRun"] is True
    # nothing written
    conn = ns["open_db"]()
    owned = conn.execute("SELECT COUNT(*) FROM weekly_usage_snapshots "
                         "WHERE source='record-credit'").fetchone()[0]
    conn.close()
    assert owned == 0


def test_json_requires_yes_or_dryrun(ns, monkeypatch, capsys):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=False, json=True))
    assert rc == 2
    assert "record-credit:" in capsys.readouterr().err


def test_non_tty_refused(ns, monkeypatch, capsys):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=False, json=False))
    assert rc == 2
    assert "record-credit:" in capsys.readouterr().err


def test_db_error_exits_3(ns, monkeypatch, capsys):
    """A sqlite3.DatabaseError raised inside the DB work returns exit 3 with a
    plain-text `record-credit:` on stderr (docs §Exit codes "3 — a database
    error"; spec §4).

    Non-vacuity: before the fix, `conn = open_db()` sat OUTSIDE the function's
    try/finally, so this exception fell through to the global handler and the
    command exited 1. With the fix (open_db() inside the try + an
    `except sqlite3.DatabaseError -> return 3` arm) it returns 3. Stash the fix
    and this asserts 1, not 3.

    Patch `open_db` on `cmd_record_credit`'s own module namespace
    (`__globals__` IS `_cctally_record.__dict__`) — the bare `open_db()` call
    resolves there, NOT through the `cctally` ns, so `setitem(ns, ...)` would
    not intercept it.
    """
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    g = ns["cmd_record_credit"].__globals__

    def boom(*a, **k):
        raise sqlite3.DatabaseError("boom")

    monkeypatch.setitem(g, "open_db", boom)
    rc = ns["cmd_record_credit"](_rc_args(dry_run=True, json=False))
    assert rc == 3
    assert "record-credit:" in capsys.readouterr().err


def test_to_ge_from_plain_stderr_even_with_json(ns, monkeypatch, capsys):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    rc = ns["cmd_record_credit"](_rc_args(to=50.0, dry_run=True, json=True))
    assert rc == 2
    cap = capsys.readouterr()
    assert "record-credit:" in cap.err
    assert cap.out.strip() == ""        # no JSON on a validation error


def test_dryrun_human_preview(ns, monkeypatch, capsys):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    rc = ns["cmd_record_credit"](_rc_args(dry_run=True, json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "record-credit" in out and "46" in out and "31" in out
    assert "dry-run" in out.lower()


# ── week resolution at a reset boundary (S11) ──────────────────────────


def test_s11_resolves_active_week_not_stale_latest(ns, monkeypatch, capsys):
    """At a reset boundary, default --week resolves the window containing
    --at/now, not merely the most-recent snapshot's (just-ended) week."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-20T06:00:00Z")
    conn = ns["open_db"]()
    # Just-ended week: window [06-06 05:00, 06-13 05:00); its latest snapshot
    # is the MOST RECENT row overall (captured 06-13 04:50).
    conn.execute(
        "INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date,"
        " week_end_date, week_start_at, week_end_at, weekly_percent, page_url,"
        " source, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
        ("2026-06-13T04:50:00Z", "2026-06-06", "2026-06-13",
         "2026-06-06T05:00:00+00:00", "2026-06-13T05:00:00+00:00", 90.0,
         None, "userscript", "{}"))
    # Active week: window [06-13 05:00, 06-20 05:00 ... ) actually the new
    # week is [06-20 05:00, 06-27 05:00) — contains now=06-20 06:00.
    conn.execute(
        "INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date,"
        " week_end_date, week_start_at, week_end_at, weekly_percent, page_url,"
        " source, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
        ("2026-06-20T05:30:00Z", "2026-06-20", "2026-06-27",
         "2026-06-20T05:00:00+00:00", "2026-06-27T05:00:00+00:00", 46.0,
         None, "userscript", "{}"))
    conn.commit(); conn.close()
    # Capture the resolved plan via the dry-run JSON envelope.
    rc = ns["cmd_record_credit"](_rc_args(to=31.0, dry_run=True, json=True))
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["week"]["weekStartDate"] == "2026-06-20"   # active, not 06-06


# ── parser registration smoke ──────────────────────────────────────────


def test_record_credit_help_smoke():
    import pathlib
    import subprocess
    binary = pathlib.Path(__file__).resolve().parent.parent / "bin" / "cctally"
    proc = subprocess.run(
        [sys.executable, str(binary), "record-credit", "--help"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert "--to" in proc.stdout


# ── #834 S1 (#835): held rows survive the stale-replica DELETE ──────────


def _seed_replica(ns, conn, *, captured, pct, held, five_hour_pct,
                  account_key="unattributed", week_start_date="2026-06-13",
                  journal_id=None):
    """One `weekly_usage_snapshots` row with an explicit held flag, a five-hour
    reading and an optional logical journal id. Returns its rowid."""
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, page_url, source, payload_json, "
        " five_hour_percent, five_hour_window_key, account_key, "
        " weekly_observation_held, journal_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (captured, week_start_date, "2026-06-20", WS_AT, WE_AT,
         pct, None, "userscript", "{}", five_hour_pct, 1781280000,
         account_key, held, journal_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def _surviving_replicas(ns, ids):
    conn = ns["open_db"]()
    try:
        return {
            int(r[0]): (int(r[1]), r[2]) for r in conn.execute(
                "SELECT id, weekly_observation_held, five_hour_percent "
                "FROM weekly_usage_snapshots "
                f"WHERE id IN ({','.join('?' * len(ids))})", tuple(ids),
            ).fetchall()
        }
    finally:
        conn.close()


@pytest.mark.parametrize("site", ["auto", "manual_ingest", "manual_inline"])
def test_835_held_row_survives_the_stale_replica_delete(ns, monkeypatch, site):
    """#835. A `weekly_observation_held = 1` row is the ONLY carrier of its
    tick's five-hour reading — #824 writes it for exactly that reason — so no
    stale-replica removal may take it. The weekly value it carries IS retired by
    the credit; what became credit-aware is the READ that consumes a weekly
    value (`_latest_seven_day_and_window`), not this removal.

    All three destructive / capture sites are driven:

    * ``auto`` — `_fire_in_place_credit`'s shared `_stale_replica_band_sql`
      DELETE (inclusive ``<= 1.0`` band).
    * ``manual_ingest`` — the PRODUCTION `record-credit` path. Nothing is
      deleted inline there: the doomed rows' `journal_id`s are captured into a
      `weekly_credit_effects` evt and its applier deletes by id, so the rows
      must carry journal ids for this leg to remove anything at all.
    * ``manual_inline`` — `_apply_credit(ctx=None)`'s legacy step-4c DELETE
      (strict ``< 1.0`` band).

    Non-vacuity: drop `weekly_observation_held = 0` from any one of the three
    and that parametrization loses the held row."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    # An ingest-path removal deletes by logical id, so that leg's rows need one.
    jid = (lambda n: f"sa:seed:{n}") if site == "manual_ingest" else (
        lambda n: None)
    conn = ns["open_db"]()
    _seed_week(ns, conn)                       # 46% pre-credit peak
    # Both rows land at/after the credit's effective instant (14:00Z, floored
    # from the 14:37 AS_OF) and inside the 1.0-point band of 46.
    plain_id = _seed_replica(
        ns, conn, captured="2026-06-19T14:10:00Z", pct=46.0, held=0,
        five_hour_pct=11.0, journal_id=jid(1))
    held_id = _seed_replica(
        ns, conn, captured="2026-06-19T14:20:00Z", pct=46.0, held=1,
        five_hour_pct=23.5, journal_id=jid(2))
    conn.close()

    if site == "auto":
        conn = ns["open_db"]()
        try:
            ns["_fire_in_place_credit"](
                conn, "2026-06-13", WE_AT, 31.0,
                observed_pre_credit_pct=46.0,
                effective_dt=dt.datetime(2026, 6, 19, 14, 0,
                                         tzinfo=dt.timezone.utc),
            )
        finally:
            conn.close()
    elif site == "manual_ingest":
        assert ns["cmd_record_credit"](_rc_args(
            to=31.0, dry_run=False, yes=True, week="2026-06-13")) == 0
    else:
        conn = ns["open_db"]()
        try:
            ns["_apply_credit"](conn, _plan(ns))
        finally:
            conn.close()

    surviving = _surviving_replicas(ns, (plain_id, held_id))
    assert plain_id not in surviving, (
        "the non-held stale replica must still be removed")
    assert held_id in surviving, (
        "the held row carries the tick's only five-hour reading and must "
        "survive the stale-replica removal")
    assert surviving[held_id][0] == 1
    assert surviving[held_id][1] == pytest.approx(23.5), (
        "the held row's five-hour evidence must remain readable")


_CLASSIFIER_EFFECTIVE = "2026-06-19T14:00:00+00:00"
_CLASSIFIER_ID_BASE = "o:deadbeefdeadbeef"


def _classify(ns, conn, *, pre_credit=46.0, manual=True, id_base=None):
    return ns["_doomed_snapshot_rows"](
        conn,
        week_start_date="2026-06-13",
        account_key="unattributed",
        effective_iso=_CLASSIFIER_EFFECTIVE,
        pre_credit=pre_credit,
        manual=manual,
        id_base=id_base,
    )


def _all_snapshot_ids(conn):
    return {int(r[0]) for r in conn.execute(
        "SELECT id FROM weekly_usage_snapshots").fetchall()}


def _ids_named_by(conn, journal_ids):
    if not journal_ids:
        return set()
    rows = conn.execute(
        "SELECT id FROM weekly_usage_snapshots WHERE journal_id IN "
        f"({','.join('?' * len(journal_ids))})", tuple(journal_ids),
    ).fetchall()
    return {int(r[0]) for r in rows}


@pytest.mark.parametrize("case", [
    "null_journal_id",
    "sub_one_point_synthetic",
    "current_synthetic_replay",
    "crash_between_event_fsync_and_commit",
])
def test_835_doomed_classifier_projections_agree_on_the_required_relation(
        ns, case):
    """#834 S1 (#835). `_doomed_snapshot_rows` classifies the doomed set ONCE
    and exposes two projections, and the requirement between them is a SET
    RELATION, never textual equality of two predicates.

    Every journal identifier in the suppression projection must name a row the
    LIVE DELETE also removes, because a suppression list that named a row the
    DELETE keeps would make the journal applier destroy on replay what the live
    pass preserved. The comparison is against the rows
    `_delete_doomed_snapshot_rows` actually removed — the table is diffed around a
    real call — and not against the classifier's own `delete_ids`, which no
    production caller consumes (Gate A S3). Measured against `delete_ids` the
    assertion was about two projections of one function and would stay green
    through a change that made the DELETE's band narrower than the classifier's.

    THE MUTATION THIS FORM UNIQUELY CATCHES IS A NARROWING OF
    `_delete_doomed_snapshot_rows`' OWN STATEMENT, not of the shared band. Both
    projections are built from `_stale_replica_band_sql`, so narrowing THAT narrows
    the classifier's projection too and the old form would have failed as well.
    Appending `AND journal_id IS NOT NULL` to the DELETE alone is the divergence
    case: nothing in the classifier moves, and the `null_journal_id` case here fails
    on `assert {3} <= {1}` because the un-journalled poisoned row survives a removal
    that must take it. An earlier version of this paragraph named the shared band as
    the mutation, and the commit that introduced the new form named the DELETE
    correctly; this is the correction of record.

    The converse does NOT hold, deliberately and in two distinct ways:

    * an un-journalled poisoned row has no logical id to name, yet it still
      holds the live seven-day surfaces at the pre-credit percentage, so the
      DELETE must remove it;
    * the current operation's own `sa:<id_base>:syn:%` family is excluded from
      suppression so the list stays a PURE FUNCTION of the operation — under a
      crash between event fsync and commit the synthetic is replayed before the
      credit re-runs, and a timing-only exclusion would name the very row the
      operation must preserve.

    Normalizing those two away and comparing the predicates as text — revision
    1 of this session's specification — would prove only that shared SQL was
    copied, which is why the assertion here is the relation itself."""
    conn = ns["open_db"]()
    try:
        pre_credit = 46.0
        id_base = None
        # A genuine doomed replica with a nameable journal id, in every case.
        genuine = _seed_replica(
            ns, conn, captured="2026-06-19T14:05:00Z", pct=46.0, held=0,
            five_hour_pct=9.0, journal_id="sa:genuine:0")
        # A held row inside the band: preserved, so nameable by neither
        # projection (#835's whole point).
        held = _seed_replica(
            ns, conn, captured="2026-06-19T14:06:00Z", pct=46.0, held=1,
            five_hour_pct=9.5, journal_id="sa:held:0")
        unnameable: set = set()
        if case == "null_journal_id":
            unnameable = {_seed_replica(
                ns, conn, captured="2026-06-19T14:07:00Z", pct=46.0, held=0,
                five_hour_pct=10.0, journal_id=None)}
        else:
            # A sub-1.0pp credit is legal and puts the synthetic at `to_pct`
            # INSIDE the band centred on `from_pct`.
            id_base = _CLASSIFIER_ID_BASE
            syn = [_seed_replica(
                ns, conn, captured="2026-06-19T14:37:00Z", pct=45.5, held=0,
                five_hour_pct=10.0,
                journal_id=f"sa:{id_base}:syn:0")]
            if case != "sub_one_point_synthetic":
                syn.append(_seed_replica(
                    ns, conn, captured="2026-06-19T14:38:00Z", pct=45.6,
                    held=0, five_hour_pct=10.5,
                    journal_id=f"sa:{id_base}:syn:1"))
            unnameable = set(syn)

        _, suppression = _classify(
            ns, conn, pre_credit=pre_credit, id_base=id_base)
        # Canonical order, so one operation cannot emit two payloads.
        assert suppression == sorted(set(suppression))
        if id_base is not None:
            assert not any(s.startswith(f"sa:{id_base}:syn:")
                           for s in suppression)

        if case == "crash_between_event_fsync_and_commit":
            # Purity: the suppression list must be identical whether or not the
            # operation's own synthetics have been replayed into the store. Run
            # inside a SAVEPOINT and roll back, because the DELETE below has to
            # see the full seeded population and every seeding helper committed.
            conn.execute("SAVEPOINT purity")
            conn.execute(
                "DELETE FROM weekly_usage_snapshots WHERE journal_id LIKE ?",
                (f"sa:{id_base}:syn:%",))
            _, suppression_pre_replay = _classify(
                ns, conn, pre_credit=pre_credit, id_base=id_base)
            conn.execute("ROLLBACK TO purity")
            conn.execute("RELEASE purity")
            assert suppression_pre_replay == suppression, (
                "the suppression list is not a pure function of the operation")

        # Resolve the suppression identifiers to rowids BEFORE the removal, or
        # there would be no row left to resolve them against.
        suppressed_ids = _ids_named_by(conn, suppression)
        assert suppressed_ids, "non-vacuity: the suppression list named no row"
        before = _all_snapshot_ids(conn)
        removed = ns["_delete_doomed_snapshot_rows"](
            conn, week_start_date="2026-06-13", account_key="unattributed",
            effective_iso=_CLASSIFIER_EFFECTIVE, pre_credit=pre_credit,
            manual=True)
        conn.commit()
        deleted = before - _all_snapshot_ids(conn)
        assert removed == len(deleted), (
            "the DELETE's rowcount disagrees with the rows it removed")

        # The relation, asserted against what the LIVE DELETE removed.
        assert suppressed_ids <= deleted, (
            "a suppression identifier named a row the DELETE keeps")
        # The DELETE additionally removes what suppression cannot name.
        assert unnameable <= deleted, (
            "the DELETE must remove the rows suppression cannot name")
        assert unnameable & suppressed_ids == set()
        # The genuine journalled replica is in BOTH.
        assert genuine in deleted
        assert "sa:genuine:0" in suppression
        # The held row is in NEITHER.
        assert held not in deleted
        assert "sa:held:0" not in suppression
    finally:
        conn.close()


# ── #834 S1 (#835) Gate A R4: the held predicate needs the chokepoint ────


def _pre_column_store(path):
    """A hand-built `weekly_usage_snapshots` WITHOUT `weekly_observation_held`.

    The column arrived with epoch 1015. Fifteen test modules and six fixture
    builders create this table without it, which is why
    `_cctally_core.weekly_held_exclusion` exists: it omits the predicate where the
    column is absent, and that is not a compromise, because a store that does not
    carry the column cannot contain a held row, so the filtered and unfiltered
    populations are identical."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE weekly_usage_snapshots ("
        " id INTEGER PRIMARY KEY, journal_id TEXT, captured_at_utc TEXT,"
        " week_start_date TEXT, account_key TEXT, weekly_percent REAL)")
    conn.executemany(
        "INSERT INTO weekly_usage_snapshots (journal_id, captured_at_utc,"
        " week_start_date, account_key, weekly_percent) VALUES (?,?,?,?,?)",
        [("sa:seed:1", "2026-06-19T14:10:00Z", "2026-06-13", "unattributed",
          46.0),
         ("sa:seed:2", "2026-06-19T14:20:00Z", "2026-06-13", "unattributed",
          31.0)])
    conn.commit()
    return conn


def test_835_r4_the_doomed_classifier_runs_on_a_pre_column_store(ns, tmp_path):
    """`_STALE_REPLICA_BAND_TEMPLATE` referenced `weekly_observation_held`
    unconditionally, so `_doomed_snapshot_rows` raised
    `sqlite3.OperationalError: no such column: weekly_observation_held` on every
    hand-built store in the estate. The commit immediately before this branch
    introduced `weekly_held_exclusion` for exactly this predicate, and the new
    band has to go through it."""
    conn = _pre_column_store(tmp_path / "pre-column.sqlite")
    try:
        delete_ids, suppression = ns["_doomed_snapshot_rows"](
            conn, week_start_date="2026-06-13", account_key="unattributed",
            effective_iso="2026-06-19T14:00:00+00:00", pre_credit=46.0,
            manual=True)
    finally:
        conn.close()
    # The 46.0 row is inside the strict band; the 31.0 row is not. Asserting the
    # selection rather than only the absence of a raise keeps the test from
    # passing on a band that silently matches nothing.
    assert delete_ids == [1]
    assert suppression == ["sa:seed:1"]


def test_835_r4_the_stale_replay_count_runs_on_a_pre_column_store(ns, tmp_path):
    """The same predicate at the preview's own query.

    #834 S2 (#837) landed the account predicate this docstring used to say was
    deliberately absent, and it landed it by routing the count through
    `_stale_replica_band_sql` — the same template the removal uses — so the
    chokepoint's pre-column tolerance now reaches the preview by construction
    rather than by a second copy of the predicate. The store here carries
    `account_key` but not `weekly_observation_held`, which is the shape fifteen
    test modules and six fixture builders create."""
    conn = _pre_column_store(tmp_path / "pre-column-count.sqlite")
    try:
        count = ns["_count_stale_replays"](
            conn, _plan(ns), account_key="unattributed")
        enumerated = ns["_stale_replay_candidates"](
            conn, _plan(ns), account_key="unattributed")
    finally:
        conn.close()
    assert count == 1
    assert len(enumerated) == count, (
        "the count must be the size of the enumeration, not a second query")


def test_835_r4_the_band_keeps_both_comparisons_from_one_text(ns):
    """The constant existed to make five sites share ONE band text, and the
    inclusive/strict divergence between the automatic and manual paths is
    deliberate. Turning the constant into a connection-taking builder must keep
    both properties: one template, two comparisons, and the held predicate
    appended by the chokepoint rather than written into either spelling."""
    conn = ns["open_db"]()
    try:
        auto = ns["_stale_replica_band_sql"](conn, manual=False)
        manual = ns["_stale_replica_band_sql"](conn, manual=True)
    finally:
        conn.close()
    assert "ABS(weekly_percent - ?) <= 1.0" in auto
    assert "ABS(weekly_percent - ?) < 1.0" in manual
    assert "<=" not in manual.split("ABS(weekly_percent - ?)")[1]
    # One text: the two differ ONLY in the comparison.
    assert auto.replace("<= 1.0", "< 1.0") == manual
    assert "weekly_observation_held = 0" in auto, (
        "the chokepoint must still append the predicate on a current store")


# ── #834 S1 (#835) Gate A R7: the deletion is ONE atomic statement ───────


def _r7_fire(ns, conn, **over):
    kw = dict(observed_pre_credit_pct=46.0,
              effective_dt=dt.datetime(2026, 6, 19, 14, 0,
                                       tzinfo=dt.timezone.utc))
    kw.update(over)
    return ns["_fire_in_place_credit"](
        conn, "2026-06-13", WE_AT, 31.0, **kw)


def test_835_r7_a_row_entering_the_band_after_classification_is_still_deleted(
        ns, monkeypatch):
    """The removal classified ids and then deleted by id in a second statement, so
    the band was evaluated at classification time rather than at deletion time. A
    row entering the band in between escaped a deletion the single predicate
    statement it replaced would have performed — and on the automatic path a
    `conn.commit()` runs in that region.

    The window is closed by making the deletion ONE statement over the same band
    predicate. This test forces a row into the band between the two, by wrapping
    the classifier, which is the narrowest way to make the window observable."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"]()
    _seed_week(ns, conn)
    original = ns["_doomed_snapshot_rows"]
    late: dict = {}

    def classify_then_insert(*args, **kwargs):
        result = original(*args, **kwargs)
        if "id" not in late:
            late["id"] = _seed_replica(
                ns, conn, captured="2026-06-19T14:25:00Z", pct=46.0, held=0,
                five_hour_pct=12.0)
        return result

    monkeypatch.setitem(ns, "_doomed_snapshot_rows", classify_then_insert)
    try:
        _r7_fire(ns, conn)
    finally:
        conn.close()

    assert "id" in late, "non-vacuity: the classifier must have been called"
    assert late["id"] not in _surviving_replicas(ns, (late["id"],)), (
        "a row that entered the band after classification survived the "
        "deletion, because the deletion named captured ids instead of the band")


def test_835_r7_the_deletion_is_not_bounded_by_a_parameter_limit(ns,
                                                                monkeypatch):
    """The id-list deletion bound its parameter count to the size of the doomed
    set. The largest `(week, account, integer percent)` group on the live store
    holds 811 rows, and an older SQLite caps a statement at 999 variables, so the
    list form was one busy week away from refusing.

    A predicate deletion takes four parameters whatever the population. This is a
    pin on the outcome rather than a reproduction: the runner's SQLite allows far
    more than 999 variables, so the ceiling itself cannot be reached here."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"]()
    _seed_week(ns, conn)
    minute = dt.datetime(2026, 6, 19, 14, 1, tzinfo=dt.timezone.utc)
    conn.executemany(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, page_url, source, payload_json, "
        " five_hour_percent, five_hour_window_key, account_key, "
        " weekly_observation_held) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
        [((minute + dt.timedelta(seconds=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "2026-06-13", "2026-06-20", WS_AT, WE_AT, 46.0, None, "userscript",
          "{}", 11.0, 1781280000, "unattributed") for n in range(1200)],
    )
    conn.commit()
    try:
        _r7_fire(ns, conn)
    finally:
        conn.close()

    conn = ns["open_db"]()
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE weekly_percent = 46.0 "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?)",
            ("2026-06-19T14:00:00+00:00",)).fetchone()[0]
    finally:
        conn.close()
    # Scoped to the band's own lower bound: `_seed_week`'s 46% row is captured
    # BEFORE the effective instant and is correctly left alone.
    assert remaining == 0, (
        f"{remaining} of 1200 in-band replicas survived the deletion")


def test_835_r7_the_automatic_path_reaches_the_classifier_by_namespace(
        ns, monkeypatch):
    """`bin/cctally` re-exports the classifier and says it does so because BOTH
    credit paths reach it through that namespace for test monkeypatching. That was
    false: `_apply_credit` called `c._doomed_snapshot_rows` while
    `_fire_in_place_credit` called the module-local symbol at both of its sites, so
    a test patching the namespace did not intercept the automatic path. The comment
    asserted a property the code did not have, and this test is what makes it
    true."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    calls: list = []
    original = ns["_doomed_snapshot_rows"]

    def spy(*args, **kwargs):
        calls.append(kwargs.get("manual"))
        return original(*args, **kwargs)

    monkeypatch.setitem(ns, "_doomed_snapshot_rows", spy)
    conn = ns["open_db"]()
    _seed_week(ns, conn)
    _seed_replica(ns, conn, captured="2026-06-19T14:10:00Z", pct=46.0, held=0,
                  five_hour_pct=11.0, journal_id="sa:seed:1")
    try:
        _r7_fire(ns, conn)
    finally:
        conn.close()
    assert False in calls, (
        "the automatic path did not reach the classifier through the cctally "
        "namespace, so a test patching it cannot intercept that path")


def test_835_r7_the_own_synthetic_exclusion_is_case_insensitive(ns, tmp_path):
    """The classifier replaced a SQL `journal_id NOT LIKE 'sa:' || ? || ':syn:%'`
    with `str.startswith`. SQLite's `LIKE` is case-insensitive for ASCII and
    `startswith` is not, so the two disagree on a mixed-case `id_base`.

    Neither spelling is reachable today, because the sole caller passes
    `id_base=rec["id"]`, a content digest built in the same process as the
    `journal_id` it is compared against. The divergence is removed rather than
    argued about, because a silent semantic change in a suppression list is the
    kind of thing that becomes reachable later without anyone noticing.

    NON-VACUITY: an empty suppression list also holds when the seeded row never
    entered the band at all, so the same classification is run WITHOUT an
    `id_base`. That form applies no exclusion, so it must name the row — and only
    then does the empty list under the mixed-case `id_base` say anything about case
    folding (Gate A S5 item 4)."""
    conn = ns["open_db"]()
    try:
        _seed_week(ns, conn)
        _seed_replica(ns, conn, captured="2026-06-19T14:10:00Z", pct=46.0,
                      held=0, five_hour_pct=11.0,
                      journal_id="sa:o:DEADBEEF:syn:0")
        _, unexcluded = _classify(ns, conn, id_base=None)
        _, suppression = _classify(ns, conn, id_base="o:deadbeef")
    finally:
        conn.close()
    assert unexcluded == ["sa:o:DEADBEEF:syn:0"], (
        "non-vacuity: the seeded row never entered the band, so an empty "
        "suppression list below would say nothing about case folding")
    assert suppression == [], (
        "the own-synthetic exclusion became case-sensitive, so the operation's "
        "own synthetic is named in its suppression list")


# ── #834 S2 (#837): the preview names the account it will write ────────
#
# `record-credit` resolved the active identity only under the writer lock,
# after the preview had been shown and confirmed, so the preview could not say
# which account the mutation targeted. On a store holding two real accounts a
# preview that does not name its target is not a preview a person can check.
# Under R8 the disclosure appears only at more than one REAL account: a lone
# `unattributed` bucket triggers nothing.


def _account_key_for(uuid):
    import _lib_accounts
    return _lib_accounts.account_key("claude", uuid)


def _activate_claude(uuid, email):
    import _cctally_core
    import json as _json
    _cctally_core.CLAUDE_JSON_PATH.write_text(_json.dumps({
        "oauthAccount": {"accountUuid": uuid, "emailAddress": email,
                         "plan": "max"},
    }))
    _cctally_core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _account_key_for(uuid)


def _register_account(conn, account_key, *, email, label):
    conn.execute(
        "INSERT OR REPLACE INTO accounts "
        "(account_key, provider, natural_id, email, label, plan_type, "
        " label_source, first_seen_utc, last_seen_utc) "
        "VALUES (?,'claude',?,?,?,'max','manual',?,?)",
        (account_key, account_key, email, label,
         "2026-06-13T00:00:00Z", "2026-06-19T00:00:00Z"),
    )


def _seed_week_for(ns, conn, account_key, *, pct=46.0,
                   captured="2026-06-18T21:12:00Z"):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, page_url, source, payload_json, "
        " account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (captured, "2026-06-13", "2026-06-20", WS_AT, WE_AT, pct,
         None, "userscript", "{}", account_key),
    )
    conn.commit()


def _preview_surfaces(ns, capsys, **over):
    """The human preview text and the `--json` preview for one dry run."""
    assert ns["cmd_record_credit"](_rc_args(dry_run=True, **over)) == 0
    text = capsys.readouterr().out
    assert ns["cmd_record_credit"](
        _rc_args(dry_run=True, json=True, **over)) == 0
    payload = json.loads(capsys.readouterr().out)
    return text, payload


def test_837_preview_discloses_the_target_account_at_two_real_accounts(
        ns, monkeypatch, capsys):
    """At >1 real account, both preview surfaces name the account written."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    active = _activate_claude("acct-uuid-active", "active@example.com")
    other = _account_key_for("acct-uuid-other")
    conn = ns["open_db"]()
    try:
        _register_account(conn, active, email="active@example.com",
                          label="Active")
        _register_account(conn, other, email="other@example.com",
                          label="Other")
        conn.commit()
        _seed_week_for(ns, conn, active)
    finally:
        conn.close()

    text, payload = _preview_surfaces(ns, capsys)
    assert "Active" in text, (
        "the human preview does not name the account the credit will be "
        f"written under:\n{text}")
    assert payload.get("accountKey") == active, (
        "the --json preview does not name the account the credit will be "
        f"written under: {payload!r}")
    assert payload.get("accountLabel") == "Active"


def test_837_preview_omits_the_account_at_one_real_account(
        ns, monkeypatch, capsys):
    """At exactly one real account, R8 suppresses the disclosure entirely.

    This is the guard that keeps the test above from being satisfied by an
    unconditional field. It passes both before and after the disclosure exists,
    which is what makes it a guard rather than a restatement of the fix.
    """
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    active = _activate_claude("acct-uuid-active", "active@example.com")
    conn = ns["open_db"]()
    try:
        _register_account(conn, active, email="active@example.com",
                          label="Active")
        conn.commit()
        _seed_week_for(ns, conn, active)
    finally:
        conn.close()

    text, payload = _preview_surfaces(ns, capsys)
    assert "account:" not in text and "Active" not in text, (
        f"R8: a single real account must render no decoration:\n{text}")
    assert "accountKey" not in payload and "accountLabel" not in payload, (
        f"R8: a single real account must emit no account field: {payload!r}")
