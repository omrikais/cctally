"""#825/#832 fix round K14b: the record path's accept/skip fold materializes
the first post-floor reading of each axis (spec 4.7 *First reading after a
floor*, revision 6; Codex design-fix-round-6 Q1 and Q2(c)).

Decisive tests of spec section 20 covered here:

- 38  fold boundaries on both axes: a capture immediately before and exactly
      at the floor, equal values across different window keys, different
      weekly ends that share a date, held five-hour evidence, held rows that
      fail to establish weekly evidence, and a held write with no valid
      weekly basis. Each asserts one first write followed by ordinary
      deduplication.
- 39  shared floor resolution: effective five-hour events whose row ids
      disagree with their logical order, then a real rebuild that renumbers
      them. The fold, the projection and the retry identity select the same
      logical floor; foreign-account and foreign-window controls.
- 41  weekly evidence across a five-hour suppression, through the real
      detector pipeline, with identical logical state after a real rebuild
      and a real rederive.
- 42  versioned history and recovery: unversioned history keeps its old
      decisions, new-policy history reproduces the new ones, and a frozen
      plan keeps its revision, payload and identity across the append and
      commit crash boundaries with no duplicate observation.

Every recording goes through the production bound kernel
(`record_usage_bound`) and the real ingest pipeline. Floors that test 38
needs at an exact instant are inserted as stats rows: the fold reads them
exactly as it reads a detected floor.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time

import pytest

import test_account_bound_replay_m19 as m19
from conftest import load_script, redirect_paths

#: 2026-10-08T00:00:00Z: a multiple of the ten-minute credit floor, so a
#: credit armed at ``NOW + a`` (a < 600) takes effect at exactly ``NOW``.
NOW = 1791417600
FIVE_RESET = NOW + 4 * 3600
WEEK_RESET = NOW + 3 * 86400


def _iso(epoch):
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
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
    cctally.open_db().close()
    return cctally


def _identity(name="A"):
    import _cctally_core as core
    import _lib_accounts
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": name, "emailAddress": f"{name}@example.test",
        "plan": "max"}}))
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _lib_accounts.account_key("claude", name)


def _record(app, monkeypatch, at, *, weekly, five, five_reset=FIVE_RESET,
            week_reset=WEEK_RESET, source="statusline", name="A"):
    """One bound `record-usage` reading through the production kernel."""
    import _cctally_statusline as sl
    _tick(monkeypatch, at)
    _identity(name)
    binding = sl._capture_binding()
    args = argparse.Namespace(
        percent=weekly, resets_at=str(week_reset), five_hour_percent=five,
        five_hour_resets_at=None if five is None else str(five_reset),
        source=source)
    assert app.record_usage_bound(args, binding=binding, nudge_dashboard=False) == 0
    return binding.account_key


_ROW_COLUMNS = (
    "captured_at_utc", "week_start_date", "week_end_date", "week_start_at",
    "week_end_at", "weekly_percent", "five_hour_percent",
    "five_hour_resets_at", "five_hour_window_key", "weekly_observation_held",
    "journal_id")


def _rows(app, account=None):
    conn = app.open_db()
    try:
        sql = f"SELECT {', '.join(_ROW_COLUMNS)} FROM weekly_usage_snapshots"
        args = ()
        if account is not None:
            sql += " WHERE account_key = ?"
            args = (account,)
        return [dict(zip(_ROW_COLUMNS, row))
                for row in conn.execute(sql + " ORDER BY captured_at_utc, id", args)]
    finally:
        conn.close()


def _execute(app, sql, args=()):
    conn = app.open_db()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _five_hour_floor(app, account, key, at, *, row_id=None):
    """An effective five-hour reset event at exactly ``NOW + at``."""
    _execute(
        app,
        "INSERT INTO five_hour_reset_events (id, detected_at_utc, "
        "five_hour_window_key, prior_percent, post_percent, "
        "effective_reset_at_utc, account_key) VALUES (?,?,?,?,?,?,?)",
        (row_id, _iso(NOW + at), int(key), 70.0, 20.0, _iso(NOW + at), account))


def _weekly_floor(app, account, week_start_date, at):
    """A weekly credit floor (the `record-credit` table) at ``NOW + at``."""
    _execute(
        app,
        "INSERT INTO weekly_credit_floors (week_start_date, effective_at_utc, "
        "observed_pre_credit_pct, applied_at_utc, account_key) "
        "VALUES (?,?,?,?,?)",
        (week_start_date, _iso(NOW + at), 40.0, _iso(NOW + at), account))


def _snapshot_insert(app, account, *, at, base, **changes):
    """A directly inserted snapshot row: ``base`` with ``changes``."""
    row = {k: base[k] for k in _ROW_COLUMNS if k != "journal_id"}
    row.update(captured_at_utc=_iso(NOW + at), **changes)
    # A logical identity, as every materialized row has: a milestone the
    # next tick derives against this row references it by journal id.
    row["journal_id"] = f"sa:fixture-{at}"
    columns = list(row) + ["source", "payload_json", "account_key"]
    _execute(
        app,
        f"INSERT INTO weekly_usage_snapshots ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(row.values()) + ("statusline", "{}", account))


def _projection(account, at):
    import _cctally_statusline as sl
    return sl._read_db_projection_once(account_key=account, at=NOW + at)


def _fold(app, payload, **kwargs):
    import _cctally_journal as jr
    conn = app.open_db()
    try:
        return jr._usage_snapshot_fold_decision(conn, payload, **kwargs)
    finally:
        conn.close()


def _first_write_then_dedup(app, monkeypatch, account, *, at, **values):
    """Record ``values`` three times at ``at``, ``at + 1`` and ``at + 2``:
    exactly one new row, then two ordinary deduplications."""
    before = len(_rows(app, account))
    counts = []
    for step in range(3):
        _record(app, monkeypatch, at + step, **values)
        counts.append(len(_rows(app, account)) - before)
    return counts


# ── Test 38: fold boundaries ───────────────────────────────────────────────


def test_38_five_hour_capture_before_and_exactly_at_the_floor(app, monkeypatch):
    """A capture one second before the five-hour floor is not the first
    post-floor reading; the capture exactly at the floor is, once."""
    account = _record(app, monkeypatch, 0, weekly=20, five=50)
    [base] = _rows(app, account)
    _five_hour_floor(app, account, base["five_hour_window_key"], 100)
    _record(app, monkeypatch, 99, weekly=20, five=50)
    assert len(_rows(app, account)) == 1, (
        "a pre-floor capture counted as the first post-floor reading")
    counts = _first_write_then_dedup(app, monkeypatch, account, at=100,
                                     weekly=20, five=50)
    assert counts == [1, 1, 1], (
        f"the capture at the five-hour floor did not materialize once: {counts}"
        f"\n  rows: {_rows(app, account)}")
    assert _rows(app, account)[-1]["captured_at_utc"] == _iso(NOW + 100)


def test_38_weekly_capture_before_and_exactly_at_the_floor(app, monkeypatch):
    """The weekly axis: a credit floor at ``NOW + 100``; the capture one
    second before is not post-floor, the one at the floor writes once."""
    account = _record(app, monkeypatch, 0, weekly=20, five=50)
    [base] = _rows(app, account)
    _weekly_floor(app, account, base["week_start_date"], 100)
    _record(app, monkeypatch, 99, weekly=20, five=50)
    assert len(_rows(app, account)) == 1, (
        "a pre-floor capture counted as the first post-floor weekly reading")
    counts = _first_write_then_dedup(app, monkeypatch, account, at=100,
                                     weekly=20, five=50)
    assert counts == [1, 1, 1], (
        f"the capture at the weekly floor did not materialize once: {counts}"
        f"\n  rows: {_rows(app, account)}")
    written = _rows(app, account)[-1]
    assert (written["weekly_observation_held"], written["weekly_percent"]) == (0, 20.0)
    assert _projection(account, 110).seven_day.captured_at == NOW + 100


def test_38_equal_five_hour_values_across_different_window_keys(app, monkeypatch):
    """An equal five-hour value in a different canonical window is new
    evidence: one write in the new window, then ordinary deduplication."""
    account = _record(app, monkeypatch, 0, weekly=20, five=0)
    counts = _first_write_then_dedup(
        app, monkeypatch, account, at=60, weekly=20, five=0,
        five_reset=FIVE_RESET + 3600)
    rows = _rows(app, account)
    assert counts == [1, 1, 1], (
        f"an equal value in another five-hour window was deduplicated: {counts}"
        f"\n  rows: {rows}")
    assert rows[0]["five_hour_window_key"] != rows[1]["five_hour_window_key"]


def test_38_different_weekly_ends_that_share_a_date(app, monkeypatch):
    """A date-only legacy row (no weekly end) shares the tick's week start
    date. It is not weekly evidence of the tick's normalized window, so the
    first reading with an end writes once and later ones deduplicate."""
    account = _identity()
    week_start = dt.datetime.fromtimestamp(WEEK_RESET - 7 * 86400, dt.timezone.utc)
    _execute(
        app,
        "INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date, "
        "week_end_date, week_start_at, week_end_at, weekly_percent, source, "
        "payload_json, account_key, journal_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (_iso(NOW - 60), week_start.date().isoformat(),
         (week_start + dt.timedelta(days=6)).date().isoformat(), None, None,
         20.0, "statusline", "{}", account, "sa:fixture-date-only"))
    counts = _first_write_then_dedup(app, monkeypatch, account, at=0,
                                     weekly=20, five=None)
    rows = _rows(app, account)
    assert counts == [1, 1, 1], (
        f"a date-only row stood in for the tick's weekly window: {counts}"
        f"\n  rows: {rows}")
    assert rows[0]["week_end_at"] is None and rows[1]["week_end_at"] is not None
    assert _projection(account, 10).seven_day.percent == 20.0


def test_38_held_five_hour_evidence_writes_once_and_counts(app, monkeypatch):
    """A weekly-held tick with no five-hour row since the five-hour floor
    writes its held row once; that held row is five-hour evidence for later
    held and unheld ticks."""
    account = _record(app, monkeypatch, 0, weekly=30, five=40)
    [base] = _rows(app, account)
    _five_hour_floor(app, account, base["five_hour_window_key"], 100)
    counts = _first_write_then_dedup(app, monkeypatch, account, at=110,
                                     weekly=25, five=40)
    rows = _rows(app, account)
    assert counts == [1, 1, 1], (
        f"the held first post-floor five-hour reading did not write once: "
        f"{counts}\n  rows: {rows}")
    held = rows[-1]
    assert (held["weekly_observation_held"], held["weekly_percent"],
            held["five_hour_percent"]) == (1, 30.0, 40.0)
    assert held["week_end_at"] == base["week_end_at"]
    _record(app, monkeypatch, 130, weekly=30, five=40)
    assert len(_rows(app, account)) == 2, (
        "held five-hour evidence was not counted for an unheld tick")


def test_38_held_rows_never_establish_weekly_evidence(app, monkeypatch):
    """After a weekly floor the only post-floor row is held: it carries no
    weekly observation, so the first unheld post-floor reading writes once."""
    account = _record(app, monkeypatch, 0, weekly=30, five=40)
    [base] = _rows(app, account)
    _weekly_floor(app, account, base["week_start_date"], 100)
    _snapshot_insert(app, account, at=110, base=base, five_hour_percent=41.0,
                     weekly_observation_held=1)
    counts = _first_write_then_dedup(app, monkeypatch, account, at=120,
                                     weekly=30, five=41)
    rows = _rows(app, account)
    assert counts == [1, 1, 1], (
        f"a held row stood in for post-floor weekly evidence: {counts}"
        f"\n  rows: {rows}")
    assert rows[-1]["weekly_observation_held"] == 0
    assert _projection(account, 130).seven_day.captured_at == NOW + 120


def test_38_held_write_without_a_weekly_basis_is_refused(app, monkeypatch):
    """New five-hour evidence on a weekly-held tick that has no accepted
    weekly basis writes nothing: a weekly observation is never fabricated.
    (A boundary control: the refusal predates revision 6 and must survive.)"""
    import _cctally_journal as jr
    account = _identity()
    week_end = dt.datetime.fromtimestamp(WEEK_RESET, dt.timezone.utc)
    fold = _fold(app, {
        "week_start_date": (week_end - dt.timedelta(days=7)).date().isoformat(),
        "week_start_at": (week_end - dt.timedelta(days=7)).isoformat(),
        "week_end_at": week_end.isoformat(),
        "weekly_percent": 20.0, "five_hour_percent": 40.0,
        "five_hour_window_key": int(app._canonical_5h_window_key(FIVE_RESET)),
        "account_key": account, "captured_at": _iso(NOW),
        "fold_policy": 1,
    }, force_weekly_hold=True, reviewed_weekly_basis=None)
    assert fold.weekly.disposition == jr.WEEKLY_HELD_CLAMP
    assert fold.snapshot_action == jr.SNAPSHOT_SKIP_NO_CHANGE


# ── Test 39: shared floor resolution ───────────────────────────────────────


def _two_own_credits(app, monkeypatch):
    """Two real in-place five-hour credits in one window: 70 -> 20 -> 20
    (effective at NOW), a rise to 40, then 80 -> 20 -> 20 (effective at
    NOW + 600). Returns the account and the window key."""
    account = _record(app, monkeypatch, 0, weekly=20, five=70)
    for at, five in ((2, 20), (4, 20), (400, 40), (608, 80), (610, 20), (612, 20)):
        _record(app, monkeypatch, at, weekly=20, five=five)
    conn = app.open_db()
    try:
        events = conn.execute(
            "SELECT id, effective_reset_at_utc, five_hour_window_key "
            "FROM five_hour_reset_events WHERE account_key = ? ORDER BY id",
            (account,)).fetchall()
    finally:
        conn.close()
    assert [e[1] for e in events] == [
        _iso(NOW).replace("Z", "+00:00"), _iso(NOW + 600).replace("Z", "+00:00")], (
        f"setup: two credits effective at NOW and NOW + 600: {events}")
    return account, int(events[0][2])


def _probe_payload(app, account, key, at, five):
    week_end = dt.datetime.fromtimestamp(WEEK_RESET, dt.timezone.utc)
    return {
        "week_start_date": (week_end - dt.timedelta(days=7)).date().isoformat(),
        "week_start_at": (week_end - dt.timedelta(days=7)).isoformat(),
        "week_end_at": week_end.isoformat(),
        "weekly_percent": 20.0, "five_hour_percent": float(five),
        "five_hour_window_key": key, "account_key": account,
        "captured_at": _iso(NOW + at), "fold_policy": 1,
    }


def _floor_view(app, account, key, at):
    """What the fold, the projection and the retry identity each select."""
    import _lib_statusline_candidates as candidates
    fold = _fold(app, _probe_payload(app, account, key, at, 25))
    projection = _projection(account, at).five_hour
    value = candidates.AxisValue(25.0, FIVE_RESET, key)
    return {
        "fold": (fold.five_hour.disposition, fold.five_hour.effective_pct),
        "generation": projection.reset_generation,
        "percent": projection.percent,
        "signature": candidates._build_retry_signature(value, projection),
    }


def test_39_fold_projection_and_retry_identity_share_the_logical_floor(
        app, monkeypatch):
    """The earlier credit's row is renumbered ABOVE the later one, so row
    order and logical order disagree. The fold's in-window maximum must use
    the logical (later) floor, as the projection does; a real rebuild that
    renumbers the rows changes none of the three selections."""
    import _cctally_journal as jr
    account, key = _two_own_credits(app, monkeypatch)
    _execute(app, "UPDATE five_hour_reset_events SET id = id + 1000 "
                  "WHERE account_key = ? AND effective_reset_at_utc = ?",
             (account, _iso(NOW).replace("Z", "+00:00")))
    before = _floor_view(app, account, key, 700)
    assert before["generation"].startswith(_iso(NOW + 600).replace("Z", "+00:00")), (
        f"projection floor: {before}")
    assert before["fold"] == (jr.FIVE_HOUR_AS_OBSERVED, 25.0), (
        "the fold resolved the five-hour floor by row id: the pre-floor 40 "
        f"clamped a post-floor reading ({before['fold']})")

    args = app.build_parser().parse_args(["db", "rebuild", "--db", "stats", "--json"])
    assert args.func(args) == 0
    conn = app.open_db()
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM five_hour_reset_events WHERE account_key = ? "
            "ORDER BY effective_reset_at_utc", (account,))]
    finally:
        conn.close()
    assert ids == sorted(ids) and max(ids) < 1000, (
        f"the rebuild must renumber the rows into logical order: {ids}")
    assert _floor_view(app, account, key, 700) == before


@pytest.mark.parametrize("foreign", ["account", "window"])
def test_39_foreign_events_never_move_the_shared_floor(app, monkeypatch, foreign):
    """A later event of ANOTHER account in the same window, or of the same
    account in another window, with the highest row id, changes neither the
    fold nor the projection nor the retry identity."""
    account, key = _two_own_credits(app, monkeypatch)
    before = _floor_view(app, account, key, 700)
    if foreign == "account":
        _five_hour_floor(app, _identity("B"), key, 650, row_id=5000)
    else:
        _five_hour_floor(app, account, key + 5 * 3600, 650, row_id=5000)
    assert _floor_view(app, account, key, 700) == before


# ── Test 41: weekly evidence across a five-hour suppression ────────────────


def _suppression_schedule(app, monkeypatch, shape):
    """A real confirmed five-hour credit whose stale-replica cleanup reaches
    a row carrying accepted weekly evidence.

    ``value``: the OAuth reader (source ``api``) observes the weekly rise to
    21 while the five-hour surfaces still show the pre-credit 70; the
    statusline, still on the old weekly 20, then confirms its own descent.
    ``capture``: one source; the arming tick itself carries the weekly rise
    to 21, and the fold clamps its five-hour reading back to 70."""
    m19._retain_session_input(app, "A", _identity())
    account = _record(app, monkeypatch, 0, weekly=20, five=70)
    if shape == "value":
        _record(app, monkeypatch, 60, weekly=20, five=20)
        _record(app, monkeypatch, 120, weekly=21, five=70, source="api")
        _record(app, monkeypatch, 180, weekly=20, five=20)
        evidence_at, last = 120, 180
    else:
        _record(app, monkeypatch, 60, weekly=21, five=20)
        _record(app, monkeypatch, 120, weekly=21, five=20)
        evidence_at, last = 60, 120
    conn = app.open_db()
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM five_hour_reset_events WHERE account_key = ?",
            (account,)).fetchone()[0]
    finally:
        conn.close()
    assert events == 1, "setup: the five-hour credit was confirmed"
    return account, evidence_at, last


def _weekly_view(app, account, at):
    projection = _projection(account, at)
    return (projection.seven_day.percent, projection.seven_day.captured_at,
            None if projection.five_hour is None else projection.five_hour.percent)


@pytest.mark.parametrize("shape", ["value", "capture"])
def test_41_five_hour_suppression_keeps_accepted_weekly_evidence(
        app, monkeypatch, shape):
    """The five-hour credit's cleanup retires the stale replica's five-hour
    effect, but the accepted weekly 21 keeps its value, capture, boundary
    and (unheld) provenance; live, rebuilt and rederived state agree."""
    account, evidence_at, last = _suppression_schedule(app, monkeypatch, shape)
    rows = _rows(app, account)
    evidence = [r for r in rows if r["captured_at_utc"] == _iso(NOW + evidence_at)]
    assert evidence and evidence[0]["weekly_percent"] == 21.0, (
        "the five-hour cleanup deleted the row carrying the accepted weekly "
        f"21 captured at NOW + {evidence_at}\n  rows: {rows}")
    [kept] = evidence
    assert kept["weekly_observation_held"] == 0
    assert kept["week_end_at"] is not None
    assert (kept["five_hour_percent"], kept["five_hour_resets_at"],
            kept["five_hour_window_key"]) == (None, None, None), (
        f"the stale replica's five-hour effect was not retired: {kept}")
    assert not [r for r in rows if r["five_hour_percent"] == 70.0
                and r["captured_at_utc"] >= _iso(NOW)], rows
    live_view = _weekly_view(app, account, last + 10)
    assert live_view == (21.0, NOW + 120, 20.0), live_view

    live = m19._logical_state(app)
    args = app.build_parser().parse_args(["db", "rebuild", "--db", "stats", "--json"])
    assert args.func(args) == 0
    m19._assert_same(live, m19._logical_state(app))
    assert _weekly_view(app, account, last + 10) == live_view
    args = app.build_parser().parse_args(
        ["db", "rederive", "--family", "claude-usage", "--yes", "--json"])
    assert args.func(args) == 0
    m19._assert_same(live, m19._logical_state(app))
    assert _weekly_view(app, account, last + 10) == live_view


# ── Test 42: versioned history and recovery ────────────────────────────────


def _unversioned(monkeypatch):
    """Record as the previous release did: no fold-policy revision on the
    raw observation."""
    import _lib_journal
    real = _lib_journal.make_obs

    def make_obs(**kwargs):
        payload = dict(kwargs.get("payload") or {})
        payload.pop("fold_policy", None)
        return real(**{**kwargs, "payload": payload})

    monkeypatch.setattr(_lib_journal, "make_obs", make_obs)


def _journal():
    import _cctally_core as core
    records = []
    for path in sorted(core.JOURNAL_DIR.glob("*.jsonl")):
        records.extend(json.loads(line) for line in path.read_text().splitlines()
                       if line.strip())
    return records


def _usage_obs():
    return [r for r in _journal()
            if r.get("t") == "obs" and r.get("provider") == "claude"]


def _equal_value_credit(app, monkeypatch):
    """K14a S1's schedule: 70 -> 20 -> 20, then 80 -> 20 -> 20."""
    m19._retain_session_input(app, "A", _identity())
    account = _record(app, monkeypatch, 0, weekly=20, five=70)
    for at, five in ((2, 20), (4, 20), (608, 80), (610, 20), (612, 20)):
        _record(app, monkeypatch, at, weekly=20, five=five)
    return account


