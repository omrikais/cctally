"""Raw observations and retained cache through the operator rederive command."""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from conftest import load_isolated_cctally_module
from tests.test_rederive_command import _persistent_tree, _logical_dump


ACCOUNT = "acct-883-885-raw"
MODEL = "claude-3-5-sonnet-20241022"


def _raw_fixture(mod, points):
    import _lib_journal as journal
    import _cctally_rederive as command

    path = "/synthetic/883-885/session.jsonl"
    conn = mod.open_cache_db()
    conn.execute(
        "INSERT INTO session_files "
        "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at,session_id,project_path) "
        "VALUES (?,?,?,?,?,?,?)",
        (path, 1000, 1, 1000, "2026-07-25T12:00:00Z", "883-885", "/synthetic/883-885"),
    )
    for offset, stamp in enumerate(("2026-07-25T11:55:00+00:00",
                                    "2026-07-25T12:04:00+00:00",
                                    "2026-07-25T12:09:00+00:00",
                                    "2026-07-25T16:00:00+00:00")):
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path,line_offset,timestamp_utc,model,input_tokens,output_tokens,"
            "cache_create_tokens,cache_read_tokens,cache_create_1h_tokens,account_key) "
            "VALUES (?,?,?,?,1000000,0,0,0,0,?)",
            (path, offset * 100, stamp, MODEL, ACCOUNT),
        )
    conn.commit()
    records = []
    for at, weekly, five, reset in points:
        records.append(journal.make_obs(
            at=at, src="record-usage", provider="claude", account=ACCOUNT,
            payload={"captured_at": at, "source": "statusline",
                     "weekly_percent": weekly,
                     "resets_at": int(dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc).timestamp()),
                     "five_hour_percent": five, "five_hour_resets_at": reset},
        ))
    plan = command.plan_claude_usage(
        records, cache_conn=conn,
        journal_high_water=("observations-2026-07.jsonl", 1),
    )
    conn.close()
    events = []
    for action in plan.actions:
        assert action.disposition == "add"
        payload = copy.deepcopy(action.payload)
        events.append(journal.make_evt(kind=payload.pop("kind"), id=action.event_id,
                                       at=action.at, payload=payload))
    return records, events


def _append_fixture(mod, records, events):
    import _cctally_journal as runtime

    runtime.append_records([*records, *events],
        now_utc=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc))
    runtime.rebuild_stats_index(context=runtime.RebuildContext(trigger="test-fixture"))


def _manifest(tmp_path, records):
    import _cctally_journal as runtime

    high_water = runtime.journal_high_water()
    body = {
        "schemaVersion": 2,
        "journalHighWater": {"segment": high_water[0], "offset": high_water[1]},
        "journalPrefixHash": runtime.journal_prefix_hash(high_water),
        "reviewedAt": "2026-07-25T17:00:00Z",
        "reason": "Synthetic retained-evidence review for #883/#885",
        "weeklyAxisDecisions": [{"observationId": row["id"], "disposition": "hold"}
                                for row in records],
        "snapshotIdentityDecisions": [],
    }
    path = tmp_path / "reviewed-883-885.json"
    path.write_text(json.dumps(body))
    return path, body


def _command(mod, capsys, *, manifest=None, apply=False):
    args = ["db", "rederive", "--family", "claude-usage", "--json"]
    if manifest is not None:
        args += ["--reviewed-weekly-decisions", str(manifest)]
    if apply:
        args += ["--yes"]
    code = mod.cmd_db_rederive(mod.build_parser().parse_args(args))
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert code == 0, (code, body, captured.err)
    return body


def _fingerprint(mod):
    import _cctally_rederive as command

    conn = mod.open_cache_db()
    try:
        return command._cache_fingerprint(conn)
    finally:
        conn.close()


def _apply_review(mod, capsys, tmp_path, held):
    path, body = _manifest(tmp_path, held)
    preview = _command(mod, capsys, manifest=path)
    body.update(expectedBaselinePlanHash=preview["baselinePlanHash"],
                expectedDecisionPlanHash=preview["decisionPlanHash"])
    path.write_text(json.dumps(body))
    before = _persistent_tree(mod.APP_DIR)
    pinned = _command(mod, capsys, manifest=path)
    assert _persistent_tree(mod.APP_DIR) == before
    applied = _command(mod, capsys, manifest=path, apply=True)
    assert applied["status"] == "applied", applied
    assert applied["planHash"] == pinned["planHash"]
    return applied


