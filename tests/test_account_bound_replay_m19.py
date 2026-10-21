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
import sys
import time

import pytest

from conftest import load_script, redirect_paths

NOW = 1791417600

#: The compared families and, per family, the columns that make up its
#: logical outcome (None = every column but `id`, the physical row id).
#: #825/#832 fix round O3 (825-832-CD4-V01): every family, five-hour blocks
#: included, is compared on every column but `id`; the only other columns
#: left out are `_OPEN_BLOCK_UNCOMPARED`'s, on an OPEN block only.
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

#: #825/#832 fix round O3 (825-832-CD4-V01): the columns of an OPEN
#: five-hour block (`is_closed = 0`) the comparison leaves out, each with its
#: reason. A closed block is a journaled `five_hour_block_close` fact and is
#: compared on every column. The open block is the re-materialized projection
#: (`docs/journal-gotchas.md`, "The open 5h block is a re-materialized
#: PROJECTION"): live upserts it per observation, a rebuild re-derives it
#: from the journal.
#:
#: Only row-write bookkeeping is left out, observed to differ and documented
#: as a projection convention, never rendered as accounting (the five-hour
#: CLI and dashboard render `last_observed_at_utc`, not these):
#:
#: * `created_at_utc`: the capture clock of the write that inserted the row;
#:   the rebuild projection sets it to `first_observed_at_utc`.
#: * `last_updated_at_utc`: the capture clock of the row's last write (an
#:   upsert or a close sweep); the rebuild projection sets it to
#:   `last_observed_at_utc`. Observed: old-recorder-weekly live 22:15:00Z vs
#:   rebuild 22:00:00Z, `last_observed_at_utc` equal.
#:
#: #825/#832 fix round J4 (825-832-CD4-V01, #954): `first_observed_at_utc`
#: and `pricing_provenance_json` are accounting content — the block's weekly
#: start instant (`_block_weekly_start_instant`) and the computation-time
#: pricing the `five_hour_block_close` harvest copies into the durable close
#: — and are compared on the open block too.
_OPEN_BLOCK_UNCOMPARED = frozenset({
    "created_at_utc", "last_updated_at_utc",
})

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


def _retain_session_input(app, name, account_key):
    """`db rederive` re-derives from retained session input; give each
    observed account one priced entry."""
    cache = app.open_cache_db()
    try:
        source = f"/retained/{name}/session.jsonl"
        cache.execute(
            "INSERT INTO session_files (path, size_bytes, mtime_ns, last_byte_offset,"
            " last_ingested_at, session_id, project_path) VALUES (?,?,?,?,?,?,?)",
            (source, 100, 1, 100, _iso(NOW), name, f"/retained/{name}"))
        cache.execute(
            "INSERT INTO session_entries (source_path, line_offset, timestamp_utc,"
            " model, input_tokens, output_tokens, cache_create_tokens,"
            " cache_read_tokens, cache_create_1h_tokens, account_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source, 0, _iso(NOW - 10), "claude-3-5-sonnet-20241022", 100, 10,
             0, 0, 0, account_key))
        cache.commit()
    finally:
        cache.close()


