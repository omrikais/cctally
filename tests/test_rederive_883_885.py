"""#883/#885: correction apply, journal selection and independent rebuild.

The retained desired graphs are synthetic reproductions of the known planner
sequences. These tests use the real correction protocol, fold/rebuild, and
public breakdown command; they do not substitute a fake planner or selector.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from conftest import load_isolated_cctally_module
from tests.test_rederive_planner import (
    AT, _875_ACCOUNT, _875_CLOSE_ID, _875_PRICED_MODEL, _875_WINDOW,
    _875_close, _875_milestone, _875_referencing,
)


def _snapshot(journal, raw_id):
    minute = {"one": 1, "two": 2, "fresh": 3, "lost-support": 4}[
        raw_id.removeprefix("o:883-885-")]
    return journal.make_evt(kind="snapshot_accept", id=f"sa:{raw_id}", at=AT,
                            payload={
        "account_key": _875_ACCOUNT,
        "captured_at_utc": f"2026-07-26T12:{minute:02d}:00Z",
        "weekly_percent": 63.0, "five_hour_percent": 2.0,
        "five_hour_window_key": _875_WINDOW,
        "five_hour_resets_at": "2026-07-26T17:50:00Z",
        "week_start_date": "2026-07-20", "week_start_at": "2026-07-20T00:00:00Z",
        "week_end_date": "2026-07-27", "week_end_at": "2026-07-27T00:00:00Z",
        "source": "statusline", "payload_json": "{}",
    })


def _close(journal, *, total, models=None, **fields):
    row = _875_close(journal, total=total,
                     models=models or [{"model": "m", "cost_usd": total,
                                        "input_tokens": 300}],
                     block_start_at="2026-07-26T12:50:00Z",
                     five_hour_resets_at="2026-07-26T17:50:00Z",
                     created_at_utc=AT, last_updated_at_utc=AT, **fields)
    for child in row["payload"]["_models"] + row["payload"]["_projects"]:
        child["five_hour_window_key"] = _875_WINDOW
    return row


def _plan(rederive, journal, records, desired, *, held=(), markers=None):
    return rederive.build_claude_usage_plan(
        selection=journal.resolve_effective_events(records),
        desired_events=desired,
        journal_high_water=("observations-2026-07.jsonl", 10),
        cache_fingerprint="sha256:retained-inputs",
        config_fingerprint="sha256:retained-config", preserved_events=(),
        reviewed_weekly_hold_ids=held, historical_close_markers=markers,
    )


def _logical_money(path):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return {
            "blocks": list(conn.execute(
                "SELECT account_key, five_hour_window_key, is_closed, "
                "total_cost_usd, seven_day_pct_at_block_end, "
                "crossed_seven_day_reset, pricing_provenance_json "
                "FROM five_hour_blocks ORDER BY account_key,five_hour_window_key")),
            "milestones": list(conn.execute(
                "SELECT account_key, five_hour_window_key, percent_threshold, "
                "block_cost_usd, marginal_cost_usd, alerted_at "
                "FROM five_hour_milestones ORDER BY account_key, "
                "five_hour_window_key, percent_threshold")),
        }
    finally:
        conn.close()


@pytest.mark.parametrize("sequence", ["causal-chain", "weekly-marker", "support-loss"])
def test_correction_apply_repreview_and_rebuild_883_885(
    tmp_path, monkeypatch, capsys, sequence,
):
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _cctally_rederive as command
    import _lib_journal as journal
    import _lib_rederive as rederive

    first = _snapshot(journal, "o:883-885-one")
    second = _snapshot(journal, "o:883-885-two")
    one = _875_referencing(_875_milestone(journal, 1, 3.0, None), first)
    two = _875_referencing(_875_milestone(journal, 2, 6.0, 2.0), second)
    records = [_close(journal, total=6.0), first, second, one, two]
    held, markers = (), None
    if sequence == "causal-chain":
        fresh = _snapshot(journal, "o:883-885-fresh")
        next_one = _875_referencing(_875_milestone(journal, 1, 4.0, None), fresh)
        next_one["payload"]["seven_day_pct_at_crossing"] = 3.0
        desired = [records[0], first, second, fresh, next_one,
                   _875_referencing(_875_milestone(journal, 2, 6.0, 2.0), second)]
        # Give the successor a wrong current marginal to require an action
        # identical in B and R, and keep its unchanged crossing frozen.
        two["payload"]["marginal_cost_usd"] = 3.0
        baseline_desired = [*desired[:-2],
                            {**next_one, "payload": {**next_one["payload"],
                                                     "seven_day_pct_at_crossing": 2.0}},
                            desired[-1]]
        baseline = _plan(rederive, journal, records, baseline_desired)
        full = _plan(rederive, journal, records, desired)
        current = {row["id"]: row for row in records}
        plan = rederive.causal_delta_plan(baseline, full, current_events=current)
        assert {action.event_id for action in plan.actions} == {
            fresh["id"], one["id"], two["id"]}
    elif sequence == "weekly-marker":
        records[0] = _close(journal, total=0.0,
            models=[{"model": _875_PRICED_MODEL, "cost_usd": 0.0,
                     "input_tokens": 300}],
            seven_day_pct_at_block_start=60.0,
            seven_day_pct_at_block_end=63.0, crossed_seven_day_reset=0)
        replayed = _close(journal, total=14.0,
            models=[{"model": _875_PRICED_MODEL, "cost_usd": 14.0,
                     "input_tokens": 300}],
            pricing={"version": 1, "unpricedModels": []},
            seven_day_pct_at_block_start=60.0,
            seven_day_pct_at_block_end=15.0, crossed_seven_day_reset=1)
        desired = [replayed, second,
                   _875_referencing(_875_milestone(journal, 1, 7.0, None), first),
                   _875_referencing(_875_milestone(journal, 2, 14.0, 7.0), second)]
        held = (first["id"][3:],)
        entry = SimpleNamespace(
            source_account_key=_875_ACCOUNT, source_line_offset=0,
            source_path="/retained/synthetic/883-885.jsonl",
            timestamp=dt.datetime(2026, 7, 26, 12, tzinfo=dt.timezone.utc),
            model=_875_PRICED_MODEL, cost_usd=None, speed=None,
            input_tokens=300, output_tokens=0, cache_creation_tokens=0,
            cache_1h_tokens=None, cache_read_tokens=0)
        markers = command._historical_close_markers(
            journal.resolve_effective_events(records), desired,
            {(_875_ACCOUNT, _875_WINDOW): [entry]})
        assert set(markers) == {_875_CLOSE_ID}
        plan = _plan(rederive, journal, records, desired, held=held, markers=markers)
        close = next(a for a in plan.actions if a.event_id == _875_CLOSE_ID)
        assert close.payload["total_cost_usd"] == 0.0
        assert close.payload["_pricing"]["basis"] == "historical-zero-cost-inference"
        assert close.payload["seven_day_pct_at_block_end"] == 15.0
    else:
        support = _snapshot(journal, "o:883-885-lost-support")
        records.append(support)
        held = (support["id"][3:],)
        desired = [first, second,
                   _875_referencing(_875_milestone(journal, 1, 7.0, None), first),
                   _875_referencing(_875_milestone(journal, 2, 14.0, 7.0), second)]
        plan = _plan(rederive, journal, records, desired, held=held)
        assert {a.event_id for a in plan.actions if a.disposition == "tombstone"} == {
            support["id"], _875_CLOSE_ID}
    corrected = [*records, *journal.make_correction_batch(
        batch_id=f"rederive:claude-usage:883-885-{sequence}",
        family="claude-usage", at=AT, actions=plan.to_correction_actions())]
    selection = journal.resolve_effective_events(corrected)
    assert not selection.protocol_violations
    assert not selection.conflicts
    next_plan = _plan(rederive, journal, corrected, desired, held=held, markers=markers)
    assert next_plan.actions == ()
    fixed = dt.datetime(2026, 7, 26, 12, tzinfo=dt.timezone.utc)
    runtime.append_records(corrected, now_utc=fixed)
    runtime.rebuild_stats_index(context=runtime.RebuildContext(trigger="test-fixture"))
    independent = tmp_path / "883-885-independent.db"
    runtime.rebuild_stats_index(context=runtime.RebuildContext(trigger="test-fixture"),
                                target_path=independent, update_quota_cache=False)
    logical = _logical_money(mod.DB_PATH)
    assert logical == _logical_money(independent)
    expected = {
        "causal-chain": [(1, 4.0, None), (2, 6.0, 2.0)],
        "weekly-marker": [(1, 3.0, None), (2, 6.0, 3.0)],
        "support-loss": [(1, 7.0, None), (2, 14.0, 7.0)],
    }[sequence]
    assert [(row[2], row[3], row[4]) for row in logical["milestones"]] == expected
    args = mod.build_parser().parse_args([
        "five-hour-breakdown", "--block-start", "2026-07-26T12:50:00Z", "--json"])
    if sequence == "support-loss":
        assert len(logical["blocks"]) == 1
        assert logical["blocks"][0][2] == 0  # mutable projection, no frozen close
    assert mod.cmd_five_hour_breakdown(args) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert [(m["percentThreshold"], m["blockCostUSD"], m["marginalCostUSD"])
            for m in rendered["milestones"]] == expected
    print(json.dumps({"issue": 883 if sequence == "causal-chain" else 885,
                      "sequence": sequence, "applyActions": len(plan.actions),
                      "nextActions": len(next_plan.actions),
                      "rebuildParity": True, "publicBreakdown": expected}))