def _assert_final(mod, capsys, tmp_path, *, sequence, applied, fingerprint, issue=885):
    import _cctally_journal as runtime

    before = _persistent_tree(mod.APP_DIR)
    second = _command(mod, capsys)
    assert second["status"] == "no-op", second
    assert all(second["actionCounts"][kind] == 0
               for kind in ("add", "supersede", "tombstone")), second
    assert _persistent_tree(mod.APP_DIR) == before
    assert _fingerprint(mod) == fingerprint
    independent = tmp_path / "883-885-raw-independent.db"
    runtime.rebuild_stats_index(context=runtime.RebuildContext(trigger="test-fixture"),
                                target_path=independent, update_quota_cache=False)
    assert _logical_dump(mod.DB_PATH) == _logical_dump(independent)
    print(json.dumps({"issue": issue, "sequence": sequence,
                      "applyStatus": applied["status"],
                      "applyActions": applied["actionCounts"],
                      "nextStatus": second["status"],
                      "nextActions": second["actionCounts"],
                      "stableCache": True, "writeFreePreview": True,
                      "independentRebuildParity": True}))


def test_raw_command_weekly_marker_converges_885(tmp_path, monkeypatch, capsys):
    """Current raw replay proves the historical marker before hold merging.

    Only retained derived historical facts are changed: the close and its
    first crossings represent the pre-card $0 state, with stale weekly axes.
    The normally derived cache population proves the unmarked close's marker.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_rederive as command
    import _lib_journal as journal
    import _cctally_journal as runtime

    points = [("2026-07-25T12:00:00Z", 60., 1., "2026-07-25T15:00:00Z"),
              ("2026-07-25T12:05:00Z", 63., 2., "2026-07-25T15:00:00Z"),
              ("2026-07-25T12:10:00Z", 15., 3., "2026-07-25T15:00:00Z"),
              ("2026-07-25T16:05:00Z", 15., 1., "2026-07-25T20:00:00Z")]
    records, events = _raw_fixture(mod, points)
    close = next(e for e in events if e["payload"]["kind"] == "five_hour_block_close")
    window = close["payload"]["five_hour_window_key"]
    p = close["payload"]
    p.update(total_cost_usd=0., seven_day_pct_at_block_end=63.,
             crossed_seven_day_reset=0, pricing_provenance_json=None)
    p.pop("_pricing")
    for child in p["_models"] + p["_projects"]:
        child["cost_usd"] = 0.
    for e in events:
        p = e["payload"]
        if p["kind"] == "five_hour_milestone" and p["five_hour_window_key"] == window:
            p["block_cost_usd"] = 0.
            if p["marginal_cost_usd"] is not None:
                p["marginal_cost_usd"] = 0.
            # A retained historical notification is not replay-derived.
            p["alerted_at"] = "2026-07-25T12:06:00Z"
    _append_fixture(mod, records, events)
    fingerprint = _fingerprint(mod)
    applied = _apply_review(mod, capsys, tmp_path, [records[1]])
    retained = mod.read_rederive_journal_prefix()[0]
    selected = journal.resolve_effective_events(retained)
    final = selected.by_id[close["id"]].record["payload"]
    assert final["total_cost_usd"] == 0.
    assert final["seven_day_pct_at_block_end"] == 15.
    assert final["crossed_seven_day_reset"] == 1
    assert final["_pricing"]["basis"] == "historical-zero-cost-inference"
    for e in events:
        p = e["payload"]
        if p["kind"] == "five_hour_milestone" and p["five_hour_window_key"] == window:
            f = selected.by_id[e["id"]].record
            assert f["at"] == e["at"]
            assert f["payload"] == p
    _assert_final(mod, capsys, tmp_path, sequence="weekly-marker", applied=applied,
                  fingerprint=fingerprint)


def test_raw_command_lost_hold_support_converges_885(tmp_path, monkeypatch, capsys):
    """A stale ordinary close supported only by a retired held snapshot.

    A real reviewed command durably records the hold with authentic pins.
    A later retained raw capture makes the following operator pass a full
    replay. The isolated historical revision recreates an obsolete unfloored
    window key; it never supplies a desired graph or bypasses selection.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_journal as journal
    import _cctally_journal as runtime

    points = [("2026-07-25T12:00:00Z", 60., 1., "2026-07-25T15:09:00Z"),
              ("2026-07-25T12:05:00Z", 60.5, 1., "2026-07-25T15:09:00Z"),
              ("2026-07-25T16:05:00Z", 60.5, 2., "2026-07-25T20:00:00Z")]
    records, events = _raw_fixture(mod, points)
    _append_fixture(mod, records, events)
    _apply_review(mod, capsys, tmp_path, [records[1]])
    retained = mod.read_rederive_journal_prefix()[0]
    selected = journal.resolve_effective_events(retained)
    snapshot_id = "sa:" + records[1]["id"]
    # A sub-percent weekly change records a snapshot but crosses neither
    # milestone axis. An integer rise would give it weekly dependents and
    # correctly preserve its support, so it cannot model #885's lost support.
    # The rollover also changes five-hour usage, giving its own milestone a
    # fresh snapshot rather than referring back to this otherwise flat tick.
    dependents = [{"id": e["id"], "kind": e["payload"]["kind"],
                   "threshold": e["payload"].get("percent_threshold"),
                   "at": e["at"], "captured": e["payload"].get("captured_at_utc")}
                  for e in events if e["payload"].get("usage_snapshot_ref") == snapshot_id]
    assert not dependents, json.dumps(dependents, sort_keys=True)
    assert selected.by_id[snapshot_id].status == "tombstone"
    snapshot = copy.deepcopy(next(e for e in events if e["id"] == snapshot_id))
    stale_close = copy.deepcopy(next(e for e in events
                                  if e["payload"]["kind"] == "five_hour_block_close"))
    window = stale_close["payload"]["five_hour_window_key"] + 540
    stale_close["id"] = f"fhbc:{ACCOUNT}:{window}"
    stale_close["payload"]["five_hour_window_key"] = window
    for child in stale_close["payload"]["_models"] + stale_close["payload"]["_projects"]:
        child["five_hour_window_key"] = window
    snapshot["payload"]["five_hour_window_key"] = window
    # Neither axis crossed a threshold. The review already retired this
    # unreferenced snapshot once; an old retained revision recreates it.
    actions = [{"action": "replace", "id": snapshot_id,
                "rev": selected.by_id[snapshot_id].rev + 1,
                "at": snapshot["at"], "payload": snapshot["payload"]},
               {"action": "replace", "id": stale_close["id"], "rev": 1,
                "at": stale_close["at"], "payload": stale_close["payload"]}]
    runtime.append_records(journal.make_correction_batch(
        batch_id="synthetic-883-885-retained-window-history", family="claude-usage",
        at="2026-07-25T16:10:00Z", actions=actions))
    later = journal.make_obs(at="2026-07-25T16:15:00Z", src="record-usage",
        provider="claude", account=ACCOUNT,
        payload={**records[-1]["payload"], "captured_at": "2026-07-25T16:15:00Z"})
    runtime.append_record(later)
    runtime.rebuild_stats_index(context=runtime.RebuildContext(trigger="test-fixture"))
    fingerprint = _fingerprint(mod)
    before = _persistent_tree(mod.APP_DIR)
    first = _command(mod, capsys)
    assert _persistent_tree(mod.APP_DIR) == before
    applied = _command(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    retained = mod.read_rederive_journal_prefix()[0]
    selected = journal.resolve_effective_events(retained)
    assert selected.by_id[snapshot_id].status == "tombstone", {
        "support": selected.by_id[snapshot_id].record,
        "dependents": [event.record for event in selected.by_id.values()
                       if event.status == "active" and
                       (event.record.get("payload") or {}).get("usage_snapshot_ref")
                       == snapshot_id],
    }
    assert selected.by_id[stale_close["id"]].status == "tombstone"
    _assert_final(mod, capsys, tmp_path, sequence="lost-support", applied=applied,
                  fingerprint=fingerprint)


@pytest.mark.parametrize("direction", ["predecessor", "replacing-close"])
def test_raw_command_causal_chain_883(tmp_path, monkeypatch, capsys, direction):
    """A real reviewed decision must bring identical B/R monetary actions."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_rederive as command
    import _cctally_journal as runtime
    import _lib_journal as journal

    points = [("2026-07-25T12:00:00Z", 60., 1., "2026-07-25T15:00:00Z"),
              ("2026-07-25T12:05:00Z", 63., 2., "2026-07-25T15:00:00Z"),
              ("2026-07-25T12:10:00Z", 61., 3., "2026-07-25T15:00:00Z"),
              ("2026-07-25T12:15:00Z", 64., 4., "2026-07-25T15:00:00Z"),
              ("2026-07-25T16:05:00Z", 65., 1., "2026-07-25T20:00:00Z")]
    if direction == "replacing-close":
        # This sub-percent flat-five tick updates the close's weekly end
        # without creating a milestone that would preserve its held snapshot.
        points.insert(-1, ("2026-07-25T12:20:00Z", 64.5, 4., "2026-07-25T15:00:00Z"))
    records, events = _raw_fixture(mod, points)
    close = next(e for e in events if e["payload"]["kind"] == "five_hour_block_close")
    window = close["payload"]["five_hour_window_key"]
    milestones = {e["payload"]["percent_threshold"]: e for e in events
                  if e["payload"]["kind"] == "five_hour_milestone"
                  and e["payload"]["five_hour_window_key"] == window}
    if direction == "predecessor":
        # R preserves threshold 2 as reviewed held history while B corrects
        # it. Threshold 3's resulting marginal differs between B and R, so
        # its cost correction is causal; threshold 4's correction is equal.
        milestones[2]["payload"].update(seven_day_pct_at_crossing=59., block_cost_usd=3.)
        milestones[3]["payload"].update(seven_day_pct_at_crossing=59., block_cost_usd=0.)
        milestones[4]["payload"]["marginal_cost_usd"] = milestones[4]["payload"]["block_cost_usd"]
        held = [records[1]]
    else:
        close["payload"].update(total_cost_usd=1., seven_day_pct_at_block_end=59.)
        for row in milestones.values():
            row["payload"]["block_cost_usd"] = 0.
            if row["payload"]["marginal_cost_usd"] is not None:
                row["payload"]["marginal_cost_usd"] = 0.
        held = [records[-2]]
        assert not any(e["payload"].get("usage_snapshot_ref") == "sa:" + held[0]["id"]
                       for e in events)
    _append_fixture(mod, records, events)
    fingerprint = _fingerprint(mod)
    path, _body = _manifest(tmp_path, held)
    op = command._reviewed_weekly_op_from_manifest(path)
    retained, hw, _ends, _protocol = mod.read_rederive_journal_prefix()
    conn = mod.open_cache_db()
    try:
        baseline = command.plan_claude_usage(retained, cache_conn=conn, journal_high_water=hw)
        reviewed = command.plan_claude_usage([*retained, op], cache_conn=conn, journal_high_water=hw)
    finally:
        conn.close()
    b = {a.event_id: a.to_correction_action() for a in baseline.actions}
    r = {a.event_id: a.to_correction_action() for a in reviewed.actions}
    trigger = milestones[3]["id"] if direction == "predecessor" else close["id"]
    sibling = milestones[4]["id"]
    assert trigger in b and trigger in r and b[trigger] != r[trigger], json.dumps({
        "baseline_trigger": b.get(trigger), "reviewed_trigger": r.get(trigger),
        "causal_differences": {key: {"baseline": b.get(key), "reviewed": r.get(key)}
                               for key in b.keys() | r.keys() if b.get(key) != r.get(key)},
    }, sort_keys=True)
    assert sibling in b and b[sibling] == r[sibling], (b, r)
    applied = _apply_review(mod, capsys, tmp_path, held)
    selected = journal.resolve_effective_events(mod.read_rederive_journal_prefix()[0])
    for threshold in (3, 4):
        row = selected.by_id[milestones[threshold]["id"]].record["payload"]
        assert row == r[milestones[threshold]["id"]]["payload"]
    predecessor = selected.by_id[milestones[3]["id"]].record["payload"]
    successor = selected.by_id[sibling].record["payload"]
    assert successor["marginal_cost_usd"] == pytest.approx(
        successor["block_cost_usd"] - predecessor["block_cost_usd"], abs=1e-9)
    _assert_final(mod, capsys, tmp_path, issue=883, sequence=direction,
                  applied=applied, fingerprint=fingerprint)
