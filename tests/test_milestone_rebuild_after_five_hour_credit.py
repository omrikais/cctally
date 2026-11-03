"""A stats rebuild keeps every milestone a journaled suppression orphaned.

#825/#832 Task 4.0 (spec 4.7, case M19). A five-hour in-place credit's
stale-replica DELETE used to remove every snapshot from floor10(arming tick)
within 1pp of the pre-credit peak. That band also held the GENUINE peak tick
when it shared the arming tick's ten-minute slot, and that tick is the one that
crossed the last weekly and five-hour milestones. Since #943 the band starts at
the arming observation's exact capture second (the `five_hour_credit_boundary`
companion), so the genuine peak is kept; the rebuild cases below still pin the
milestone resolver for a suppression that names a crossing tick. A rebuild folds the suppression
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


def _snapshot_rows(app):
    """``{minute: (weekly, five-hour, held)}`` of every snapshot row."""
    conn = app.open_db()
    try:
        return {
            (int(r[0]) - NOW) // 60: (r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT unixepoch(captured_at_utc), weekly_percent,"
                " five_hour_percent, weekly_observation_held"
                " FROM weekly_usage_snapshots")}
    finally:
        conn.close()


def _assert_trace_d_rows(app, peak_ref, label, *, later=None):
    """Trace D's rows after its five-hour credit (#943, spec 6.2): the genuine
    00:21 peak keeps its five-hour 38 and its weekly 12, unheld; no 00:24 row
    carries the clamped 38; the post-credit 00:26 reads its own 3 (and
    ``later`` the minute and five-hour value of a further post-credit tick);
    the five-hour milestones the peak crossed keep a real
    ``usage_snapshot_id`` naming it."""
    rows = _snapshot_rows(app)
    assert rows[PEAK_MINUTE] == (12.0, 38.0, 0), (
        f"{label}: genuine 00:21 peak lost or retired: {sorted(rows.items())}")
    assert rows.get(24, (None, None, None))[1] != 38.0, (
        f"{label}: the 00:24 clamped replica survived")
    assert rows[26][1] == 3.0, f"{label}: credit undone at 00:26: {rows}"
    if later is not None:
        assert rows[later[0]][1] == later[1], (
            f"{label}: credit undone at {later[0]}: {rows}")
    conn = app.open_db()
    try:
        crossed = conn.execute(
            "SELECT m.percent_threshold, m.usage_snapshot_id, s.journal_id"
            " FROM five_hour_milestones m"
            " LEFT JOIN weekly_usage_snapshots s ON s.id = m.usage_snapshot_id"
            " WHERE unixepoch(m.captured_at_utc) = ?",
            (NOW + PEAK_MINUTE * 60,)).fetchall()
    finally:
        conn.close()
    assert crossed, f"{label}: non-vacuity: the peak crossed five-hour milestones"
    assert all(int(r[1]) != 0 and r[2] == peak_ref for r in crossed), (
        f"{label}: a peak milestone lost its snapshot link: "
        f"{[tuple(r) for r in crossed]}")


@pytest.mark.parametrize("policy", ["historical", "first_post_floor"])
def test_rebuild_keeps_milestones_whose_snapshot_a_five_hour_credit_suppressed(
        app, monkeypatch, tmp_path, alert_sink, policy):
    """Under either fold policy the credit keeps the genuine peak tick and its
    milestones (#943; before it the floored band retired the peak's five-hour
    effect, spec 4.7 revision 6, fix round K15). The milestones survive a
    rebuild and the next tick does not wedge ingest.

    Detects (#943 F2): a five-hour credit banded from the floored ten-minute
    slot of its arming DETECTION clock retires the genuine 00:21 peak of 38
    captured before the arming 00:24 observation, under either fold policy;
    and the older policy's inline clamp floor must move with the boundary, or
    the post-credit 3 is clamped back up to that kept 38."""
    if policy == "historical":
        _record_historical_policy(monkeypatch)
    peak_ref = _trace_d(app, monkeypatch, tmp_path, alert_sink)
    assert peak_ref, "the :21 peak tick was accepted and journaled"

    # Non-vacuity: the credit fired, and its one suppression list no longer
    # names the genuine peak tick (#943 inverts the K15-era pin): the band
    # starts at the arming observation's exact capture second.
    credits = [r for r in _journal() if r.get("t") == "evt"
               and (r.get("payload") or {}).get("kind") == "five_hour_credit"]
    assert len(credits) == 1, credits
    assert "five_hour_retirement" not in credits[0]["payload"]
    assert peak_ref not in (credits[0]["payload"].get("suppression") or [])
    _assert_trace_d_rows(app, peak_ref, "live")
    live = _milestones(app)
    peak_pm = [json.loads(r) for r in live["percent_milestones"]
               if json.loads(r)["percent_threshold"] == 12]
    assert peak_pm and peak_pm[0]["captured_at_utc"].startswith("2026-10-08T00:21")
    live_alerts = _alert_keys(alert_sink)
    assert ("weekly", 12) in live_alerts and ("five_hour", 35) in live_alerts

    assert _rebuild(app) == 0
    assert _milestones(app) == live
    _assert_trace_d_rows(app, peak_ref, "rebuilt")

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


def _replay(monkeypatch, replay):
    """One production replay of the live index: the epoch transition or the
    corrupt-index heal's detached worker (as
    `tests/test_account_bound_replay_m19.py` drives them)."""
    import os
    import types
    import _cctally_core as core
    import _cctally_journal as JR
    if replay == "epoch-transition":
        JR.run_epoch_transition(claude_json_path=str(core.CLAUDE_JSON_PATH))
        return
    import _cctally_db
    import _cctally_store as st
    import _cctally_update
    monkeypatch.setattr(_cctally_update, "_spawn_detached", lambda command: True)
    monkeypatch.setattr(_cctally_update, "_spawn_detached_with_pid",
                        lambda command, **_kw: os.getpid())
    with open(core.DB_PATH, "r+b") as handle:
        handle.write(b"not a database " * 200)
    with pytest.raises(_cctally_db.StatsHealDeferred):
        core.open_db()
    assert st.cmd_stats_corruption_heal_internal(types.SimpleNamespace()) == 0


@pytest.mark.parametrize("replay", ["epoch-transition", "corruption-heal"])
def test_five_hour_credit_keeps_the_genuine_peak_through_every_replay(
        app, monkeypatch, tmp_path, alert_sink, replay):
    """Trace D, then a post-credit 00:34 reading of 7 (#943 A3, spec 6.2).

    Detects: a replay path (the epoch transition, the corruption heal) that
    folds the credit without its exact boundary, so it bands from the floored
    slot again and retires the genuine 00:21 peak, or reads a post-credit
    value against the floored slot and clamps 3 and 7 back up to 38. The
    `db rebuild` path is the policy-parametrized test above."""
    peak_ref = _trace_d(app, monkeypatch, tmp_path, alert_sink)
    assert _drive(app, monkeypatch, tmp_path, 34, 12.0, 7.0) == 0
    _assert_trace_d_rows(app, peak_ref, "live", later=(34, 7.0))
    live_rows = _snapshot_rows(app)
    live = _milestones(app)
    _replay(monkeypatch, replay)
    _assert_trace_d_rows(app, peak_ref, replay, later=(34, 7.0))
    assert _snapshot_rows(app) == live_rows
    assert _milestones(app) == live


def _peak_ref(app, minute):
    conn = app.open_db()
    try:
        return conn.execute(
            "SELECT journal_id FROM weekly_usage_snapshots"
            " WHERE unixepoch(captured_at_utc) = ?",
            (NOW + minute * 60,)).fetchone()[0]
    finally:
        conn.close()


def _published_effective_at(app, minute):
    """#836 (D-D): the `effective_seven_day_pct_at_crossing` every surface
    publishes for the five-hour milestones crossed at ``minute``, read
    through the ONE milestone read (`_load_five_hour_milestones`) that the
    CLI breakdown, the live route and the history route share."""
    conn = app.open_db()
    try:
        blocks = sorted({int(r[0]) for r in conn.execute(
            "SELECT block_id FROM five_hour_milestones"
            " WHERE unixepoch(captured_at_utc) = ?", (NOW + minute * 60,))})
        out = []
        for block_id in blocks:
            out.extend(
                m["effective_seven_day_pct_at_crossing"]
                for m in app._load_five_hour_milestones(conn, block_id=block_id)
                if m["captured_at_utc"] == _iso(NOW + minute * 60))
        return out
    finally:
        conn.close()


def _suppressing_events(peak_ref):
    return [r for r in _journal() if r.get("t") == "evt"
            and peak_ref in ((r.get("payload") or {}).get("suppression") or [])]


@pytest.mark.parametrize("credit", ["record-credit", "weekly-drop"])
def test_rebuild_keeps_milestones_whose_snapshot_a_weekly_credit_suppressed(
        app, monkeypatch, tmp_path, alert_sink, capsys, credit):
    """The weekly suppressions ride the same resolver.

    `record-credit` (`weekly_credit_effects`, order 50) bands from its
    effective instant, which since #943 (O1) is the exact `--at` second
    (00:20) rather than its hour floor. The peak tick (00:21) is captured
    AFTER that second, so it is a genuine stale replica in the band and the
    suppression still names it. Since spec 4.7 revision 7 the tick, which
    carries its own five-hour reading, is kept weekly-held rather than
    deleted, and the milestone keeps its reference.

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
        # #943 (O1): unchanged by the exact `--at` second, because the 00:21
        # peak is captured after the 00:20 `--at` instant and so stays in the
        # band; a reading captured before `--at` is the case that changed
        # (`tests/test_record_credit.py`, the F3 trace).
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

    # #836 (D-D). The manual credit RETIRED the peak's weekly value: the row
    # is weekly-held by the cleanup, not by a held write (its payload carries
    # no `weeklyObservationHeld` marker), so the five-hour milestone it
    # crossed must publish no effective weekly value rather than the retired
    # 12. The automatic drop cannot suppress the earlier peak (above), so its
    # valid 32 is published. Detects: a retired weekly value rendered as an
    # ordinary effective `7D %` on the Current Week modal and the CLI.
    expected = None if credit == "record-credit" else 32.0
    published = _published_effective_at(app, PEAK_MINUTE)
    assert published, "non-vacuity: the peak tick crossed five-hour milestones"
    assert published == [expected] * len(published), ("live", published)

    assert _rebuild(app) == 0
    assert _milestones(app) == live
    assert _published_effective_at(app, PEAK_MINUTE) == published, (
        "after rebuild")


def test_a_genuine_held_writes_crossing_keeps_its_basis_value(
        app, monkeypatch, tmp_path, alert_sink):
    """#836 (D-D) control: a HELD WRITE (a weekly reading below the accepted
    one while the five-hour reading grew) stores the effective basis value
    with the `weeklyObservationHeld` payload marker. Only a suppression's
    retirement makes a row held WITHOUT that marker, so the crossing the held
    write records keeps publishing its basis value, live and after a
    rebuild. Detects: the retired-join predicate over-reaching onto clamped
    held writes, which would blank a valid `7D %`."""
    import _cctally_core as core
    core.CONFIG_PATH.write_text(json.dumps({"alerts": {
        "enabled": True, "five_hour_thresholds": [35]}}))
    for minute, weekly, five in ((13, 10.0, 30.0), (17, 11.0, 34.0),
                                 (21, 10.0, 38.0)):
        assert _drive(app, monkeypatch, tmp_path, minute, weekly, five) == 0
    conn = app.open_db()
    try:
        held, weekly, payload = conn.execute(
            "SELECT weekly_observation_held, weekly_percent, payload_json"
            " FROM weekly_usage_snapshots WHERE unixepoch(captured_at_utc) = ?",
            (NOW + 21 * 60,)).fetchone()
    finally:
        conn.close()
    assert (held, weekly) == (1, 11.0), "non-vacuity: 00:21 is a held write"
    assert json.loads(payload).get("weeklyObservationHeld") is True
    published = _published_effective_at(app, 21)
    assert published, "non-vacuity: the held write crossed five-hour 35"
    assert published == [11.0] * len(published), ("live", published)
    assert _rebuild(app) == 0
    assert _published_effective_at(app, 21) == published, "after rebuild"
