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
            conn, "2026-06-13", WS_AT, WE_AT)
    finally:
        conn.close()


def test_apply_happy_path_s1(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn); conn.close()
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))
    assert rc == 0
    conn = ns["open_db"]()
    ev = conn.execute("SELECT observed_pre_credit_pct, new_week_end_at "
                      "FROM week_reset_events WHERE new_week_end_at=?",
                      ("2026-06-20T05:00:00+00:00",)).fetchone()
    assert ev is not None and float(ev[0]) == 46.0
    snap = conn.execute("SELECT weekly_percent, source FROM weekly_usage_snapshots "
                        "WHERE source='record-credit'").fetchone()
    assert snap is not None and float(snap[0]) == 31.0
    conn.close()
    assert _weekly_reads(ns) == 31.0     # reset-aware HWM now reads 31
    assert (ns["_cctally_core"].APP_DIR / "hwm-7d").read_text().split()[1] == "31.0"


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
    """Event present, no command-owned snapshot -> plain rerun finishes it."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    conn = ns["open_db"](); _seed_week(ns, conn)
    # Simulate crash: fire pivots only, NO synthetic snapshot.
    eff = dt.datetime(2026, 6, 19, 14, 0, tzinfo=dt.timezone.utc)
    ns["_fire_in_place_credit"](conn, "2026-06-13", "2026-06-20T05:00:00+00:00",
                                31.0, observed_pre_credit_pct=46.0, effective_dt=eff)
    conn.close()
    assert _weekly_reads(ns) != 31.0                  # half-applied
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))  # no --force
    assert rc == 0 and _weekly_reads(ns) == 31.0      # completed


def test_s4_fully_applied_refused(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    assert _apply_once(ns) == 0
    rc = ns["cmd_record_credit"](_rc_args(dry_run=False, yes=True))  # again, no force
    assert rc == 2                                    # refused


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
    assert out["actions"]["resetEventInserted"] is True
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
