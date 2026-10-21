"""A stats rebuild keeps every milestone a journaled suppression orphaned.

#825/#832 Task 4.0 (spec 4.7, case M19). A five-hour in-place credit's
stale-replica DELETE removes every snapshot from floor10(arming tick) within
1pp of the pre-credit peak. That band also holds the GENUINE peak tick when it
shares the arming tick's ten-minute slot, and that tick is the one that crossed
the last weekly and five-hour milestones. A rebuild folds the suppression
(order 30) before the milestones (order 60), so the milestones' logical
`usage_snapshot_ref` no longer resolved: `INSERT OR IGNORE` dropped every such
NOT NULL row without a word, and the next tick re-recorded the highest weekly
milestone, whose conflict convergence then raised
`IntegrityError: NOT NULL constraint failed: percent_milestones.usage_snapshot_id`
on every cycle, so ingest never advanced again.

The trace is the reporter's trace D: real JSONL under a fake
`~/.claude/projects`, bound recordings at :13 (w10/5h30), :17 (w11/5h34),
:21 (w12/5h38), :24 (w12/5h2, arms the credit) and :26 (w12/5h3, confirms it),
then a :34 tick at w13 after the rebuild.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

import pytest

from conftest import load_script, redirect_paths

NOW = 1791417600  # 2026-10-08T00:00:00Z
WEEK_RESET = NOW + 3 * 86400
FIVE_RESET = NOW + 4 * 3600  # one physical window, on a ten-minute boundary

#: (minute, weekly %, five-hour %) — trace D.
TRACE = (
    (13, 10.0, 30.0),
    (17, 11.0, 34.0),
    (21, 12.0, 38.0),
    (24, 12.0, 2.0),
    (26, 12.0, 3.0),
)
PEAK_MINUTE = 21


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _tick(monkeypatch, at):
    monkeypatch.setattr(time, "time", lambda: float(NOW + at))
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(NOW + at))


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _tick(monkeypatch, 0)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    cctally = sys.modules["cctally"]
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda *a, **k: None,
                        raising=False)
    cctally.open_db().close()
    return cctally


@pytest.fixture
def alert_sink(monkeypatch):
    import _cctally_journal as JR
    sink = []
    monkeypatch.setattr(JR, "ALERT_DISPATCHER",
                        lambda alerts: sink.extend(list(alerts)))
    return sink


def _identity(name):
    import _cctally_core as core
    import _lib_accounts
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": name, "emailAddress": f"{name}@example.test"}}))
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _lib_accounts.account_key("claude", name)


def _assistant(ts, msg_id, out):
    return json.dumps({
        "type": "assistant", "timestamp": ts, "sessionId": "s1",
        "cwd": "/Users/u/proj", "requestId": f"r-{msg_id}",
        "message": {"id": msg_id, "model": "claude-sonnet-4-5-20250929",
                    "usage": {"input_tokens": 1000, "output_tokens": out,
                              "cache_creation_input_tokens": 2000,
                              "cache_read_input_tokens": 50000}},
    })


def _append_jsonl(tmp_path, lines):
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    with (proj / "s1.jsonl").open("a") as fh:
        for line in lines:
            fh.write(line + "\n")


def _record(app, weekly, five):
    import _cctally_statusline as sl
    _identity("A")
    args = argparse.Namespace(
        percent=weekly, resets_at=str(WEEK_RESET), five_hour_percent=five,
        five_hour_resets_at=str(FIVE_RESET), source="statusline")
    return app.record_usage_bound(args, binding=sl._capture_binding(),
                                  nudge_dashboard=False)


def _drive(app, monkeypatch, tmp_path, minute, weekly, five):
    n = minute * 10
    _append_jsonl(tmp_path, [
        _assistant(_iso(NOW + minute * 60 - 200 + k * 60), f"m{n + k}",
                   4000 + 100 * (n + k))
        for k in range(3)])
    _tick(monkeypatch, minute * 60)
    return _record(app, weekly, five)


def _journal():
    import _cctally_core as core
    import _lib_journal as J
    out = []
    for path in sorted(core.JOURNAL_DIR.glob("*.jsonl")):
        with path.open("rb") as fh:
            for raw in fh:
                rec = J.decode_line(raw)
                if rec is not None:
                    out.append(rec)
    return out


def _logical_ref(conn, table, rowid):
    """A rowid FK as the logical identity of the row it names: the `"0"`
    no-FK sentinel, the referenced row's `journal_id`, or `DANGLING` for a
    rowid whose row no longer exists (which a rebuild cannot reproduce)."""
    if rowid in (0, None):
        return "0"
    row = conn.execute(
        f"SELECT journal_id FROM {table} WHERE id = ?", (rowid,)).fetchone()
    return "DANGLING" if row is None else row[0]


_PM = ("account_key", "week_start_date", "week_end_date", "percent_threshold",
       "captured_at_utc", "cumulative_cost_usd", "marginal_cost_usd",
       "five_hour_percent_at_crossing", "alerted_at", "journal_id")
_FHM = ("account_key", "five_hour_window_key", "percent_threshold",
        "captured_at_utc", "block_cost_usd", "marginal_cost_usd",
        "seven_day_pct_at_crossing", "alerted_at", "journal_id")


def _milestones(app):
    conn = app.open_db()
    conn.row_factory = None
    try:
        pm = []
        for row in conn.execute(
                f"SELECT {', '.join(_PM)}, reset_event_id, usage_snapshot_id,"
                " cost_snapshot_id FROM percent_milestones"):
            logical = dict(zip(_PM, row[:len(_PM)]))
            logical["reset_event"] = _logical_ref(conn, "week_reset_events", row[-3])
            logical["usage_snapshot"] = _logical_ref(
                conn, "weekly_usage_snapshots", row[-2])
            logical["cost_snapshot"] = _logical_ref(
                conn, "weekly_cost_snapshots", row[-1])
            pm.append(json.dumps(logical, sort_keys=True))
        fhm = []
        for row in conn.execute(
                f"SELECT {', '.join(_FHM)}, reset_event_id, usage_snapshot_id"
                " FROM five_hour_milestones"):
            logical = dict(zip(_FHM, row[:len(_FHM)]))
            logical["reset_event"] = _logical_ref(
                conn, "five_hour_reset_events", row[-2])
            logical["usage_snapshot"] = _logical_ref(
                conn, "weekly_usage_snapshots", row[-1])
            fhm.append(json.dumps(logical, sort_keys=True))
        return {"percent_milestones": sorted(pm),
                "five_hour_milestones": sorted(fhm)}
    finally:
        conn.close()


def _cursor(app):
    conn = app.open_db()
    try:
        return tuple(conn.execute(
            "SELECT segment, offset FROM journal_cursor").fetchone())
    finally:
        conn.close()


def _rebuild(app):
    args = app.build_parser().parse_args(["db", "rebuild", "--db", "stats", "--json"])
    return args.func(args)


def _alert_keys(sink):
    return sorted((str(p.get("axis")), int(p.get("threshold")))
                  for p in sink if isinstance(p, dict))


def _trace_d(app, monkeypatch, tmp_path, alert_sink):
    import _cctally_core as core
    core.CONFIG_PATH.write_text(json.dumps({"alerts": {
        "enabled": True, "weekly_thresholds": [12],
        "five_hour_thresholds": [35]}}))
    peak_ref = None
    for minute, weekly, five in TRACE:
        assert _drive(app, monkeypatch, tmp_path, minute, weekly, five) == 0
        if minute == PEAK_MINUTE:
            conn = app.open_db()
            try:
                peak_ref = conn.execute(
                    "SELECT journal_id FROM weekly_usage_snapshots"
                    " WHERE unixepoch(captured_at_utc) = ?",
                    (NOW + PEAK_MINUTE * 60,)).fetchone()[0]
            finally:
                conn.close()
    return peak_ref


def _record_historical_policy(monkeypatch):
    """Record as the release before spec 4.7 revision 6 did: no fold-policy
    revision on the raw observations. The credit's stale-replica cleanup is
    not part of the fold policy: since fix round K15 it retires the genuine
    peak tick's unheld row in place under every policy."""
    import _lib_journal
    real = _lib_journal.make_obs

    def make_obs(**kwargs):
        payload = dict(kwargs.get("payload") or {})
        payload.pop("fold_policy", None)
        return real(**{**kwargs, "payload": payload})

    monkeypatch.setattr(_lib_journal, "make_obs", make_obs)


