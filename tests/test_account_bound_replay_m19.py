"""#825/#832 case M19 (spec 4.7): one journal prefix yields the same logical
account-local outcomes through live ingest, a real `db rebuild --db stats` and
a real `db rederive`.

Ported from the rejected branch's
`test_replay_rebuild_and_real_rederive_preserve_bound_outcomes` with every
`accounting-authority.json` assertion removed (there is no store fork). Rowid
foreign keys are resolved to the logical (journal) identity of the row they
reference, never dropped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import typing

import pytest

from conftest import load_script, redirect_paths

NOW = 1791417600

#: The compared families and, per family, the columns that make up its
#: logical outcome (None = every column but `id`, the physical row id).
#: #825/#832 fix round O3 (825-832-CD4-V01): every family, five-hour blocks
#: included, is compared on every column but `id`; the only other column
#: left out is `_BLOCK_UNCOMPARED`'s (#956).
#:
#: The milestone tables joined in #825/#832 Task 4.0. A rebuild used to drop
#: every milestone whose snapshot a five-hour credit's stale-replica removal
#: deleted (percent_milestones 11 -> 1, five_hour_milestones 52 -> 2 on this
#: trace at 4c36730a4); `_FK_COLUMNS` resolves their rowid references to
#: logical identities so the comparison is between journaled facts.
#:
#: #825/#832 fix round C (review P3-10): the cost-bearing families and the
#: block children are compared too — weekly cost snapshots, block token and
#: cost rollups, the per-model and per-project children, and the Claude
#: calibration transitions. Operator Q7 / residual 5 checks them by ingest,
#: rebuild and rederive equivalence over the same admitted journal (never by
#: equality with a uniform-current trace), which is exactly this comparison.
_FAMILIES = {
    "weekly_usage_snapshots": None,
    "five_hour_reset_events": None,
    "week_reset_events": None,
    "weekly_credit_floors": None,
    "five_hour_blocks": None,
    "five_hour_block_models": None,
    "five_hour_block_projects": None,
    "weekly_cost_snapshots": None,
    "meter_rate_change_events": None,
    "percent_milestones": None,
    "five_hour_milestones": None,
}

#: #956 (spec 4.5.5): the only five-hour block column the comparison leaves
#: out besides `id`, on an open and a closed block alike. It is row-write
#: bookkeeping (the clock of the row's last write, an upsert, a projection
#: refresh or a close sweep), never rendered as accounting. Every other
#: column is compared: the end columns and totals come from one capture-order
#: projection on every path (`_cctally_five_hour.project_open_block_end`), so
#: `created_at_utc`, `last_observed_at_utc`, `final_five_hour_percent`,
#: `seven_day_pct_at_block_end`, the totals and `pricing_provenance_json` are
#: compared too. Before #956 the open block also left out `created_at_utc`,
#: and the out-of-order trace needed a settling reading to agree.
_BLOCK_UNCOMPARED = frozenset({"last_updated_at_utc"})

#: Rowid foreign keys, per family, and how each resolves to a logical identity:
#: a journaled table by its `journal_id`, the open block by its natural key.
_FK_COLUMNS = {
    "percent_milestones": {
        "usage_snapshot_id": ("weekly_usage_snapshots", "journal_id"),
        "cost_snapshot_id": ("weekly_cost_snapshots", "journal_id"),
        "reset_event_id": ("week_reset_events", "journal_id"),
    },
    "five_hour_milestones": {
        "usage_snapshot_id": ("weekly_usage_snapshots", "journal_id"),
        "reset_event_id": ("five_hour_reset_events", "journal_id"),
        "block_id": ("five_hour_blocks", "account_key || ':' || five_hour_window_key"),
    },
    "five_hour_block_models": {
        "block_id": ("five_hour_blocks", "account_key || ':' || five_hour_window_key"),
    },
    "five_hour_block_projects": {
        "block_id": ("five_hour_blocks", "account_key || ':' || five_hour_window_key"),
    },
}


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
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
    cctally.open_db().close()
    return cctally


def _identity(name):
    import _cctally_core as core
    import _lib_accounts
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": name, "emailAddress": f"{name}@example.test"}}))
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _lib_accounts.account_key("claude", name)


#: The one model every retained entry is priced as, and its token shape.
_ENTRY_MODEL = "claude-3-5-sonnet-20241022"
_ENTRY_INPUT, _ENTRY_OUTPUT = 100, 10


def _retain_session_input(app, name, account_key, offsets=(-10,)):
    """`db rederive` re-derives from retained session input; give each
    observed account one priced entry per offset (seconds from NOW)."""
    cache = app.open_cache_db()
    try:
        source = f"/retained/{name}/session.jsonl"
        cache.execute(
            "INSERT INTO session_files (path, size_bytes, mtime_ns, last_byte_offset,"
            " last_ingested_at, session_id, project_path) VALUES (?,?,?,?,?,?,?)",
            (source, 100, 1, 100, _iso(NOW), name, f"/retained/{name}"))
        for line, offset in enumerate(offsets):
            cache.execute(
                "INSERT INTO session_entries (source_path, line_offset,"
                " timestamp_utc, model, input_tokens, output_tokens,"
                " cache_create_tokens, cache_read_tokens, cache_create_1h_tokens,"
                " account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source, line, _iso(NOW + offset), _ENTRY_MODEL, _ENTRY_INPUT,
                 _ENTRY_OUTPUT, 0, 0, 0, account_key))
        cache.commit()
    finally:
        cache.close()


def _spool_and_reduce(app, name, session, weekly, five, at, five_reset):
    import _cctally_statusline as sl
    if name is None:
        _unattributed()
    else:
        _identity(name)
    candidate = sl._candidate_from_input(app._lib_statusline.parse_statusline_stdin(
        json.dumps({"session_id": session, "rate_limits": {
            "seven_day": {"used_percentage": weekly, "resets_at": _iso(NOW + 3 * 86400)},
            "five_hour": {"used_percentage": five, "resets_at": _iso(five_reset)},
        }}).encode()), received_at=NOW + at)
    sl._write_candidate(candidate)
    return sl._statusline_reduce_and_publish(nudge_sink=lambda: None)


def _record(app, name, weekly, five, five_reset):
    import _cctally_statusline as sl
    _identity(name)
    args = argparse.Namespace(
        percent=weekly, resets_at=str(NOW + 3 * 86400), five_hour_percent=five,
        five_hour_resets_at=str(five_reset), source="statusline")
    assert app.record_usage_bound(args, binding=sl._capture_binding(),
                                  nudge_dashboard=False) == 0


def _record_calibration(app, account_key, *, at):
    """A real Claude metering-rate transition for ``account_key``, through
    the production ingest entry `cmd_quota` uses (#661 S2 §6.5)."""
    import _cctally_journal as jr
    import _lib_meter_rate_change as mrc
    transition = mrc.RateChangeTransition(
        provider="claude", account_key=account_key,
        effective_from=_iso(NOW - 86400).replace("Z", "+00:00"),
        previous_units_per_point=2_442_620.0,
        new_units_per_point=1_685_000.0, severity="alarm",
        detected_at=_iso(NOW + at).replace("Z", "+00:00"))
    result = jr.run_stats_ingest(
        mode="authoritative",
        meter_rate_change={"transition": transition, "notify": False,
                           "created_at": _iso(NOW + at),
                           "notification_owed": False})
    assert result.conflicts_dropped == 0
    return transition


def _logical_state(app):
    """Every compared family as a multiset of logical rows (`id` dropped)."""
    conn = app.open_db()
    try:
        state = {}
        for table, keep in _FAMILIES.items():
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            fks = _FK_COLUMNS.get(table, {})
            rows = []
            for row in conn.execute(f"SELECT * FROM {table}"):
                logical = {
                    column: value for column, value in zip(columns, row)
                    if column != "id" and (keep is None or column in keep)
                }
                if table == "five_hour_blocks":
                    for column in _BLOCK_UNCOMPARED:
                        logical.pop(column, None)
                for column, (ref_table, identity) in fks.items():
                    logical[column] = _logical_fk(
                        conn, ref_table, identity, logical[column])
                rows.append(json.dumps(logical, sort_keys=True, default=str))
            state[table] = sorted(rows)
        return state
    finally:
        conn.close()


def _logical_fk(conn, ref_table, identity, rowid):
    """A rowid FK as the logical identity of the row it names: `"0"` for the
    no-row sentinel, `DANGLING` for a rowid whose row is gone (which a rebuild
    could never reproduce)."""
    if rowid in (0, None):
        return "0"
    found = conn.execute(
        f"SELECT {identity} FROM {ref_table} WHERE id = ?", (rowid,)).fetchone()
    return "DANGLING" if found is None else found[0]


def _state_diff(expected, actual):
    """Per table, the logical rows only one side has (for the failure text)."""
    diff = {}
    for table in sorted(set(expected) | set(actual)):
        left, right = expected.get(table, []), actual.get(table, [])
        only_left = [row for row in left if row not in right]
        only_right = [row for row in right if row not in left]
        if only_left or only_right:
            diff[table] = {"live": only_left, "replayed": only_right}
    return diff


def _assert_same(live, replayed):
    diff = _state_diff(live, replayed)
    if diff:
        pytest.fail("replay changed logical state:\n" + json.dumps(diff, indent=1),
                    pytrace=False)


def _anchors(account_keys):
    import _cctally_statusline as sl
    return {key: sl._read_transition(key, now_epoch=NOW + 10).five_hour_anchor
            for key in account_keys}


def _projections(account_keys):
    import _cctally_statusline as sl
    result = {}
    for key in account_keys:
        projection = sl._read_db_projection_once(account_key=key)
        result[key] = (projection.five_hour, projection.seven_day)
    return result


def _record_historical_policy(monkeypatch):
    """Record as the release before spec 4.7 revision 6 did: the raw
    observations carry no fold-policy revision, so ingest, rebuild and
    rederive all apply the historical fold policy. (A five-hour credit's
    stale-replica cleanup is not part of the fold policy: since fix round
    K15 it retires an unheld row in place under every policy.)"""
    import _lib_journal
    real = _lib_journal.make_obs

    def make_obs(**kwargs):
        payload = dict(kwargs.get("payload") or {})
        payload.pop("fold_policy", None)
        return real(**{**kwargs, "payload": payload})

    monkeypatch.setattr(_lib_journal, "make_obs", make_obs)


def _retired_snapshot_ids():
    """Every snapshot a journaled five-hour credit's suppression names (spec
    4.7 revision 6, fix round K15: the applier retires an unheld one in
    place and deletes a held one)."""
    import _cctally_core as core
    import _lib_journal
    retired = set()
    for path in sorted(core.JOURNAL_DIR.glob("*.jsonl")):
        with path.open("rb") as handle:
            for raw in handle:
                record = _lib_journal.decode_line(raw)
                payload = (record or {}).get("payload") or {}
                if (record or {}).get("t") == "evt" and payload.get(
                        "kind") == "five_hour_credit":
                    retired.update(payload.get("suppression") or [])
    return retired


@pytest.mark.parametrize("policy", ["historical", "first_post_floor"])
def test_m19_live_rebuild_and_rederive_yield_the_same_account_local_outcomes(
        app, monkeypatch, policy):
    """Case M19: a two-account trace with account-local anchors, five-hour
    credits and statusline publications in both accounts gives the same
    logical observations and attribution, reset events, credits, block
    identities, projections (including the #832 logical generation) and
    account-local anchors after live ingest, a real rebuild and a real
    rederive — for history recorded under the historical fold policy and
    under spec 4.7 revision 6's policy alike."""
    if policy == "historical":
        _record_historical_policy(monkeypatch)
    keys = {name: _identity(name) for name in ("A", "B")}
    boundary = ((NOW + 3600) // 600) * 600
    resets = {"A": boundary - 1, "B": boundary + 599}
    for name in ("A", "B"):
        _retain_session_input(app, name, keys[name])
    # Statusline publications with jitter that only the account-local anchor
    # resolves (A's raw reset straddles a 600 s bucket by one second).
    _spool_and_reduce(app, "A", "a-low", 20, 20, 0, resets["A"])
    _spool_and_reduce(app, "A", "a-high", 30, 70, 0, boundary)
    _spool_and_reduce(app, "B", "b-1", 40, 60, 0, resets["B"])
    # Bound recordings with a same-source five-hour descent and confirmation
    # in each account: an account-local five-hour credit. A's descent also
    # crosses weekly 31, so a weekly milestone names the arming snapshot,
    # which its credit's stale-replica band reaches (#943: the band starts
    # at the arming capture, so the t=0 readings stay out of it).
    for at, (a_five, b_five) in ((2, (20, 15)), (4, (20, 15))):
        _tick(monkeypatch, at)
        _record(app, "A", 31, a_five, resets["A"])
        _record(app, "B", 40, b_five, resets["B"])
    # A real calibration transition (Codex fix-round-2 Q3, P3-10): listing an
    # empty family compares nothing.
    _tick(monkeypatch, 6)
    _record_calibration(app, keys["A"], at=6)
    live = _logical_state(app)
    projections = _projections(keys.values())
    anchors = _anchors(keys.values())
    conn = app.open_db()
    try:
        events = {row[0] for row in conn.execute(
            "SELECT account_key FROM five_hour_reset_events")}
        accounts = {row[0] for row in conn.execute(
            "SELECT DISTINCT account_key FROM weekly_usage_snapshots")}
    finally:
        conn.close()
    assert events == set(keys.values()), "each account mints its own credit"
    assert accounts == set(keys.values())
    # Non-vacuity for the milestone families: the trace crosses weekly and
    # five-hour thresholds in both accounts, and at least one milestone names
    # a snapshot a credit's stale-replica cleanup reached (A's arming
    # snapshot, named by its weekly 31 milestone). Under either fold
    # policy that unheld snapshot is retired in place, its weekly evidence
    # kept, so the milestone keeps naming it (fix round K15; before it the
    # historical policy deleted the row and the milestone named 0).
    for table in ("percent_milestones", "five_hour_milestones"):
        owners = {json.loads(row)["account_key"] for row in live[table]}
        assert owners == set(keys.values()), (table, owners)
    retired = _retired_snapshot_ids()
    assert any(json.loads(row)["usage_snapshot_id"] in retired
               for table in ("percent_milestones", "five_hour_milestones")
               for row in live[table]), (
        "no milestone names a snapshot a credit retired in place")
    # Non-vacuity for the cost-bearing families (review P3-10): priced blocks
    # with per-model and per-project children in both accounts, and a weekly
    # cost snapshot.
    priced = {json.loads(row)["account_key"] for row in live["five_hour_blocks"]
              if json.loads(row)["total_cost_usd"] > 0}
    assert priced == set(keys.values()), live["five_hour_blocks"]
    for table in ("five_hour_block_models", "five_hour_block_projects"):
        owners = {json.loads(row)["account_key"] for row in live[table]}
        assert owners == set(keys.values()), (table, live[table])
    assert live["weekly_cost_snapshots"], "no weekly cost snapshot"
    [calibration] = [json.loads(row)
                     for row in live["meter_rate_change_events"]]
    assert calibration["account_key"] == keys["A"], calibration
    assert calibration["new_units_per_point"] == 1_685_000.0, calibration

    args = app.build_parser().parse_args(["db", "rebuild", "--db", "stats", "--json"])
    assert args.func(args) == 0
    _assert_same(live, _logical_state(app))
    assert _projections(keys.values()) == projections
    assert _anchors(keys.values()) == anchors

    args = app.build_parser().parse_args(
        ["db", "rederive", "--family", "claude-usage", "--yes", "--json"])
    assert args.func(args) == 0
    _assert_same(live, _logical_state(app))
    assert _projections(keys.values()) == projections
    assert _anchors(keys.values()) == anchors


def _drop_calibration(_conn, _evt):
    return None


def _change_calibration(real):
    def insert(conn, evt):
        changed = dict(evt)
        changed["payload"] = dict(evt["payload"])
        changed["payload"]["new_units_per_point"] = 1_700_000.0
        return real(conn, changed)
    return insert


@pytest.mark.parametrize("mutation", ["dropped", "changed"])
def test_m19_rejects_a_dropped_or_changed_calibration_transition(
        app, monkeypatch, mutation):
    """The calibration family's comparison is not vacuous (Codex
    fix-round-2 Q3, P3-10): a replay that drops or changes the retained
    transition fails the M19 comparison, while the unmutated replay passes."""
    import _cctally_journal as jr
    key = _identity("A")
    _retain_session_input(app, "A", key)
    _tick(monkeypatch, 2)
    _record(app, "A", 30, 20, ((NOW + 3600) // 600) * 600)
    _record_calibration(app, key, at=4)
    live = _logical_state(app)
    assert live["meter_rate_change_events"], "non-vacuity: a transition"
    argv = ["db", "rebuild", "--db", "stats", "--json"]
    args = app.build_parser().parse_args(argv)
    assert args.func(args) == 0
    _assert_same(live, _logical_state(app))  # the positive control

    real = jr._insert_meter_rate_change
    with monkeypatch.context() as patch:
        patch.setattr(jr, "_insert_meter_rate_change",
                      _drop_calibration if mutation == "dropped"
                      else _change_calibration(real))
        args = app.build_parser().parse_args(argv)
        assert args.func(args) == 0
    with pytest.raises(pytest.fail.Exception, match="meter_rate_change"):
        _assert_same(live, _logical_state(app))


def _shift_one_block_last_observation(app, seconds=60):
    """Move ONE five-hour block's `last_observed_at_utc` by ``seconds``,
    every other column untouched: a replay that differs from live only in
    when the block was last observed. A priced block is preferred. Returns
    the block's logical identity."""
    conn = app.open_db()
    try:
        row = conn.execute(
            "SELECT id, account_key, five_hour_window_key, last_observed_at_utc"
            " FROM five_hour_blocks"
            " ORDER BY (total_cost_usd > 0) DESC, id LIMIT 1").fetchone()
        assert row is not None, "non-vacuity: a five-hour block"
        block_id, account_key, window_key, observed = row
        moved = dt.datetime.fromisoformat(observed.replace("Z", "+00:00")) + (
            dt.timedelta(seconds=seconds))
        spelled = moved.isoformat()
        if observed.endswith("Z"):
            spelled = spelled.replace("+00:00", "Z")
        conn.execute("UPDATE five_hour_blocks SET last_observed_at_utc = ?"
                     " WHERE id = ?", (spelled, block_id))
        conn.commit()
        return account_key, window_key
    finally:
        conn.close()


def test_m19_rejects_a_rebuild_that_moves_a_block_observation_instant(
        app, monkeypatch):
    """#825/#832 fix round O3 (825-832-CD4-V01): a five-hour block's
    observation instants are accounting content — `five-hour-blocks`
    renders `lastObservedAtUtc` — so the logical replay comparison holds
    them. A rebuild whose only change is one nonempty block's
    `last_observed_at_utc`, 60 s later, every other column kept, fails M19;
    the unmutated rebuild passes."""
    key = _identity("A")
    _retain_session_input(app, "A", key)
    for at, five in ((0, 30), (2, 40)):
        _tick(monkeypatch, at)
        _record(app, "A", 30, five, ((NOW + 3600) // 600) * 600)
    live = _logical_state(app)
    argv = ["db", "rebuild", "--db", "stats", "--json"]
    args = app.build_parser().parse_args(argv)
    assert args.func(args) == 0
    _assert_same(live, _logical_state(app))  # the positive control

    args = app.build_parser().parse_args(argv)
    assert args.func(args) == 0
    _shift_one_block_last_observation(app)
    with pytest.raises(pytest.fail.Exception, match="five_hour_blocks"):
        _assert_same(live, _logical_state(app))


# --------------------------------------------------------------------------
# #825/#832 fix round J4 (825-832-CD4-V01, #954): an OPEN block's first
# observation and computation-time pricing provenance are accounting content.
# A rebuild must re-materialize both exactly as live ingest left them, and a
# later close must freeze the live values into the durable close.
# --------------------------------------------------------------------------

def _mutate_one_open_block(app, column):
    """Change ONE open five-hour block's ``column``, every other column kept,
    the way #954 changed it: `first_observed_at_utc` 60 s later,
    `pricing_provenance_json` erased. A nonempty (priced) block is preferred.
    Returns the block's logical identity."""
    conn = app.open_db()
    try:
        row = conn.execute(
            "SELECT id, account_key, five_hour_window_key, first_observed_at_utc,"
            " pricing_provenance_json FROM five_hour_blocks WHERE is_closed = 0"
            " ORDER BY (total_cost_usd > 0) DESC, id LIMIT 1").fetchone()
        assert row is not None, "non-vacuity: an open five-hour block"
        block_id, account_key, window_key, observed, provenance = row
        if column == "first_observed_at_utc":
            moved = dt.datetime.fromisoformat(observed.replace("Z", "+00:00")) + (
                dt.timedelta(seconds=60))
            value = moved.isoformat()
            if observed.endswith("Z"):
                value = value.replace("+00:00", "Z")
        else:
            assert provenance is not None, (
                "non-vacuity: the open block carries pricing provenance")
            value = None
        conn.execute(f"UPDATE five_hour_blocks SET {column} = ? WHERE id = ?",
                     (value, block_id))
        conn.commit()
        return account_key, window_key
    finally:
        conn.close()


@pytest.mark.parametrize(
    "column", ["first_observed_at_utc", "pricing_provenance_json"])
def test_m19_rejects_a_rebuild_that_changes_an_open_block_accounting_column(
        app, monkeypatch, column):
    """A rebuild whose only change is one OPEN block's first observation
    (60 s later) or its pricing provenance (erased), every other column kept,
    fails M19; the unmutated rebuild passes."""
    key = _identity("A")
    _retain_session_input(app, "A", key)
    for at, five in ((0, 30), (2, 40)):
        _tick(monkeypatch, at)
        _record(app, "A", 30, five, ((NOW + 3600) // 600) * 600)
    live = _logical_state(app)
    opened = [json.loads(row) for row in live["five_hour_blocks"]
              if json.loads(row)["is_closed"] == 0]
    assert opened and all(row["pricing_provenance_json"] for row in opened), (
        "non-vacuity: a priced open block", opened)
    argv = ["db", "rebuild", "--db", "stats", "--json"]
    args = app.build_parser().parse_args(argv)
    assert args.func(args) == 0
    _assert_same(live, _logical_state(app))  # the positive control

    args = app.build_parser().parse_args(argv)
    assert args.func(args) == 0
    _mutate_one_open_block(app, column)
    with pytest.raises(pytest.fail.Exception, match="five_hour_blocks"):
        _assert_same(live, _logical_state(app))


#: The credit trace's first five-hour window and its successor (both inside
#: the recorder's [now-10m, now+6h] band throughout the trace).
_CLOSE_RESET = NOW + 600
_SUCCESSOR_RESET = _CLOSE_RESET + 5 * 3600


def _block_close_trace(ns, monkeypatch, tmp_path, *, rebuild):
    """One account's five-hour window observed at 60% (t=0), then a
    same-source descent to 15% (t=2, armed) and its confirmation (t=4): an
    in-place five-hour credit. Before #943 its stale-replica removal deleted
    the window's FIRST snapshot (the band started at floor10 of the arming
    tick); since #943 the band starts at the arming capture, so that genuine
    earlier reading survives.
    ``rebuild`` runs a real `db rebuild` there. A reading of the successor
    window (t=6) then closes the block. Returns the open block before the
    successor, the surviving snapshots' first capture, the closed row and
    the durable close payload."""
    import _cctally_journal as jr
    import _lib_journal as J
    redirect_paths(ns, monkeypatch, tmp_path)
    app = sys.modules["cctally"]
    app.open_db().close()
    key = _identity("B")
    _retain_session_input(app, "B", key)
    for at, five in ((0, 60), (2, 15), (4, 15)):
        _tick(monkeypatch, at)
        _record(app, "B", 40, five, _CLOSE_RESET)
    if rebuild:
        args = app.build_parser().parse_args(
            ["db", "rebuild", "--db", "stats", "--json"])
        assert args.func(args) == 0
    columns = None
    conn = app.open_db()
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(five_hour_blocks)") if row[1] != "id"]
        [opened] = conn.execute(
            f"SELECT {', '.join(columns)} FROM five_hour_blocks"
            " WHERE is_closed = 0").fetchall()
        opened = dict(zip(columns, opened))
        surviving = conn.execute(
            "SELECT MIN(captured_at_utc) FROM weekly_usage_snapshots"
            " WHERE account_key = ? AND five_hour_window_key = ?",
            (key, opened["five_hour_window_key"])).fetchone()[0]
        credits = conn.execute(
            "SELECT COUNT(*) FROM five_hour_reset_events WHERE account_key = ?",
            (key,)).fetchone()[0]
    finally:
        conn.close()
    assert credits == 1, "non-vacuity: an in-place five-hour credit"
    _tick(monkeypatch, 6)
    _record(app, "B", 40, 20, _SUCCESSOR_RESET)
    conn = app.open_db()
    try:
        closed = conn.execute(
            f"SELECT {', '.join(columns)} FROM five_hour_blocks"
            " WHERE is_closed = 1 AND five_hour_window_key = ?",
            (opened["five_hour_window_key"],)).fetchall()
    finally:
        conn.close()
    assert len(closed) == 1, closed
    closes = []
    for segment in jr.list_segments():
        for line in (jr._cctally_core.JOURNAL_DIR / segment).read_bytes(
                ).splitlines():
            record = J.decode_line(line)
            payload = (record or {}).get("payload") or {}
            if (payload.get("kind") == "five_hour_block_close"
                    and payload.get("five_hour_window_key")
                    == opened["five_hour_window_key"]):
                closes.append(payload)
    assert len(closes) == 1, closes
    return opened, surviving, dict(zip(columns, closed[0])), closes[0]


def test_m19_a_rebuilt_open_block_closes_with_its_live_observation_and_pricing(
        monkeypatch, tmp_path):
    """#954: a real rebuild re-materializes the open block, and a successor
    window then closes it. The re-materialized block, the closed row and the
    durable `five_hour_block_close` record carry the live first observation
    — which, before #943, the credit's stale-replica removal deleted —
    and the live computation-time pricing provenance."""
    ns = load_script()
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    monkeypatch.setattr(sys.modules["cctally"], "_nudge_dashboard_repaint",
                        lambda: None)
    live_open, surviving, live_closed, live_close = _block_close_trace(
        ns, monkeypatch, tmp_path / "live", rebuild=False)
    # Non-vacuity: the live first observation is the t=0 reading, and live
    # stamped pricing. #943 (accuracy: a credit deleting a genuine reading
    # captured before its descent armed): the t=0 snapshot precedes the
    # arming capture, so the stale-replica band no longer reaches it and it
    # survives; before #943 it was deleted (`surviving` was the t=2 row).
    assert live_open["first_observed_at_utc"].startswith(
        _iso(NOW).replace("Z", "")), live_open
    assert surviving.startswith(_iso(NOW).replace("Z", "")), (
        surviving, live_open)
    assert live_open["pricing_provenance_json"], live_open

    rebuilt_open, _surviving, closed, close = _block_close_trace(
        ns, monkeypatch, tmp_path / "rebuilt", rebuild=True)
    for column in ("first_observed_at_utc", "pricing_provenance_json"):
        assert rebuilt_open[column] == live_open[column], (column, rebuilt_open)
        assert closed[column] == live_closed[column], (column, closed)
        assert close.get(column) == live_close.get(column), (column, close)
    assert closed == live_closed
    assert close == live_close


def _drive_replay(app, monkeypatch, replay):
    """One production replay of the live index: `db rebuild`, `db
    rederive`, the account epoch transition, or the corrupt-index heal's
    detached worker. A rederive whose re-derivation reaches the journaled
    decisions appends no correction and rebuilds nothing, so its leg proves
    that the re-derivation in journal order agrees on the first writer."""
    if replay == "epoch-transition":
        import _cctally_core as core
        import _cctally_journal as jr
        jr.run_epoch_transition(claude_json_path=str(core.CLAUDE_JSON_PATH))
        return
    if replay == "rebuild":
        argv = ["db", "rebuild", "--db", "stats", "--json"]
    elif replay == "rederive":
        argv = ["db", "rederive", "--family", "claude-usage", "--yes",
                "--json"]
    else:
        import _cctally_core as core
        import _cctally_db
        import _cctally_store as st
        import _cctally_update
        import types
        monkeypatch.setattr(_cctally_update, "_spawn_detached",
                            lambda command: True)
        monkeypatch.setattr(_cctally_update, "_spawn_detached_with_pid",
                            lambda command, **_kw: os.getpid())
        with open(core.DB_PATH, "r+b") as handle:
            handle.write(b"not a database " * 200)
        with pytest.raises(_cctally_db.StatsHealDeferred):
            core.open_db()
        assert st.cmd_stats_corruption_heal_internal(
            types.SimpleNamespace()) == 0
        return
    args = app.build_parser().parse_args(argv)
    assert args.func(args) == 0


# --------------------------------------------------------------------------
# #956 class 1 (spec 4.5, test 6.6): the open five-hour block's END columns,
# totals, children and pricing come from ONE capture-order projection on
# live ingest, derivation completion, rebuild, epoch transition, corruption
# heal and rederive. Each schedule below is replayed and compared on every
# compared family (five-hour blocks on every column but `id` and
# `last_updated_at_utc`), on the later closed row and on its durable
# `five_hour_block_close` payload; and live is checked against independent
# expected values (the end reading each schedule names, and token and cost
# totals summed from the fixture's own retained entries), because identical
# wrong selectors or zeroed totals would also agree.
# --------------------------------------------------------------------------

#: A window whose reset lies one hour after `_CLOSE_RESET`, so the two
#: canonical windows overlap for four hours ([`_OVERLAP_RESET` - 5h,
#: `_CLOSE_RESET`)), where the earlier-reset window owns an entry.
_OVERLAP_RESET = _CLOSE_RESET + 3600
_OVERLAP_SUCCESSOR_RESET = _OVERLAP_RESET + 5 * 3600

#: The replay legs a schedule is run through (spec 4.5.3): `rederive` runs
#: the live pipeline; `rebuilds` chains `db rebuild`, the account epoch
#: transition and the corrupt-index heal, comparing after each; `completion`
#: leaves the stage's last recording underived (its ingest never ran) and
#: lets the rebuild's derivation completion derive it.
_LEGS = ("rederive", "rebuilds", "completion")
_LEG_REPLAYS = {
    "rederive": ("rederive",),
    "rebuilds": ("rebuild", "epoch-transition", "corruption-heal"),
    "completion": ("rebuild",),
}

def _entry_cost():
    """One retained entry's USD cost, from the embedded price card (never
    from a block row)."""
    import _lib_pricing
    card = _lib_pricing.CLAUDE_MODEL_PRICING[_ENTRY_MODEL]
    return (_ENTRY_INPUT * card["input_cost_per_token"]
            + _ENTRY_OUTPUT * card["output_cost_per_token"])


def _epoch(stamp):
    return int(dt.datetime.fromisoformat(
        stamp.replace("Z", "+00:00")).timestamp())


class _Recorder:
    """The recorders a schedule drives, each through its real entry point:
    `bound` (`record_usage_bound`, the status-line source), `cli` (the manual
    `cctally record-usage` handler), `publication` (the status-line spool and
    reducer, which journals a planned window key) and `credit` (`cctally
    record-credit`).

    ``during`` runs before this recording's observation reaches the journal,
    so an earlier capture is appended (and derived) after a later one.
    ``owed`` (the completion leg only) skips this recording's own ingest, so
    the observation is in the journal but underived. ``batched`` always skips
    it: the next recording's ingest derives both in one cycle."""

    def __init__(self, app, monkeypatch, *, owe):
        self.app = app
        self.monkeypatch = monkeypatch
        self.owe = owe
        self.owed = []
        self.obs = {}

    def _run(self, label, at, call, *, during, skip_ingest):
        import types
        import _cctally_journal as jr
        real_append = jr.append_record
        real_ingest = jr.run_stats_ingest
        state = {"done": False, "skip": False}

        def append(record, *args, **kwargs):
            if not state["done"] and record.get("t") == "obs":
                state["done"] = True
                if during is not None:
                    during()
                result = real_append(record, *args, **kwargs)
                self.obs[label] = record
                state["skip"] = skip_ingest
                return result
            return real_append(record, *args, **kwargs)

        def ingest(*args, **kwargs):
            if state["skip"]:
                state["skip"] = False
                return types.SimpleNamespace(ran=False, error=None,
                                             events_emitted=0)
            return real_ingest(*args, **kwargs)

        _tick(self.monkeypatch, at)
        with self.monkeypatch.context() as patch:
            patch.setattr(jr, "append_record", append)
            patch.setattr(jr, "run_stats_ingest", ingest)
            assert call() == 0, label
        assert state["done"], f"non-vacuity: {label} appended an observation"

    def _skip(self, owed, batched):
        if owed and self.owe:
            self.owed.append(owed)
        return batched or (owed is not None and self.owe)

    def bound(self, label, at, weekly, five, reset, *, during=None, owed=None,
              batched=False, planned=None):
        import _cctally_statusline as sl
        args = argparse.Namespace(
            percent=weekly, resets_at=str(NOW + 3 * 86400),
            five_hour_percent=five, five_hour_resets_at=str(reset),
            source="statusline")
        if planned is not None:
            args.five_hour_window_key = planned
        self._run(label, at, lambda: self.app.record_usage_bound(
            args, binding=sl._capture_binding(), nudge_dashboard=False),
            during=during, skip_ingest=self._skip(owed, batched))

    def cli(self, label, at, weekly, five, reset, *, owed=None, batched=False):
        import _cctally_record as rec
        args = argparse.Namespace(
            percent=weekly, resets_at=str(NOW + 3 * 86400),
            five_hour_percent=five, five_hour_resets_at=str(reset))
        self._run(label, at, lambda: rec.cmd_record_usage(
            args, nudge_dashboard=False), during=None,
            skip_ingest=self._skip(owed, batched))

    def publication(self, name, at, weekly, five, reset):
        _tick(self.monkeypatch, at)
        _spool_and_reduce(self.app, name, "b-1", weekly, five, at, reset)

    def credit(self, at, to, credit_at, *, force=False):
        _tick(self.monkeypatch, at)
        argv = ["record-credit", "--to", str(to), "--at",
                _iso(NOW + credit_at), "--yes", "--json"]
        if force:
            argv.append("--force")
        args = self.app.build_parser().parse_args(argv)
        assert args.func(args) == 0, argv


def _unattributed():
    """No active Claude identity: readings bind to `unattributed`."""
    import _cctally_core as core
    import _lib_accounts
    if core.CLAUDE_JSON_PATH.exists():
        core.CLAUDE_JSON_PATH.unlink()
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _lib_accounts.UNATTRIBUTED


def _journal_records():
    import _cctally_journal as jr
    import _lib_journal as J
    out = []
    for segment in jr.list_segments():
        for line in (jr._cctally_core.JOURNAL_DIR / segment).read_bytes(
                ).splitlines():
            record = J.decode_line(line)
            if record is not None:
                out.append(record)
    return out


def _close_payloads():
    """Every durable `five_hour_block_close` payload, keyed by window, with
    `last_updated_at_utc` (row-write bookkeeping) left out."""
    closes = {}
    for record in _journal_records():
        payload = record.get("payload") or {}
        if payload.get("kind") == "five_hour_block_close":
            kept = {k: v for k, v in payload.items()
                    if k not in _BLOCK_UNCOMPARED}
            closes.setdefault(
                (payload["account_key"], payload["five_hour_window_key"]),
                []).append(json.dumps(kept, sort_keys=True, default=str))
    return closes


def _blocks(state):
    """The compared five-hour block rows of one logical state, in window
    order."""
    return sorted((json.loads(row) for row in state["five_hour_blocks"]),
                  key=lambda row: row["five_hour_window_key"])


def _expect_block(block, *, end_at, final, weekly_end, entries,
                  first_at=None, is_closed=None):
    """Assert one block's end reading and totals against independent values:
    the capture (seconds from NOW) and readings of the reading that must be
    its end, and how many retained entries it owns."""
    assert _epoch(block["last_observed_at_utc"]) == NOW + end_at, block
    assert block["final_five_hour_percent"] == final, block
    assert block["seven_day_pct_at_block_end"] == weekly_end, block
    assert block["total_input_tokens"] == entries * _ENTRY_INPUT, block
    assert block["total_output_tokens"] == entries * _ENTRY_OUTPUT, block
    assert block["total_cost_usd"] == pytest.approx(
        entries * _entry_cost(), abs=1e-12), block
    if first_at is not None:
        assert _epoch(block["first_observed_at_utc"]) == NOW + first_at, block
    if is_closed is not None:
        assert block["is_closed"] == is_closed, block


def _window_of(state, reset):
    [block] = [row for row in _blocks(state)
               if _epoch(row["five_hour_resets_at"]) == reset]
    return block


# -- schedules ---------------------------------------------------------------
# Each schedule: `offsets` of its retained entries, `stages` (each a callable
# over a `_Recorder`), an optional `close` stage (a successor reading after
# the replays), and `expect(live_states)` over the live state after each stage
# and after the close.

def _ooo_window_stages(r):
    # Recorder A captures 40/50 at t=0 and pauses; recorder B captures 40/35
    # at t=2, appends and ingests first, materializing the block; A then
    # appends its t=0 reading, accepted (its five-hour value is new). No
    # later reading settles the end.
    r.bound("A", 0, 40, 50, _CLOSE_RESET, owed="A", during=lambda: r.bound(
        "B", 2, 40, 35, _CLOSE_RESET))


def _ooo_window_expect(states):
    # The end is the latest CAPTURE's (B, t=2, 35%), never the journal-last
    # older reading's (A, t=0, 50%), and the totals run to it: the entry at
    # t=1 lies after A's capture. B is the block's first writer.
    opened, closed = states
    _expect_block(_window_of(opened, _CLOSE_RESET), end_at=2, final=35.0,
                  weekly_end=40.0, entries=2, first_at=2, is_closed=0)
    _expect_block(_window_of(closed, _CLOSE_RESET), end_at=2, final=35.0,
                  weekly_end=40.0, entries=2, first_at=2, is_closed=1)


def _ooo_across_stages(r):
    # A newer window is recorded first (B, t=1000); then A's earlier-captured
    # reading of the OLDER, overlapping window is appended and creates its
    # block late. Both blocks stay open. The entry at t=-10 lies in the
    # overlap and belongs to the earlier-reset window (#751a), so A's late
    # creation must take it from B's open block.
    r.bound("A", 0, 40, 30, _CLOSE_RESET, owed="A", during=lambda: r.bound(
        "B", 1000, 40, 50, _OVERLAP_RESET))


def _ooo_across_close(r):
    r.bound("C", 1200, 40, 20, _OVERLAP_SUCCESSOR_RESET)


def _ooo_across_expect(states):
    opened, closed = states
    _expect_block(_window_of(opened, _CLOSE_RESET), end_at=0, final=30.0,
                  weekly_end=40.0, entries=1, is_closed=0)
    _expect_block(_window_of(opened, _OVERLAP_RESET), end_at=1000, final=50.0,
                  weekly_end=40.0, entries=1, is_closed=0)
    _expect_block(_window_of(closed, _CLOSE_RESET), end_at=0, final=30.0,
                  weekly_end=40.0, entries=1, is_closed=1)
    _expect_block(_window_of(closed, _OVERLAP_RESET), end_at=1000,
                  final=50.0, weekly_end=40.0, entries=1, is_closed=1)


#: The equal-second tie's two five-hour readings: B (appended first) and A
#: (appended last). A is the larger, so the fold clamps neither, and at these
#: values A's observation id sorts BELOW B's (the non-vacuity assertion in
#: `_tie_expect` pins that), so identity order and journal order disagree.
_TIE_B, _TIE_A = 38.0, 44.0


def _tie_stages(r):
    # Two recorders capture the SAME second; B appends first, A last.
    r.bound("A", 2, 40, _TIE_A, _CLOSE_RESET, owed="A", during=lambda: r.bound(
        "B", 2, 40, _TIE_B, _CLOSE_RESET))


def _tie_expect(states, recorder):
    # The tie breaks on logical identity (`journal_id DESC`), never on
    # journal or rowid order: non-vacuity requires that the journal-last
    # reading (A) is NOT the one the identity order selects.
    winner = max(("A", "B"), key=lambda label: "sa:" + recorder.obs[label]["id"])
    assert winner == "B", (
        "non-vacuity: the journal-last reading must lose the identity tie",
        {k: "sa:" + v["id"] for k, v in recorder.obs.items()})
    opened, closed = states
    for state, is_closed in ((opened, 0), (closed, 1)):
        _expect_block(_window_of(state, _CLOSE_RESET), end_at=2,
                      final=_TIE_B, weekly_end=40.0, entries=1,
                      is_closed=is_closed)


def _credit_stages(r):
    # J6-E01 / #954. Within one window and source, weekly 40 steady: 70 is
    # captured and appended at t=120 (the window's first snapshot and first
    # writer); a delayed 20 captured at t=0 is appended after it (a descent:
    # it arms a five-hour credit at its own capture); a distinct delayed 25
    # captured at t=60 confirms. The credit's stale-replica band starts at
    # the arming capture (t=0), so it retires the first writer's 70 — the
    # latest-captured snapshot and the block's end until then.
    r.bound("A20", 0, 40, 20, _CLOSE_RESET, during=lambda: r.bound(
        "X70", 120, 40, 70, _CLOSE_RESET))
    # Not owed on the completion leg: the source-local confirmation state is
    # disposable (`docs/five-hour-gotchas.md`, the admitted cold-start gap),
    # so a confirmation derived after a rebuild has no armed descent to
    # confirm, and that leg is a plain rebuild here.
    r.bound("C25", 60, 40, 25, _CLOSE_RESET)


def _credit_close(r):
    r.bound("S", 300, 40, 20, _SUCCESSOR_RESET)


def _credit_expect(states, recorder):
    [credit] = [record["payload"] for record in _journal_records()
                if (record.get("payload") or {}).get("kind")
                == "five_hour_credit"]
    assert "sa:" + recorder.obs["X70"]["id"] in (credit.get("suppression")
                                                 or []), (
        "non-vacuity: the credit's suppression names the window's first "
        "snapshot (#954)", credit)
    # The suppression moved the end backward to the latest SURVIVING reading
    # (C25, t=60), and the first writer (X70, t=120) keeps the start.
    opened, closed = states
    for state, is_closed in ((opened, 0), (closed, 1)):
        _expect_block(_window_of(state, _CLOSE_RESET), end_at=60, final=25.0,
                      weekly_end=40.0, entries=1, first_at=120,
                      is_closed=is_closed)


def _force_stages_credit(r):
    r.bound("T0", 0, 40, 30, _CLOSE_RESET)
    r.bound("T2", 2, 41, 34, _CLOSE_RESET)
    # The synthetic of `record-credit --at t=3` is the window's latest
    # capture: it carries the window's five-hour reading and the credited 5.
    r.credit(4, 5, 3)


def _force_stages_force(r):
    # `--force` removes that synthetic; its replacement is captured at t=1,
    # so the end moves back to the latest surviving reading (t=2).
    r.credit(5, 6, 1, force=True)


def _force_close(r):
    r.bound("S", 8, 6, 20, _SUCCESSOR_RESET)


def _force_expect(states):
    synthetic, forced, closed = states
    _expect_block(_window_of(synthetic, _CLOSE_RESET), end_at=3, final=34.0,
                  weekly_end=5.0, entries=1, is_closed=0)
    [t2] = [json.loads(row) for row in forced["weekly_usage_snapshots"]
            if _epoch(json.loads(row)["captured_at_utc"]) == NOW + 2]
    for state, is_closed in ((forced, 0), (closed, 1)):
        _expect_block(_window_of(state, _CLOSE_RESET), end_at=2, final=34.0,
                      weekly_end=t2["weekly_percent"], entries=1,
                      is_closed=is_closed)


def _held_rollover_stages(r):
    # CD4-V01. The weekly basis is 63; a new window is then read at a raw
    # weekly 60, which the fold holds at 63. An unplanned reading (the manual
    # CLI) and a planned one (an explicit account-local key) of the new
    # window are ingested in ONE cycle; the unplanned one is appended first
    # and is the new block's first writer.
    r.bound("W0", 0, 63, 30, _CLOSE_RESET)
    r.cli("U", 2, 60, 30, _SUCCESSOR_RESET, batched=True)
    r.bound("P", 4, 60, 32, _SUCCESSOR_RESET, planned=_SUCCESSOR_RESET,
            owed="P")


def _held_rollover_expect(states):
    [state] = states
    _expect_block(_window_of(state, _CLOSE_RESET), end_at=0, final=30.0,
                  weekly_end=63.0, entries=1, is_closed=1)
    successor = _window_of(state, _SUCCESSOR_RESET)
    # The fold-effective weekly value (63), never the raw reading (60).
    assert successor["seven_day_pct_at_block_start"] == 63.0, successor
    _expect_block(successor, end_at=4, final=32.0, weekly_end=63.0,
                  entries=0, first_at=2, is_closed=0)


def _cli_heal_stages(r):
    # J6-E02 (manual CLI), recorded under the historical fold policy (every
    # observation before #825/#832; the current policy writes a new window's
    # first reading as a snapshot): the successor-window reading repeats the
    # last snapshot's weekly and five-hour values, so it is a dedup skip and
    # the rollover heal opens its block without a snapshot.
    # Not owed on the completion leg: a dedup skip writes no snapshot
    # decision, so a completion has no reading of its own to derive here.
    r.cli("W0", 0, 40, 30, _CLOSE_RESET)
    r.cli("W1", 2, 40, 30, _SUCCESSOR_RESET)


def _publication_heal_stages(r):
    # J6-E02 (unattributed, planned): the same rollover through the
    # status-line reducer with no active Claude identity.
    _unattributed()
    r.publication(None, 0, 40, 30, _CLOSE_RESET)
    r.publication(None, 2, 40, 30, _SUCCESSOR_RESET)


def _heal_expect(states):
    [state] = states
    _expect_block(_window_of(state, _CLOSE_RESET), end_at=0, final=30.0,
                  weekly_end=40.0, entries=1, is_closed=1)
    healed = _window_of(state, _SUCCESSOR_RESET)
    # Non-vacuity: the heal opened the block with no snapshot of its window,
    # so its end is the opening reading (the selection is empty).
    assert not [row for row in state["weekly_usage_snapshots"]
                if json.loads(row)["five_hour_window_key"]
                == healed["five_hour_window_key"]], state
    _expect_block(healed, end_at=2, final=30.0, weekly_end=40.0, entries=0,
                  first_at=2, is_closed=0)


class _Schedule(typing.NamedTuple):
    offsets: tuple                 # retained entries, seconds from NOW
    stages: tuple                  # each a callable over a `_Recorder`
    close: object                  # a successor stage after the replays
    expect: object                 # expect(live_states, recorder)
    unattributed: bool = False     # recorded with no active identity
    historical: bool = False       # observations carry no fold policy


_SCHEDULES = {
    "ooo-window": _Schedule(
        (-10, 1), (_ooo_window_stages,), _credit_close,
        lambda s, r: _ooo_window_expect(s)),
    "ooo-across-overlap": _Schedule(
        (-10, 800), (_ooo_across_stages,), _ooo_across_close,
        lambda s, r: _ooo_across_expect(s)),
    "equal-second-tie": _Schedule(
        (-10,), (_tie_stages,), _credit_close, _tie_expect),
    "credit-suppressed-end": _Schedule(
        (-10,), (_credit_stages,), _credit_close, _credit_expect),
    "record-credit-force": _Schedule(
        (-10,), (_force_stages_credit, _force_stages_force), _force_close,
        lambda s, r: _force_expect(s)),
    "held-rollover-mixed-cycle": _Schedule(
        (-10,), (_held_rollover_stages,), None,
        lambda s, r: _held_rollover_expect(s)),
    "manual-cli-heal": _Schedule(
        (-10,), (_cli_heal_stages,), None, lambda s, r: _heal_expect(s),
        historical=True),
    "unattributed-planned-heal": _Schedule(
        (-10,), (_publication_heal_stages,), None,
        lambda s, r: _heal_expect(s),
        unattributed=True, historical=True),
}


def _run_schedule(ns, monkeypatch, tmp_path, name, *, leg):
    """One run of a schedule: live (``leg`` None) or through a replay leg.
    Returns the logical state after each stage (after each of its replays on
    a replay leg), the state after the close, the close payloads and the
    recorder."""
    schedule = _SCHEDULES[name]
    redirect_paths(ns, monkeypatch, tmp_path)
    app = sys.modules["cctally"]
    app.open_db().close()
    if schedule.historical:
        _record_historical_policy(monkeypatch)
    if schedule.unattributed:
        _retain_session_input(app, "unattributed", _unattributed(),
                              schedule.offsets)
    else:
        _retain_session_input(app, "B", _identity("B"), schedule.offsets)
    recorder = _Recorder(app, monkeypatch, owe=(leg == "completion"))
    states = []
    for stage in schedule.stages:
        recorder.owed.clear()
        stage(recorder)
        if leg is None:
            states.append({"live": _logical_state(app)})
            continue
        replayed = {}
        for replay in _LEG_REPLAYS[leg]:
            _drive_replay(app, monkeypatch, replay)
            replayed[replay] = _logical_state(app)
        if recorder.owed:
            # Non-vacuity: the completion derived the reading whose ingest
            # never ran — its snapshot decision is now journaled.
            accepted = {record["id"] for record in _journal_records()
                        if record.get("t") == "evt"}
            for label in recorder.owed:
                assert "sa:" + recorder.obs[label]["id"] in accepted, (
                    "the completion left an owed reading underived", label)
        states.append(replayed)
    if schedule.close is not None:
        schedule.close(recorder)
        states.append({"close": _logical_state(app)})
    return states, _close_payloads(), recorder


def _schedule_params():
    return [pytest.param(name, leg, id=f"{name}-{leg}")
            for name in _SCHEDULES for leg in _LEGS]


@pytest.mark.parametrize("name,leg", _schedule_params())
def test_m19_the_open_block_end_is_one_capture_order_projection_on_every_path(
        monkeypatch, tmp_path, name, leg):
    """#956 (spec 4.5, 4.6): detects live and replay disagreeing on the
    open block's end columns, totals, children, pricing or open population,
    and an end that is not the window's latest surviving capture. Live ingest
    left the end of the journal-last reading (an earlier capture appended
    later kept `last_observed < first_observed`, its totals cut at the older
    capture), never refreshed an open block whose ownership a late-created
    predecessor changed, ignored a `record-credit` synthetic, and broke an
    equal-second tie by journal order, while the rebuild projected the
    latest capture. And an apply-only replay re-materialized only each
    account's latest-captured window, so a second open window and a block a
    snapshotless rollover heal opened were lost; every creation now retains
    an opening fact (`five_hour_block_open`) whose fold materializes the
    block. Each schedule is compared after each stage's replays, through its
    later close and the durable close payload, against live and against its
    independent expected values."""
    ns = load_script()
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    monkeypatch.setattr(sys.modules["cctally"], "_nudge_dashboard_repaint",
                        lambda: None)
    schedule = _SCHEDULES[name]
    live, live_closes, live_recorder = _run_schedule(
        ns, monkeypatch, tmp_path / "live", name, leg=None)
    schedule.expect([next(iter(stage.values())) for stage in live],
                    live_recorder)
    replayed, closes, _recorder = _run_schedule(
        ns, monkeypatch, tmp_path / "replayed", name, leg=leg)
    for live_stage, replayed_stage in zip(live, replayed):
        [expected] = live_stage.values()
        for replay, state in replayed_stage.items():
            diff = _state_diff(expected, state)
            if diff:
                pytest.fail(f"{name}: {replay} changed logical state:\n"
                            + json.dumps(diff, indent=1), pytrace=False)
    assert closes == live_closes


def _accept(J, event_id, captured, weekly, window=1791421200, account="acct"):
    return J.make_evt("snapshot_accept", event_id, captured, {
        "account_key": account, "captured_at_utc": captured,
        "five_hour_window_key": window, "five_hour_percent": 10.0,
        "five_hour_resets_at": "2026-10-08T05:00:00+00:00",
        "weekly_percent": weekly})


def test_journaled_first_writer_is_the_first_effective_decision_in_journal_order():
    """The pure first-writer rule (fix round J5): decisions are taken in
    journal order, never by capture time; a correction revising a decision
    keeps the decision's place and supplies its effective content; a
    tombstoned decision never wrote; and a decision without an explicit
    account is left to the survivors."""
    import _cctally_journal as jr
    import _lib_journal as J
    later = _accept(J, "sa:b", "2026-10-08T00:00:02Z", 40.0)
    earlier = _accept(J, "sa:a", "2026-10-08T00:00:00Z", 41.0)
    retired = _accept(J, "sa:r", "2026-10-08T00:00:01Z", 30.0, window=7)
    replaced = _accept(J, "sa:c", "2026-10-08T00:00:03Z", 50.0, window=8)
    survivor = _accept(J, "sa:d", "2026-10-08T00:00:00Z", 55.0, window=8)
    unstamped = _accept(J, "sa:u", "2026-10-08T00:00:00Z", 60.0, window=9)
    del unstamped["payload"]["account_key"]
    revised = dict(replaced["payload"], weekly_percent=51.0)
    records = [None, later, earlier, retired, replaced, survivor, unstamped,
               *J.make_correction_batch(
                   batch_id="b1", family="claude-usage",
                   at="2026-10-08T00:00:09Z", actions=[
                       {"action": "replace", "id": "sa:c", "rev": 1,
                        "at": "2026-10-08T00:00:09Z", "payload": revised},
                       {"action": "tombstone", "id": "sa:r", "rev": 1,
                        "at": "2026-10-08T00:00:09Z", "payload": None},
                   ])]
    by_id = J.resolve_effective_events(records).by_id
    first = jr.journaled_block_first_observations(records, by_id)
    assert first == {
        ("acct", 1791421200): {
            "captured_at_utc": "2026-10-08T00:00:02Z",
            "five_hour_resets_at": "2026-10-08T05:00:00+00:00",
            "weekly_percent": 40.0},
        ("acct", 8): {
            "captured_at_utc": "2026-10-08T00:00:03Z",
            "five_hour_resets_at": "2026-10-08T05:00:00+00:00",
            "weekly_percent": 51.0},
    }, first