def _post_floor_five_hour_rows(app, account):
    return [r for r in _rows(app, account)
            if r["five_hour_percent"] is not None
            and r["captured_at_utc"] >= _iso(NOW + 600)]


def _cross_key_equal_value(app, monkeypatch):
    """An equal reading in the next five-hour window: the historical fold
    compares no window key and deduplicates it; the first-post-floor fold
    counts it as new evidence (test 38's cross-key rule)."""
    m19._retain_session_input(app, "A", _identity())
    account = _record(app, monkeypatch, 0, weekly=20, five=0)
    _record(app, monkeypatch, 60, weekly=20, five=0,
            five_reset=FIVE_RESET + 3600)
    return account


def _second_window_rows(app, account):
    first = _rows(app, account)[0]["five_hour_window_key"]
    return [r for r in _rows(app, account)
            if r["five_hour_window_key"] not in (None, first)]


@pytest.mark.parametrize("schedule", ["cross_key", "equal_value_credit"])
@pytest.mark.parametrize("history", ["unversioned", "versioned"])
def test_42_each_observation_keeps_its_recorded_fold_policy(
        app, monkeypatch, history, schedule):
    """``cross_key``: unversioned history keeps the historical decision (the
    equal reading in the next five-hour window is deduplicated);
    new-policy history writes it. ``equal_value_credit`` (K14a S1): since
    fix round K15 a five-hour credit's cleanup retires the stale replica in
    place under every policy, so the historical fold's latest row is that
    retired replica, which carries no five-hour reading, and the historical
    fold too writes the first post-floor reading. A real rebuild and a real
    rederive reproduce each history's own decisions."""
    if history == "unversioned":
        _unversioned(monkeypatch)
    if schedule == "cross_key":
        account = _cross_key_equal_value(app, monkeypatch)
    else:
        account = _equal_value_credit(app, monkeypatch)
    policies = {r["payload"].get("fold_policy") for r in _usage_obs()}
    expected_policies = {None} if history == "unversioned" else {1}
    assert policies == expected_policies, (
        f"raw observations carry fold policies {policies}")
    if schedule == "cross_key":
        second = _second_window_rows(app, account)
        if history == "unversioned":
            assert second == [], (
                f"an unversioned observation was re-policied: {second}")
        else:
            assert [r["captured_at_utc"] for r in second] == [_iso(NOW + 60)], (
                "the new policy did not write the next window's equal "
                f"reading: {_rows(app, account)}")
    else:
        post_floor = _post_floor_five_hour_rows(app, account)
        assert [(r["captured_at_utc"], r["five_hour_percent"])
                for r in post_floor] == [(_iso(NOW + 612), 20.0)], (
            "the first post-floor reading was not written: "
            f"{_rows(app, account)}")
    # An unversioned observation recorded by the CURRENT binary is an
    # artificial history (production stamps every new observation with
    # policy 1): its live block rollup still opens the next window's block
    # for the deduplicated reading, with no journal identity, which replay
    # cannot reproduce. That rollup is not what this test decides, so the
    # block families are compared only where the live path is real.
    blocks = ("five_hour_blocks", "five_hour_block_models",
              "five_hour_block_projects")

    def state():
        logical = m19._logical_state(app)
        if history == "unversioned" and schedule == "cross_key":
            for table in blocks:
                logical.pop(table, None)
        return logical

    live = state()
    for argv in (["db", "rebuild", "--db", "stats", "--json"],
                 ["db", "rederive", "--family", "claude-usage", "--yes", "--json"]):
        args = app.build_parser().parse_args(argv)
        assert args.func(args) == 0
        m19._assert_same(live, state())