@pytest.mark.parametrize("policy", ["historical", "first_post_floor"])
def test_rebuild_keeps_milestones_whose_snapshot_a_five_hour_credit_suppressed(
        app, monkeypatch, tmp_path, alert_sink, policy):
    """Under either fold policy the credit retires only the peak tick's
    five-hour effect and keeps its accepted weekly 12 (spec 4.7 revision 6,
    fix round K15). The milestones survive a rebuild and the next tick does
    not wedge ingest."""
    if policy == "historical":
        _record_historical_policy(monkeypatch)
    peak_ref = _trace_d(app, monkeypatch, tmp_path, alert_sink)
    assert peak_ref, "the :21 peak tick was accepted and journaled"

    # Non-vacuity: the credit fired and its one suppression list names the
    # genuine peak tick, so the 12% weekly and 35%-38% five-hour milestones
    # it crossed now reference a snapshot whose five-hour effect the credit
    # retired.
    credits = [r for r in _journal() if r.get("t") == "evt"
               and (r.get("payload") or {}).get("kind") == "five_hour_credit"]
    assert len(credits) == 1, credits
    assert "five_hour_retirement" not in credits[0]["payload"]
    assert peak_ref in credits[0]["payload"]["suppression"]
    live = _milestones(app)
    peak_pm = [json.loads(r) for r in live["percent_milestones"]
               if json.loads(r)["percent_threshold"] == 12]
    assert peak_pm and peak_pm[0]["captured_at_utc"].startswith("2026-10-08T00:21")
    live_alerts = _alert_keys(alert_sink)
    assert ("weekly", 12) in live_alerts and ("five_hour", 35) in live_alerts

    assert _rebuild(app) == 0
    assert _milestones(app) == live

    # The next tick after the rebuild must not re-record the 12% milestone (a
    # re-record is what converged into a NOT NULL violation and wedged ingest).
    import _cctally_journal as JR
    before = _cursor(app)
    alert_sink.clear()
    assert _drive(app, monkeypatch, tmp_path, 34, 13.0, 7.0) == 0
    assert _cursor(app) > before
    # A cycle's own emissions land past the high-water mark it snapshotted, so
    # one more cycle with nothing new appended must consume them and reach the
    # journal's end: a wedged ingest raises here instead.
    JR.run_stats_ingest(mode="authoritative")
    assert _cursor(app) == tuple(JR.journal_high_water())
    conn = app.open_db()
    try:
        twelve = conn.execute(
            "SELECT captured_at_utc FROM percent_milestones"
            " WHERE percent_threshold = 12").fetchall()
        thirteen = conn.execute(
            "SELECT COUNT(*) FROM percent_milestones"
            " WHERE percent_threshold = 13").fetchone()[0]
    finally:
        conn.close()
    assert thirteen == 1, "the post-rebuild tick still records its own crossing"
    assert [r[0][:16] for r in twelve] == ["2026-10-08T00:21"]
    assert not {("weekly", 12), ("five_hour", 35)} & set(_alert_keys(alert_sink))

    # And the index stays rebuild-equivalent after the post-rebuild tick.
    after_tick = _milestones(app)
    assert _rebuild(app) == 0
    assert _milestones(app) == after_tick