def _spool_and_reduce(app, name, session, weekly, five, at, five_reset):
    import _cctally_statusline as sl
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
                if table == "five_hour_blocks" and logical.get("is_closed") == 0:
                    for column in _OPEN_BLOCK_UNCOMPARED:
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
    # in each account: an account-local five-hour credit.
    for at, (a_five, b_five) in ((2, (20, 15)), (4, (20, 15))):
        _tick(monkeypatch, at)
        _record(app, "A", 30, a_five, resets["A"])
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
    # a snapshot a credit's stale-replica cleanup reached. Under either fold
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
    in-place five-hour credit whose stale-replica removal deletes the
    window's FIRST snapshot (the band starts at floor10 of the arming tick).
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
    — although the credit's stale-replica removal deleted that snapshot —
    and the live computation-time pricing provenance."""
    ns = load_script()
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    monkeypatch.setattr(sys.modules["cctally"], "_nudge_dashboard_repaint",
                        lambda: None)
    live_open, surviving, live_closed, live_close = _block_close_trace(
        ns, monkeypatch, tmp_path / "live", rebuild=False)
    # Non-vacuity: the live first observation is the t=0 reading, whose
    # snapshot the stale-replica removal deleted, and live stamped pricing.
    assert live_open["first_observed_at_utc"].startswith(
        _iso(NOW).replace("Z", "")), live_open
    assert surviving > live_open["first_observed_at_utc"], (
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


# --------------------------------------------------------------------------
# #825/#832 fix round J5 (825-832-CD4-V01, #954): the FIRST WRITER, not the
# earliest capture, owns the open block's start columns. Two recorders of
# one account race: A captures first but appends last, so live ingest
# materializes the block from B's later reading and keeps B's start columns
# when A's earlier-captured reading is accepted after it.
# --------------------------------------------------------------------------

#: The block columns the first materializing writer owns (none is in the
#: live upsert's `DO UPDATE`).
_BLOCK_START_COLUMNS = (
    "first_observed_at_utc", "five_hour_resets_at", "block_start_at",
    "seven_day_pct_at_block_start",
)


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
        with open(core.DB_PATH, "r+b") as handle:
            handle.write(b"not a database " * 200)
        with pytest.raises(_cctally_db.StatsHealDeferred):
            core.open_db()
        assert st.cmd_stats_corruption_heal_internal(
            types.SimpleNamespace()) == 0
        return
    args = app.build_parser().parse_args(argv)
    assert args.func(args) == 0


def _out_of_order_trace(ns, monkeypatch, tmp_path, *, replay):
    """Recorder A captures 40% / 50% at t=0 and pauses before appending;
    recorder B captures 40% / 35% at t=2, appends and ingests first,
    materializing the block; A then appends and ingests its t=0 reading,
    which is accepted (its five-hour value is new). A later reading at t=4
    updates the block's mutable columns. ``replay`` (None for live) then
    runs there, and a reading of the successor window (t=6) closes the
    block. Returns the open block before the successor, the window's
    earliest surviving capture, the closed row and the durable close
    payload."""
    import _cctally_journal as jr
    import _cctally_statusline as sl
    import _lib_journal as J
    redirect_paths(ns, monkeypatch, tmp_path)
    app = sys.modules["cctally"]
    app.open_db().close()
    key = _identity("B")
    _retain_session_input(app, "B", key)
    real_append = jr.append_record
    paused = {"A": True}

    def append_after_b(record, *args, **kwargs):
        # A's usage observation reaches the journal only after B's
        # recording has appended and ingested in full.
        if paused["A"] and record.get("t") == "obs":
            paused["A"] = False
            _tick(monkeypatch, 2)
            _record(app, "B", 40, 35, _CLOSE_RESET)
        return real_append(record, *args, **kwargs)

    _tick(monkeypatch, 0)
    _identity("B")
    args = argparse.Namespace(
        percent=40, resets_at=str(NOW + 3 * 86400), five_hour_percent=50,
        five_hour_resets_at=str(_CLOSE_RESET), source="statusline")
    with monkeypatch.context() as patch:
        patch.setattr(jr, "append_record", append_after_b)
        assert app.record_usage_bound(args, binding=sl._capture_binding(),
                                      nudge_dashboard=False) == 0
    assert not paused["A"], "non-vacuity: B ran inside A's pause"
    _tick(monkeypatch, 4)
    _record(app, "B", 40, 55, _CLOSE_RESET)
    if replay is not None:
        _drive_replay(app, monkeypatch, replay)
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
    finally:
        conn.close()
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


@pytest.mark.parametrize(
    "replay", ["rebuild", "rederive", "epoch-transition", "corruption-heal"])
def test_m19_a_replayed_open_block_keeps_its_first_writer_under_out_of_order_capture(
        monkeypatch, tmp_path, replay):
    """825-832-CD4-V01: recorder A captures first and appends last. Live
    ingest materializes the block from B's later reading, and its upsert
    keeps B's start columns when A's earlier-captured reading is accepted
    after it. A replay re-materializes the open block with the same first
    writer, so the open row, the row a successor then closes and the
    durable `five_hour_block_close` match live, although a snapshot captured
    before the first writer survives."""
    ns = load_script()
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    monkeypatch.setattr(sys.modules["cctally"], "_nudge_dashboard_repaint",
                        lambda: None)
    live_open, surviving, live_closed, live_close = _out_of_order_trace(
        ns, monkeypatch, tmp_path / "live", replay=None)
    # Non-vacuity: B (t=2) is the first writer, and A's accepted t=0
    # snapshot survives, captured before it.
    assert live_open["first_observed_at_utc"].startswith(
        _iso(NOW + 2).replace("Z", "")), live_open
    assert surviving.startswith(_iso(NOW).replace("Z", "")), surviving
    assert surviving < live_open["first_observed_at_utc"]

    replayed_open, _surviving, closed, close = _out_of_order_trace(
        ns, monkeypatch, tmp_path / "replayed", replay=replay)
    for column in _BLOCK_START_COLUMNS:
        assert replayed_open[column] == live_open[column], (
            column, replayed_open, live_open)
        assert closed[column] == live_closed[column], (column, closed)
        assert close.get(column) == live_close.get(column), (column, close)
    for column in _OPEN_BLOCK_UNCOMPARED:
        replayed_open.pop(column)
        live_open.pop(column)
    assert replayed_open == live_open
    assert closed == live_closed
    assert close == live_close


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
