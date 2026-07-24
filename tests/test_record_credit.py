"""record-credit: pure helpers + cmd_record_credit integration."""
from __future__ import annotations

import argparse
import datetime as dt
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
    """S15: `project`'s _load_week_snapshots reports the credited week's per-week
    MAX as the post-credit value (31), not the stale 46.

    Non-vacuity (RED proof): revert the _load_week_snapshots floor and the
    per-week MAX returns 46."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    assert ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True)) == 0
    since = dt.datetime(2026, 6, 13, 0, 0, tzinfo=dt.timezone.utc)
    until = dt.datetime(2026, 6, 20, 0, 0, tzinfo=dt.timezone.utc)
    snaps = ns["_load_week_snapshots"](since, until)
    key = dt.datetime(2026, 6, 13, 5, 0, tzinfo=dt.timezone.utc)
    assert snaps.get(key) == 31.0, f"project week MAX not floored: {snaps!r}"


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


def test_apply_clears_reset_zero_marker(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    ns["_arm_reset_zero_marker"](
        "2026-06-13", "2026-06-20T05:00:00+00:00",
        baseline_pct=46.0, first_zero_iso="2026-06-19T14:00:00+00:00")
    assert ns["_read_reset_zero_marker"]() is not None
    ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))
    assert ns["_read_reset_zero_marker"]() is None


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