def _peak_ref(app, minute):
    conn = app.open_db()
    try:
        return conn.execute(
            "SELECT journal_id FROM weekly_usage_snapshots"
            " WHERE unixepoch(captured_at_utc) = ?",
            (NOW + minute * 60,)).fetchone()[0]
    finally:
        conn.close()


def _suppressing_events(peak_ref):
    return [r for r in _journal() if r.get("t") == "evt"
            and peak_ref in ((r.get("payload") or {}).get("suppression") or [])]


@pytest.mark.parametrize("credit", ["record-credit", "weekly-drop"])
def test_rebuild_keeps_milestones_whose_snapshot_a_weekly_credit_suppressed(
        app, monkeypatch, tmp_path, alert_sink, capsys, credit):
    """The weekly suppressions ride the same resolver.

    `record-credit` (`weekly_credit_effects`, order 50) floors its effective
    instant to the hour, so its band reaches back over the genuine peak tick
    the 12% weekly milestone was recorded against — the same defect as the
    five-hour credit, on the manual remedy's path. Since spec 4.7 revision 7
    the tick, which carries its own five-hour reading, is kept weekly-held
    rather than deleted, and the milestone keeps its reference.

    The automatic weekly paths cannot reach it: since #750 S3 §1.4 a
    `week_reset` bands from the EXACT capture second of the triggering (or
    first-zero) observation, so no earlier tick is in its band. That case pins
    the unreachability, so a later return to a floored instant fails here
    rather than silently reintroducing the drop."""
    import _cctally_core as core
    core.CONFIG_PATH.write_text(json.dumps({"alerts": {
        "enabled": True, "weekly_thresholds": [12],
        "five_hour_thresholds": [35]}}))
    if credit == "record-credit":
        plan = ((13, 10.0, 30.0), (17, 11.0, 34.0), (21, 12.0, 38.0),
                (24, 12.0, 39.0))
    else:
        plan = ((13, 30.0, 30.0), (17, 31.0, 34.0), (21, 32.0, 38.0),
                (24, 2.0, 39.0))
    for minute, weekly, five in plan[:3]:
        assert _drive(app, monkeypatch, tmp_path, minute, weekly, five) == 0
    peak_ref = _peak_ref(app, PEAK_MINUTE)
    if credit == "record-credit":
        assert _drive(app, monkeypatch, tmp_path, *plan[3]) == 0
        _tick(monkeypatch, 25 * 60)
        capsys.readouterr()
        args = app.build_parser().parse_args(
            ["record-credit", "--to", "5", "--at", _iso(NOW + 20 * 60),
             "--yes", "--json"])
        assert args.func(args) == 0, capsys.readouterr()
    else:
        assert _drive(app, monkeypatch, tmp_path, *plan[3]) == 0
    live = _milestones(app)
    if credit == "record-credit":
        assert _suppressing_events(peak_ref), (
            "non-vacuity: the credit's suppression names the peak tick")
        # #825/#832 spec 4.7 revision 7 (fix round K16b): the peak tick
        # carries its own five-hour reading, so the weekly cleanup retires
        # only its weekly effect and keeps it weekly-held. The milestone keeps
        # its reference to the retained row (held semantics: an already
        # recorded milestone keeps its identity, value and multiplicity).
        assert any(json.loads(r)["usage_snapshot"] == peak_ref
                   for r in live["percent_milestones"]), live
        conn = app.open_db()
        try:
            assert tuple(conn.execute(
                "SELECT weekly_observation_held, five_hour_percent "
                "FROM weekly_usage_snapshots WHERE journal_id = ?",
                (peak_ref,)).fetchone()) == (1, 38.0)
        finally:
            conn.close()
    else:
        assert not _suppressing_events(peak_ref)
        assert any(r.get("t") == "evt"
                   and (r.get("payload") or {}).get("kind") == "week_reset"
                   for r in _journal()), "non-vacuity: the weekly drop fired"

    assert _rebuild(app) == 0
    assert _milestones(app) == live