def _spool(app, monkeypatch, at, *, weekly, five):
    import _cctally_statusline as sl
    _tick(monkeypatch, at)
    _identity()
    candidate = sl._candidate_from_input(app._lib_statusline.parse_statusline_stdin(
        json.dumps({"session_id": "peer", "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": _iso(FIVE_RESET)},
            "seven_day": {"used_percentage": weekly, "resets_at": _iso(WEEK_RESET)},
        }}).encode()), received_at=NOW + at)
    assert candidate is not None
    sl._write_candidate(candidate)
    return candidate


def _reduce():
    import _cctally_statusline as sl
    return sl._statusline_reduce_and_publish(nudge_sink=lambda: None)


def _publication_obs():
    return [r for r in _usage_obs() if r["payload"].get("publication_id")]


def _previous_release_plan(plan, account):
    """The same frozen plan as the previous release froze it: no fold-policy
    revision, and the identity that release hashed without one."""
    import _lib_statusline_candidates as candidates
    old = {k: v for k, v in plan.items() if k != "foldPolicy"}
    frozen = {k: old[k] for k in ("capturedAt", "binding", "sevenDay", "fiveHour",
                                  "fiveHourWindowKey", "provenance")}
    old["publicationId"] = hashlib.sha256(json.dumps(
        {"protocolGeneration": candidates.PROTOCOL_GENERATION,
         "account": account, **frozen},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return old


@pytest.mark.parametrize("plan_era", ["previous", "current"])
@pytest.mark.parametrize("boundary", ["before_append", "after_append", "before_commit"])
def test_42_a_frozen_plan_keeps_its_revision_payload_and_identity(
        app, monkeypatch, boundary, plan_era):
    """A publication stopped at each durable boundary recovers with the
    revision it was frozen with. A current plan carries the current fold
    policy and so does its one observation. A plan the previous release
    froze (and, past the append boundary, appended) carries none, so its
    observation selects the historical policy, and recovery keeps its
    payload and identity. Either way exactly one observation exists."""
    import _cctally_journal as journal
    import _cctally_statusline as sl
    import _lib_journal
    real_freeze = sl._bound_publication_plan
    real_make_obs = _lib_journal.make_obs
    if plan_era == "previous":
        _unversioned(monkeypatch)

        def freeze(decision, projection, candidates, binding, **kwargs):
            plan = real_freeze(decision, projection, candidates, binding, **kwargs)
            return None if plan is None else _previous_release_plan(
                plan, binding.account_key)

        monkeypatch.setattr(sl, "_bound_publication_plan", freeze)
    candidate = _spool(app, monkeypatch, 0, weekly=20, five=30)
    account = candidate.binding.account_key
    if boundary == "before_append":
        owner, name = app, "cmd_record_usage"
    elif boundary == "after_append":
        owner, name = journal, "run_stats_ingest"
    else:
        owner, name = sl, "_commit_publication"
    real = getattr(owner, name)

    def stop(*args, **kwargs):
        if boundary == "after_append" and not _publication_obs():
            return real(*args, **kwargs)
        raise RuntimeError("process stop")

    monkeypatch.setattr(owner, name, stop)
    assert _reduce()[0].status == "record_failed"
    # The upgrade: the current binary, unpatched, recovers the plan.
    import _lib_journal
    monkeypatch.setattr(owner, name, real)
    monkeypatch.setattr(sl, "_bound_publication_plan", real_freeze)
    monkeypatch.setattr(_lib_journal, "make_obs", real_make_obs)
    plan = sl._read_transition(account, now_epoch=NOW).publication
    assert plan is not None
    if plan_era == "current":
        assert plan.get("foldPolicy") == 1, (
            f"a newly frozen plan carries no fold-policy revision: {sorted(plan)}")
    else:
        assert "foldPolicy" not in plan
    appended = _publication_obs()
    assert len(appended) == (0 if boundary == "before_append" else 1)
    _identity("B")
    _tick(monkeypatch, 100)
    results = _reduce()
    assert [(r.account_key, r.status) for r in results] == [(account, "recovered")]
    records = _publication_obs()
    assert len(records) == 1, f"{len(records)} observations for one plan"
    if appended:
        assert records == appended
    payload = records[0]["payload"]
    assert payload["publication_id"] == plan["publicationId"]
    assert payload.get("fold_policy") == plan.get("foldPolicy")
    assert records[0]["at"] == _iso(NOW)
    assert sl._read_transition(account, now_epoch=NOW + 100).publication is None
    assert len(_rows(app, account)) == 1
