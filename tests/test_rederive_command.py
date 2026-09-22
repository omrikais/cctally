"""#372 Task C — fail-safe `cctally db rederive` orchestration."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import signal
import subprocess
import time

import pytest

from tests._support_http import PRESENCE_BACKSTOP_SECONDS, remaining

#: One budget for the three child `db rederive` runs of the SIGKILL
#: recovery test. Three presence backstops, because each run is a real
#: child process rather than an in-process wait, and the three are
#: sequential: the crash run cannot start until the preview has returned.
_CHILD_RUNS_BUDGET_S = 3 * PRESENCE_BACKSTOP_SECONDS


AT = "2026-07-25T12:00:00Z"


def _isolated(tmp_path, monkeypatch):
    from conftest import load_isolated_cctally_module

    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _seed_cache(mod):
    path = "/tmp/claude/projects/repo/session.jsonl"
    conn = mod.open_cache_db()
    conn.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
        (path, 100, 1, 100, AT, "session-a", "/repo"),
    )
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, "
        " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            path,
            0,
            "2026-07-25T11:00:00+00:00",
            "claude-3-5-sonnet-20241022",
            0,
            0,
            100,
            0,
            40,
            "acct-a",
        ),
    )
    conn.commit()
    conn.close()


def _raw_obs(lib):
    resets = int(
        dt.datetime(2026, 7, 27, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    )
    return lib.make_obs(
        at=AT,
        src="record-usage",
        provider="claude",
        account="acct-a",
        payload={
            "captured_at": AT,
            "source": "statusline",
            "weekly_percent": 10.0,
            "resets_at": resets,
        },
    )


def _seed_wrong_journal(mod):
    import _cctally_journal as runtime
    import _lib_journal as journal

    obs = _raw_obs(journal)
    cache = mod.open_cache_db()
    desired = mod.plan_claude_usage_rederive(
        [obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1),
    )
    cache.close()
    wrong_events = []
    for action in desired.actions:
        payload = dict(action.payload or {})
        if payload.get("kind") == "snapshot_accept":
            payload["weekly_percent"] = 99.0
        if payload.get("kind") == "weekly_cost_snapshot":
            payload["cost_usd"] += 5.0
        wrong_events.append(
            journal.make_evt(
                kind=payload.pop("kind"),
                id=action.event_id,
                at=action.at,
                payload=payload,
            )
        )
    fixed = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
    for record in [obs, *wrong_events]:
        runtime.append_record(record, now_utc=fixed)
    runtime.rebuild_stats_index(context=runtime.RebuildContext(trigger="test-fixture"))
    return obs, wrong_events


def _args(*, yes=False, as_json=True):
    return argparse.Namespace(family="claude-usage", yes=yes, json=as_json)


def _journal_bytes(mod):
    return {
        path.name: path.read_bytes()
        for path in sorted(mod.JOURNAL_DIR.glob("*.jsonl"))
    }


def _persistent_tree(root):
    root = pathlib.Path(root)
    return {
        path.relative_to(root).as_posix(): (
            "dir" if path.is_dir() else path.read_bytes()
        )
        for path in sorted(root.rglob("*"))
    }


def _logical_dump(path):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        out = {}
        for table in (
            "weekly_usage_snapshots",
            "weekly_cost_snapshots",
            "week_reset_events",
            "five_hour_reset_events",
            "five_hour_blocks",
            "five_hour_block_models",
            "five_hour_block_projects",
            "weekly_credit_floors",
            "percent_milestones",
            "five_hour_milestones",
            "budget_milestones",
            "projected_milestones",
            "project_budget_milestones",
            "quota_alert_arming",
            "quota_window_blocks",
            "quota_percent_milestones",
            "quota_threshold_events",
            "accounts",
            "journal_effective_events",
            "journal_protocol_violations",
            "journal_cursor",
        ):
            columns = [
                row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                if row[1] not in {
                    "id", "usage_snapshot_id", "cost_snapshot_id",
                    "reset_event_id", "block_id",
                    # The trailing open block is a time-sensitive projection.
                    "created_at_utc", "last_updated_at_utc", "is_closed",
                    # Quota projection generations are per rebuild, not truth.
                    "generation",
                }
            ]
            out[table] = conn.execute(
                "SELECT " + ",".join(columns) + f" FROM {table} "
                + "ORDER BY " + ",".join(columns)
            ).fetchall()
        return out
    finally:
        conn.close()


def test_parser_registers_preview_first_rederive_surface(cctally_module):
    parser = cctally_module.build_parser()

    preview = parser.parse_args(["db", "rederive", "--family", "claude-usage"])
    apply = parser.parse_args(
        ["db", "rederive", "--family", "claude-usage", "--yes", "--json"]
    )
    unsupported = parser.parse_args(
        ["db", "rederive", "--family", "future-family", "--json"]
    )

    assert callable(preview.func)
    assert preview.func.__name__ == "cmd_db_rederive"
    assert preview.yes is False
    assert preview.json is False
    assert apply.yes is True
    assert apply.json is True
    assert unsupported.family == "future-family"


def test_reviewed_weekly_decision_command_is_durable_and_idempotent(
    tmp_path, monkeypatch, capsys,
):
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    import _cctally_journal as runtime
    import _cctally_rederive as rederive
    import _lib_journal as journal
    import _lib_rederive as lib_rederive

    records = []
    for minute, (weekly, end_day) in enumerate(
        ((60, 27), (10, 28), (60, 27), (10, 28), (60, 27), (10, 28))
    ):
        captured = f"2026-07-25T12:{minute:02d}:00Z"
        payload = dict(_raw_obs(journal)["payload"])
        payload.update({
            "captured_at": captured,
            "weekly_percent": weekly,
            "resets_at": int(dt.datetime(
                2026, 7, end_day, tzinfo=dt.timezone.utc).timestamp()),
            "five_hour_percent": 20 + minute,
            "five_hour_resets_at": "2026-07-25T16:00:00Z",
        })
        records.append(journal.make_obs(
            at=captured, src="record-usage", provider="claude",
            account="acct-a", payload=payload,
        ))
    runtime.append_records(
        records, now_utc=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc),
    )
    high_water = runtime.journal_high_water()
    manifest = {
        "schemaVersion": 2,
        "journalHighWater": {
            "segment": high_water[0], "offset": high_water[1],
        },
        "journalPrefixHash": runtime.journal_prefix_hash(high_water),
        "reviewedAt": "2026-07-25T13:00:00Z",
        "reason": "Reviewed stale replica captures against retained evidence",
        "weeklyAxisDecisions": [
            {"observationId": record["id"], "disposition": "hold"}
            for record in records[2:]
        ],
        "snapshotIdentityDecisions": [],
    }
    path = tmp_path / "reviewed-decisions.json"
    path.write_text(json.dumps(manifest))
    cache = mod.open_cache_db()
    stable_cache_fingerprint = rederive._cache_fingerprint(cache)
    cache.close()
    parser = mod.build_parser()
    preview_args = parser.parse_args([
        "db", "rederive", "--family", "claude-usage",
        "--reviewed-weekly-decisions", str(path), "--json",
    ])
    assert not (mod.APP_DIR / "logs").exists()
    before = _persistent_tree(mod.APP_DIR)
    assert mod.cmd_db_rederive(preview_args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "preview"
    assert preview["planHash"]
    assert preview["baselinePlanHash"]
    assert preview["baselineActionCounts"]
    assert sum(
        preview["baselineActionCounts"][name]
        for name in ("supersede", "tombstone", "add")
    ) > sum(
        preview["decisionActionCounts"][name]
        for name in ("supersede", "tombstone", "add")
    )
    assert preview["reviewedWeeklyDecisionId"]
    assert _persistent_tree(mod.APP_DIR) == before

    text_args = parser.parse_args([
        "db", "rederive", "--family", "claude-usage",
        "--reviewed-weekly-decisions", str(path),
    ])
    assert mod.cmd_db_rederive(text_args) == 0
    text_preview = capsys.readouterr().out
    assert f"baseline plan {preview['baselinePlanHash']}" in text_preview
    assert f"decision plan {preview['decisionPlanHash']}" in text_preview
    assert "baseline action counts:" in text_preview
    assert "decision action counts:" in text_preview

    alternate = dict(manifest, reason="Different operator evidence review")
    alternate_path = tmp_path / "alternate-review.json"
    alternate_path.write_text(json.dumps(alternate))
    alternate_args = parser.parse_args([
        "db", "rederive", "--family", "claude-usage",
        "--reviewed-weekly-decisions", str(alternate_path), "--json",
    ])
    assert mod.cmd_db_rederive(alternate_args) == 0
    assert json.loads(capsys.readouterr().out)["planHash"] != preview["planHash"]
    assert _persistent_tree(mod.APP_DIR) == before

    apply_args = parser.parse_args([
        "db", "rederive", "--family", "claude-usage",
        "--reviewed-weekly-decisions", str(path), "--yes", "--json",
    ])
    assert mod.cmd_db_rederive(apply_args) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["status"] == "conflict"
    assert "expectedBaselinePlanHash" in refused["conflicts"][0]
    assert _persistent_tree(mod.APP_DIR) == before

    legacy_path = tmp_path / "legacy-reviewed-decisions.json"
    legacy_path.write_text(json.dumps({
        "schemaVersion": 1,
        "journalHighWater": manifest["journalHighWater"],
        "journalPrefixHash": manifest["journalPrefixHash"],
        "reviewedAt": manifest["reviewedAt"],
        "reason": manifest["reason"],
        "decisions": manifest["weeklyAxisDecisions"],
    }))
    with pytest.raises(
        lib_rederive.RederiveConflict,
        match="schemaVersion 1 apply requires expectedBaselinePlanHash",
    ):
        rederive._reviewed_weekly_expected_hashes(
            legacy_path, required=True,
        )

    manifest.update({
        "expectedBaselinePlanHash": "sha256:" + "0" * 64,
        "expectedDecisionPlanHash": preview["decisionPlanHash"],
    })
    path.write_text(json.dumps(manifest))
    stable_journal = _journal_bytes(mod)
    stable_stats = mod.DB_PATH.read_bytes() if mod.DB_PATH.exists() else None
    assert mod.cmd_db_rederive(apply_args) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["status"] == "conflict"
    assert "baseline plan hash drifted" in refused["conflicts"][0]
    assert _journal_bytes(mod) == stable_journal
    assert (mod.DB_PATH.read_bytes() if mod.DB_PATH.exists() else None) == stable_stats
    cache = mod.open_cache_db()
    assert rederive._cache_fingerprint(cache) == stable_cache_fingerprint
    cache.close()

    manifest["expectedBaselinePlanHash"] = preview["baselinePlanHash"]
    manifest["expectedDecisionPlanHash"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(manifest))
    assert mod.cmd_db_rederive(apply_args) == 2
    refused = json.loads(capsys.readouterr().out)
    assert "decision plan hash drifted" in refused["conflicts"][0]
    assert _journal_bytes(mod) == stable_journal
    cache = mod.open_cache_db()
    assert rederive._cache_fingerprint(cache) == stable_cache_fingerprint
    cache.close()

    manifest["expectedDecisionPlanHash"] = preview["decisionPlanHash"]
    path.write_text(json.dumps(manifest))
    assert mod.cmd_db_rederive(preview_args) == 0
    pinned_preview = json.loads(capsys.readouterr().out)
    assert pinned_preview["baselinePlanHash"] == preview["baselinePlanHash"]
    assert pinned_preview["decisionPlanHash"] == preview["decisionPlanHash"]
    assert (pinned_preview["reviewedWeeklyDecisionId"]
            != preview["reviewedWeeklyDecisionId"])
    assert mod.cmd_db_rederive(apply_args) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert (applied["reviewedWeeklyDecisionId"]
            == pinned_preview["reviewedWeeklyDecisionId"])
    assert applied["planHash"] == preview["planHash"]
    assert applied["batchId"] == preview["batchId"]
    after = mod.read_rederive_journal_prefix()[0]
    assert {
        record["id"] for record in after
        if record.get("t") == "correction_batch"
    } == {preview["batchId"]}
    assert sum(
        record.get("t") == "correction"
        and record.get("batch") == preview["batchId"]
        for record in after
    ) == sum(
        preview["decisionActionCounts"][name]
        for name in ("supersede", "tombstone", "add")
    )
    assert sum(
        record.get("t") == "op"
        and (record.get("payload") or {}).get("kind")
        == "claude_weekly_observation_decision"
        for record in after
    ) == 1
    stable = _journal_bytes(mod)
    assert mod.cmd_db_rederive(_args(yes=True)) == 0
    plain_retry = json.loads(capsys.readouterr().out)
    assert plain_retry["status"] == "no-op", {
        key: plain_retry[key] for key in (
            "status", "batchId", "planHash", "actionCounts",
            "actionCountsByEventKind",
        )
    }
    assert _journal_bytes(mod) == stable
    assert mod.cmd_db_rederive(apply_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "no-op"
    assert _journal_bytes(mod) == stable

    independent = tmp_path / "reviewed-rebuild.db"
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        target_path=independent,
    )
    assert _logical_dump(mod.DB_PATH) == _logical_dump(independent)

    later_high_water = runtime.journal_high_water()
    accept_manifest = {
        **{
            key: value for key, value in manifest.items()
            if key not in {
                "expectedBaselinePlanHash", "expectedDecisionPlanHash",
            }
        },
        "journalHighWater": {
            "segment": later_high_water[0], "offset": later_high_water[1],
        },
        "journalPrefixHash": runtime.journal_prefix_hash(later_high_water),
        "reviewedAt": "2026-07-25T14:00:00Z",
        "reason": "Reviewed first pair as genuine later readings",
        "weeklyAxisDecisions": [
            {"observationId": record["id"], "disposition": "accept"}
            for record in records[2:4]
        ],
    }
    accept_path = tmp_path / "accept-review.json"
    accept_path.write_text(json.dumps(accept_manifest))
    accept_args = parser.parse_args([
        "db", "rederive", "--family", "claude-usage",
        "--reviewed-weekly-decisions", str(accept_path), "--yes", "--json",
    ])
    accept_preview_args = parser.parse_args([
        "db", "rederive", "--family", "claude-usage",
        "--reviewed-weekly-decisions", str(accept_path), "--json",
    ])
    assert mod.cmd_db_rederive(accept_preview_args) == 0
    accept_preview = json.loads(capsys.readouterr().out)
    accept_manifest.update({
        "expectedBaselinePlanHash": accept_preview["baselinePlanHash"],
        "expectedDecisionPlanHash": accept_preview["decisionPlanHash"],
    })
    accept_path.write_text(json.dumps(accept_manifest))
    assert mod.cmd_db_rederive(accept_args) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "applied"
    assert accepted["decisionActionCounts"]["add"] == 1
    conn = mod.open_db()
    try:
        # The operator delta adds the newly authorized reset. Any reset shared
        # by the baseline and reviewed desired graphs remains baseline drift.
        assert conn.execute("SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 1
    finally:
        conn.close()


def test_reviewed_weekly_completed_op_recovers_with_plain_rederive(
    tmp_path, monkeypatch, capsys,
):
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    import _cctally_journal as runtime
    import _cctally_rederive as rederive
    import _lib_journal as journal

    first = _raw_obs(journal)
    later_payload = dict(first["payload"])
    later_payload.update({
        "captured_at": "2026-07-25T12:01:00Z",
        "weekly_percent": 5.0,
        "five_hour_percent": 21.0,
        "five_hour_resets_at": "2026-07-25T16:00:00Z",
    })
    later = journal.make_obs(
        at="2026-07-25T12:01:00Z", src="record-usage",
        provider="claude", account="acct-a", payload=later_payload,
    )
    runtime.append_records(
        [first, later],
        now_utc=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc),
    )
    high_water = runtime.journal_high_water()
    manifest = {
        "schemaVersion": 2,
        "journalHighWater": {
            "segment": high_water[0], "offset": high_water[1],
        },
        "journalPrefixHash": runtime.journal_prefix_hash(high_water),
        "reviewedAt": "2026-07-25T13:00:00Z",
        "reason": "Reviewed one stale low capture",
        "weeklyAxisDecisions": [{
            "observationId": later["id"], "disposition": "hold",
        }],
        "snapshotIdentityDecisions": [],
    }
    path = tmp_path / "crash-review.json"
    path.write_text(json.dumps(manifest))
    op = rederive._reviewed_weekly_op_from_manifest(path)
    pre = rederive.preview_db_rederive("claude-usage", reviewed_op=op)
    expected_hashes = (pre.baseline_plan.plan_hash, pre.plan.plan_hash)
    manifest.update({
        "expectedBaselinePlanHash": expected_hashes[0],
        "expectedDecisionPlanHash": expected_hashes[1],
    })
    path.write_text(json.dumps(manifest))
    op = rederive._reviewed_weekly_op_from_manifest(path)
    assert op["payload"]["expected_baseline_plan_hash"] == expected_hashes[0]
    assert op["payload"]["expected_decision_plan_hash"] == expected_hashes[1]
    pre = rederive.preview_db_rederive("claude-usage", reviewed_op=op)

    def crash(stage):
        if stage == "after-reviewed-decision-op":
            raise RuntimeError("simulated crash after durable op")

    monkeypatch.setattr(rederive, "_REDERIVE_CRASH_HOOK", crash)
    with pytest.raises(RuntimeError, match="durable op"):
        rederive.apply_db_rederive(
            "claude-usage", reviewed_op=op,
            expected_plan_hashes=expected_hashes,
        )
    monkeypatch.setattr(rederive, "_REDERIVE_CRASH_HOOK", None)
    after_op = mod.read_rederive_journal_prefix()[0]
    assert after_op[-1] == op
    assert not any(record.get("t") == "correction_batch" for record in after_op)

    cache = mod.open_cache_db()
    cache.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, "
        " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "/tmp/claude/projects/repo/session.jsonl", 1,
            "2026-07-25T11:30:00+00:00", "claude-3-5-sonnet-20241022",
            1000, 1000, 0, 0, 0, "acct-a",
        ),
    )
    cache.commit()
    cache.close()
    journal_before_refusal = _journal_bytes(mod)
    assert mod.cmd_db_rederive(_args(yes=True)) == 2
    refused = json.loads(capsys.readouterr().out)
    assert "plan hash drifted" in refused["conflicts"][0]
    assert _journal_bytes(mod) == journal_before_refusal

    cache = mod.open_cache_db()
    cache.execute(
        "DELETE FROM session_entries WHERE source_path=? AND line_offset=1",
        ("/tmp/claude/projects/repo/session.jsonl",),
    )
    cache.commit()
    assert rederive._cache_fingerprint(cache) == pre.plan.cache_fingerprint
    cache.close()

    assert mod.cmd_db_rederive(_args(yes=True)) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["status"] == "recovered"
    assert recovered["batchId"] == pre.batch_id
    after = mod.read_rederive_journal_prefix()[0]
    assert sum(record == op for record in after) == 1
    stable = _journal_bytes(mod)
    assert mod.cmd_db_rederive(_args(yes=True)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "no-op"
    assert _journal_bytes(mod) == stable


def test_reviewed_weekly_retry_refuses_new_observations_after_decision(
    tmp_path, monkeypatch,
):
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    import _cctally_journal as runtime
    import _cctally_rederive as rederive
    import _lib_journal as journal

    first = _raw_obs(journal)
    stale_payload = dict(first["payload"])
    stale_payload.update({
        "captured_at": "2026-07-25T12:01:00Z",
        "weekly_percent": 5.0,
        "five_hour_percent": 21.0,
    })
    stale = journal.make_obs(
        at="2026-07-25T12:01:00Z", src="record-usage",
        provider="claude", account="acct-a", payload=stale_payload,
    )
    runtime.append_records(
        [first, stale],
        now_utc=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc),
    )
    high_water = runtime.journal_high_water()
    manifest = {
        "schemaVersion": 1,
        "journalHighWater": {
            "segment": high_water[0], "offset": high_water[1],
        },
        "journalPrefixHash": runtime.journal_prefix_hash(high_water),
        "reviewedAt": "2026-07-25T13:00:00Z",
        "reason": "Reviewed stale capture",
        "decisions": [{
            "observationId": stale["id"], "disposition": "hold",
        }],
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(manifest))
    op = rederive._reviewed_weekly_op_from_manifest(path)
    preview = rederive.preview_db_rederive("claude-usage", reviewed_op=op)
    expected_hashes = (
        preview.baseline_plan.plan_hash, preview.plan.plan_hash,
    )
    manifest.update({
        "expectedBaselinePlanHash": expected_hashes[0],
        "expectedDecisionPlanHash": expected_hashes[1],
    })
    path.write_text(json.dumps(manifest))
    op = rederive._reviewed_weekly_op_from_manifest(path)

    def crash(stage):
        if stage == "after-reviewed-decision-op":
            raise RuntimeError("simulated crash after durable op")

    monkeypatch.setattr(rederive, "_REDERIVE_CRASH_HOOK", crash)
    with pytest.raises(RuntimeError, match="durable op"):
        rederive.apply_db_rederive(
            "claude-usage", reviewed_op=op,
            expected_plan_hashes=expected_hashes,
        )
    monkeypatch.setattr(rederive, "_REDERIVE_CRASH_HOOK", None)

    later_payload = dict(first["payload"])
    later_payload.update({
        "captured_at": "2026-07-25T12:02:00Z",
        "weekly_percent": 7.0,
        "five_hour_percent": 22.0,
    })
    later = journal.make_obs(
        at="2026-07-25T12:02:00Z", src="record-usage",
        provider="claude", account="acct-a", payload=later_payload,
    )
    runtime.append_records([later])
    stable = _journal_bytes(mod)
    with pytest.raises(rederive._lib_rederive.RederiveConflict,
                       match="new journal records"):
        rederive.apply_db_rederive(
            "claude-usage", reviewed_op=op,
            expected_plan_hashes=expected_hashes,
        )
    assert _journal_bytes(mod) == stable


def test_reviewed_weekly_apply_excludes_unrelated_baseline_corrections(
    tmp_path, monkeypatch,
):
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    first, _wrong_events = _seed_wrong_journal(mod)
    import _cctally_journal as runtime
    import _cctally_rederive as rederive
    import _lib_journal as journal

    payload = dict(first["payload"])
    payload.update({
        "captured_at": "2026-07-25T12:01:00Z",
        "weekly_percent": 5.0,
    })
    later = journal.make_obs(
        at="2026-07-25T12:01:00Z", src="record-usage",
        provider="claude", account="acct-a", payload=payload,
    )
    runtime.append_records(
        [later], now_utc=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc),
    )
    high_water = runtime.journal_high_water()
    manifest = {
        "schemaVersion": 1,
        "journalHighWater": {
            "segment": high_water[0], "offset": high_water[1],
        },
        "journalPrefixHash": runtime.journal_prefix_hash(high_water),
        "reviewedAt": "2026-07-25T13:00:00Z",
        "reason": "Reviewed later weekly reading",
        "decisions": [{
            "observationId": later["id"], "disposition": "hold",
        }],
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(manifest))
    op = rederive._reviewed_weekly_op_from_manifest(path)
    preview = rederive.preview_db_rederive("claude-usage", reviewed_op=op)
    assert any(action.at < later["at"]
               for action in preview.baseline_plan.actions)
    assert not any(action.at < later["at"] for action in preview.plan.actions)
    assert preview.baseline_plan.plan_hash != preview.plan.plan_hash
    expected_hashes = (
        preview.baseline_plan.plan_hash, preview.plan.plan_hash,
    )
    manifest.update({
        "expectedBaselinePlanHash": expected_hashes[0],
        "expectedDecisionPlanHash": expected_hashes[1],
    })
    path.write_text(json.dumps(manifest))
    op = rederive._reviewed_weekly_op_from_manifest(path)

    result = rederive.apply_db_rederive(
        "claude-usage", reviewed_op=op,
        expected_plan_hashes=expected_hashes,
    )
    assert result.status == "applied"
    records = mod.read_rederive_journal_prefix()[0]
    decision_actions = [
        record for record in records
        if record.get("t") == "correction"
        and record.get("batch") == result.batch_id
    ]
    assert not any(record["at"] < later["at"] for record in decision_actions)


def test_reviewed_weekly_manifest_rejects_bad_ids_and_prefix_drift(
    tmp_path, monkeypatch, capsys,
):
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    import _cctally_journal as runtime
    import _lib_journal as journal

    obs = _raw_obs(journal)
    runtime.append_records(
        [obs], now_utc=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc),
    )
    high_water = runtime.journal_high_water()
    base = {
        "schemaVersion": 1,
        "journalHighWater": {
            "segment": high_water[0], "offset": high_water[1],
        },
        "journalPrefixHash": runtime.journal_prefix_hash(high_water),
        "reviewedAt": "2026-07-25T13:00:00Z",
        "reason": "Reviewed exact capture",
        "decisions": [{"observationId": obs["id"], "disposition": "hold"}],
    }
    path = tmp_path / "invalid-review.json"
    parser = mod.build_parser()
    args = parser.parse_args([
        "db", "rederive", "--family", "claude-usage",
        "--reviewed-weekly-decisions", str(path), "--json",
    ])
    for changed, expected in (
        ({"reason": ""}, "reason"),
        ({"decisions": base["decisions"] * 2}, "duplicate"),
        ({"decisions": [{"observationId": "o:absent", "disposition": "hold"}]},
         "unknown"),
        ({"journalPrefixHash": "sha256:" + "0" * 64}, "PrefixHash"),
    ):
        path.write_text(json.dumps(dict(base, **changed)))
        before = _journal_bytes(mod)
        assert mod.cmd_db_rederive(args) == 2
        response = json.loads(capsys.readouterr().out)
        assert response["status"] == "conflict"
        assert expected in response["conflicts"][0]
        assert _journal_bytes(mod) == before

    path.write_text(json.dumps(base))
    before = _journal_bytes(mod)
    assert mod.cmd_db_rederive(args) == 2
    response = json.loads(capsys.readouterr().out)
    assert "accepted account basis" in response["conflicts"][0]
    assert _journal_bytes(mod) == before

    runtime.append_records(
        [journal.make_op(
            at="2026-07-25T13:01:00Z", src="fixture",
            payload={"kind": "sync_week"},
        )],
        now_utc=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc),
    )
    before = _journal_bytes(mod)
    assert mod.cmd_db_rederive(args) == 2
    response = json.loads(capsys.readouterr().out)
    assert "journalHighWater drifted" in response["conflicts"][0]
    assert _journal_bytes(mod) == before

    path.unlink()
    assert mod.cmd_db_rederive(args) == 3
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "failed"
    assert "No such file" in response["errors"][0]
    assert _journal_bytes(mod) == before


def test_reviewed_weekly_v2_manifest_records_exact_identity_pair(
    tmp_path,
):
    import _cctally_rederive as rederive

    accepted = "o:b8db6f3413ca6dd0"
    replay = "o:f1c3e45eafe02d65"
    manifest = {
        "schemaVersion": 2,
        "journalHighWater": {
            "segment": "observations-2026-08.jsonl", "offset": 1234,
        },
        "journalPrefixHash": "sha256:" + "a" * 64,
        "reviewedAt": "2026-09-19T00:00:00Z",
        "reason": "Reviewed exact August snapshot identity",
        "weeklyAxisDecisions": [],
        "snapshotIdentityDecisions": [{
            "acceptedObservationId": accepted,
            "replayObservationId": replay,
            "disposition": "preserve",
        }],
    }
    path = tmp_path / "review-v2.json"
    path.write_text(json.dumps(manifest))
    reviewed = rederive._reviewed_weekly_op_from_manifest(path)
    assert reviewed["payload"]["schema_version"] == 2
    assert reviewed["payload"]["snapshot_identity_decisions"] == [{
        "acceptedObservationId": accepted,
        "replayObservationId": replay,
        "disposition": "preserve",
    }]

    for decisions in (
        [{"acceptedObservationId": accepted,
          "replayObservationId": accepted,
          "disposition": "preserve"}],
        [
            {"acceptedObservationId": accepted,
             "replayObservationId": replay,
             "disposition": "preserve"},
            {"acceptedObservationId": accepted,
             "replayObservationId": "o:other",
             "disposition": "preserve"},
        ],
        [{"acceptedObservationId": accepted,
          "replayObservationId": replay,
          "disposition": "unexpected"}],
    ):
        path.write_text(json.dumps({
            **manifest, "snapshotIdentityDecisions": decisions,
        }))
        with pytest.raises(rederive._lib_rederive.RederiveConflict,
                           match="malformed, repeated, or ambiguous"):
            rederive._reviewed_weekly_op_from_manifest(path)

    reverse_manifest = {
        **manifest,
        "reviewedAt": "2026-09-19T00:01:00Z",
        "reason": "Restore automatic replay identity",
        "snapshotIdentityDecisions": [{
            "acceptedObservationId": accepted,
            "replayObservationId": replay,
            "disposition": "rederive",
        }],
    }
    path.write_text(json.dumps(reverse_manifest))
    reversed_op = rederive._reviewed_weekly_op_from_manifest(path)
    held, accepted_ids, identity_pairs, ops = rederive._reviewed_weekly_state([
        reviewed, reversed_op,
    ])
    assert held == frozenset()
    assert accepted_ids == frozenset()
    assert identity_pairs == ()
    assert ops == [reviewed, reversed_op]


def test_reviewed_weekly_large_manifest_checks_encoded_line_limit(tmp_path):
    import _cctally_journal as runtime
    import _cctally_rederive as rederive
    import _lib_journal as journal
    import _lib_rederive

    manifest = {
        "schemaVersion": 1,
        "journalHighWater": {
            "segment": "observations-2026-09.jsonl", "offset": 12345,
        },
        "journalPrefixHash": "sha256:" + "a" * 64,
        "reviewedAt": "2026-09-05T05:00:00Z",
        "reason": "Reviewed one stale high replay interval",
        "decisions": [
            {"observationId": f"o:{index:016x}", "disposition": "hold"}
            for index in range(942)
        ],
    }
    path = tmp_path / "large-review.json"
    path.write_text(json.dumps(manifest))
    op = rederive._reviewed_weekly_op_from_manifest(path)
    assert len(journal.encode_line(op)) <= runtime._MAX_LINE_BYTES
    manifest["decisions"].extend(
        {"observationId": f"o:{index:016x}", "disposition": "hold"}
        for index in range(942, 1300)
    )
    path.write_text(json.dumps(manifest))
    with pytest.raises(_lib_rederive.RederiveConflict,
                       match="journal line limit"):
        rederive._reviewed_weekly_op_from_manifest(path)


def test_retained_reviewed_weekly_op_requires_its_exact_preceding_prefix(
    tmp_path, monkeypatch, capsys,
):
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    import _cctally_journal as runtime
    import _lib_journal as journal

    first = _raw_obs(journal)
    later_payload = dict(first["payload"])
    later_payload.update({
        "captured_at": "2026-07-25T12:01:00Z",
        "weekly_percent": 5.0,
    })
    later = journal.make_obs(
        at="2026-07-25T12:01:00Z", src="record-usage",
        provider="claude", account="acct-a", payload=later_payload,
    )
    runtime.append_records(
        [first, later],
        now_utc=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc),
    )
    high_water = runtime.journal_high_water()
    bad = journal.make_op(
        at="2026-07-25T13:00:00Z", src="rederive",
        payload={
            "kind": "claude_weekly_observation_decision",
            "schema_version": 1,
            "journal_high_water": {
                "segment": high_water[0], "offset": high_water[1],
            },
            "journal_prefix_hash": "sha256:" + "0" * 64,
            "reason": "Reviewed a stale low reading",
            "decisions": [{
                "observationId": later["id"], "disposition": "hold",
            }],
        },
    )
    runtime.append_records(
        [bad], now_utc=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc),
    )
    assert mod.cmd_db_rederive(_args()) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "conflict"
    assert "retained reviewed weekly" in response["conflicts"][0]


def test_preview_is_write_free_apply_converges_and_second_apply_is_noop(
    tmp_path, monkeypatch, capsys
):
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    _seed_wrong_journal(mod)
    import _cctally_journal as runtime

    runtime.ALERT_DISPATCHER = lambda alerts: pytest.fail(
        f"rederive dispatched alerts: {alerts}"
    )
    for lock_path in mod.APP_DIR.rglob("*.lock"):
        lock_path.unlink()
    before_tree = _persistent_tree(mod.APP_DIR)

    assert mod.cmd_db_rederive(_args()) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["schemaVersion"] == 1
    assert preview["status"] == "preview"
    assert preview["family"] == "claude-usage"
    assert preview["batchId"]
    assert preview["actionCounts"]["supersede"] >= 2
    assert preview["conflicts"] == []
    assert preview["dataGaps"] == []
    assert preview["rebuild"] is None
    assert preview["noOp"] is False
    assert _persistent_tree(mod.APP_DIR) == before_tree
    before_journal = _journal_bytes(mod)

    assert mod.cmd_db_rederive(_args(yes=True)) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert applied["batchId"] == preview["batchId"]
    assert applied["rebuild"]["linesFolded"] > 0
    assert applied["noOp"] is False
    after_journal = _journal_bytes(mod)
    for name, old_bytes in before_journal.items():
        assert after_journal[name].startswith(old_bytes)

    conn = mod.open_db()
    try:
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC LIMIT 1"
        ).fetchone()[0] == pytest.approx(10.0)
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_effective_events WHERE batch_id = ?",
            (applied["batchId"],),
        ).fetchone()[0] > 0
    finally:
        conn.close()

    stable_journal = _journal_bytes(mod)
    assert mod.cmd_db_rederive(_args(yes=True)) == 0
    noop = json.loads(capsys.readouterr().out)
    assert noop["status"] == "no-op"
    assert noop["noOp"] is True
    assert _journal_bytes(mod) == stable_journal

    independent = tmp_path / "independent.db"
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        target_path=independent,
    )
    assert _logical_dump(mod.DB_PATH) == _logical_dump(independent)


def test_week_reset_add_burst_refuses_preview_and_apply_before_append(
    tmp_path, monkeypatch, capsys,
):
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    import _cctally_journal as runtime
    import _lib_journal as journal

    base_payload = dict(_raw_obs(journal)["payload"])
    for minute, weekly in enumerate((60.0, 10.0, 60.0, 10.0, 60.0, 10.0)):
        captured = f"2026-07-25T12:{minute:02d}:00Z"
        payload = dict(base_payload)
        payload["captured_at"] = captured
        payload["weekly_percent"] = weekly
        runtime.append_record(journal.make_obs(
            at=captured,
            src="record-usage",
            provider="claude",
            account="acct-a",
            payload=payload,
        ))
    monkeypatch.setattr(runtime, "rebuild_stats_index", lambda **kwargs: pytest.fail(
        "rebuild reached after a guarded plan"
    ))
    journal_before = _journal_bytes(mod)

    for yes in (False, True):
        assert mod.cmd_db_rederive(_args(yes=yes)) == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["schemaVersion"] == 1
        assert payload["status"] == "conflict"
        assert payload["planGuard"] == {
            "code": "week-reset-add-burst",
            "limit": 2,
            "violations": [{
                "accountKey": "acct-a",
                "newWeekEndAt": "2026-07-27T00:00:00+00:00",
                "observedPreCreditPct": 60.0,
                "addCount": 3,
            }],
        }
        assert payload["actionCounts"]["add"] >= 3
        assert payload["actionCountsByEventKind"]["week_reset"]["add"] == 3
        assert payload["actionCountsByEventKind"]["five_hour_credit"] == {
            "retain": 0, "supersede": 0, "tombstone": 0, "add": 0,
        }
        assert payload["planHash"].startswith("sha256:")
        assert payload["batchId"].startswith("rederive:claude-usage:")
        assert payload["rebuild"] is None
        assert payload["noOp"] is False
        assert _journal_bytes(mod) == journal_before


def _cutover_manifests(app_dir) -> list:
    """Every current cold-quarantine manifest, oldest first."""
    root = pathlib.Path(app_dir) / "quarantine"
    if not root.is_dir():
        return []
    out = []
    for incident in sorted(root.iterdir()):
        path = incident / "manifest.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text())
        if payload.get("cutoverProtocol") == "cold-quarantine-then-replace-v2":
            out.append(payload)
    return out


def _fail_in_place_pre_commit(monkeypatch):
    """Force the physical fallback while leaving the destination READABLE.

    #496 S3 publishes a readable destination in place, and an in-place publish
    never preserves, so the preservation manifest under test is only written by
    the physical fallback. A structural failure raised in the `PRE_COMMIT`
    phase is the one way to reach that fallback without also making the
    destination unreadable, which the apply's own rebuild would then report
    differently.
    """
    import sqlite3

    import _cctally_journal as jr
    import _lib_stats_publish as sp

    def stub(conn, scratch, **kwargs):
        exc = sqlite3.DatabaseError("database disk image is malformed")
        setattr(exc, "_cctally_publication_phase", sp.PRE_COMMIT)
        raise exc

    monkeypatch.setattr(jr, "_publish_generation_in_place", stub)


def test_rederive_apply_incident_records_the_rederive_apply_trigger(
    tmp_path, monkeypatch, capsys
):
    """#496 S1 F3, driven through the real `db rederive --yes` entry point.

    Asserting on the manifest rather than on the call expression is the point:
    a context that never reaches preservation would leave the incident
    unattributed exactly as it was before this work.
    """
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    _seed_wrong_journal(mod)
    capsys.readouterr()

    # The fixture rebuild above already preserved one family under the
    # test-only identity, so the assertion is about the LAST incident.
    before = [m["trigger"] for m in _cutover_manifests(mod.APP_DIR)]
    db = pathlib.Path(mod.DB_PATH)
    with db.open("r+b") as handle:
        handle.seek(18)
        handle.write(b"\xff\xff")

    assert mod.cmd_db_rederive(_args(yes=True)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"

    manifests = _cutover_manifests(mod.APP_DIR)
    assert len(manifests) == len(before) + 1, (
        "the apply must have preserved the family it replaced"
    )
    assert manifests[-1]["trigger"] == "rederive-apply"
    assert manifests[-1]["schemaVersion"] == 2


def test_command_path_closes_family_and_preserves_provider_owned_state(
    tmp_path, monkeypatch, capsys
):
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    import _cctally_journal as runtime
    import _lib_journal as journal

    cache = mod.open_cache_db()
    cache.execute(
        "INSERT INTO codex_source_roots "
        "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?)",
        ("root-a", "/codex/root-a", AT, AT),
    )
    cache.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, limit_id, limit_name, "
        " window_minutes, used_percent, resets_at_utc, plan_type, account_key) "
        "VALUES ('codex',?,?,?,?,?,'limit-primary','native-primary','Primary',"
        "300,25.0,'2026-07-25T15:00:00Z','pro','unattributed')",
        ("root-a", "/codex/root-a/rollout.jsonl", 10, AT, "primary"),
    )
    cache.execute(
        "UPDATE session_entries SET timestamp_utc=?",
        ("2099-01-01T11:00:00+00:00",),
    )
    cache.commit()

    future_at = "2099-01-01T12:00:00Z"
    obs = journal.make_obs(
        at=future_at,
        src="record-usage",
        provider="claude",
        account="acct-a",
        payload={
            "captured_at": future_at,
            "source": "statusline",
            "weekly_percent": 10.0,
            "resets_at": int(dt.datetime(
                2099, 1, 5, 0, 0, tzinfo=dt.timezone.utc
            ).timestamp()),
            "five_hour_percent": 10.0,
            "five_hour_resets_at": "2099-01-01T15:00:00Z",
        },
    )
    desired = mod.plan_claude_usage_rederive(
        [obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1),
    )
    cache.close()
    desired_snapshot = next(
        action for action in desired.actions
        if (action.payload or {}).get("kind") == "snapshot_accept"
    )
    wrong_snapshot_payload = dict(desired_snapshot.payload)
    wrong_snapshot_payload.pop("kind")

    account_observe = journal.make_account_observe(
        at="2026-07-25T11:55:00Z",
        account_key="acct-a",
        provider="claude",
        natural_id="uuid-a",
        email="a@example.test",
    )
    account_label = journal.make_account_label(
        at="2026-07-25T11:56:00Z",
        account_key="acct-a",
        provider="claude",
        label="Primary",
    )
    stale_block_key = 987654
    stale_events = [
        journal.make_evt(
            kind="snapshot_accept",
            id="sa:obsolete-identity",
            at=desired_snapshot.at,
            payload=wrong_snapshot_payload,
        ),
        journal.make_evt(
            kind="weekly_credit_effects",
            id="wce:obsolete",
            at=AT,
            payload={"suppression": []},
        ),
        journal.make_evt(
            kind="week_reset",
            id="wr:obsolete",
            at=AT,
            payload={
                "detected_at_utc": AT,
                "old_week_end_at": "2026-07-26T00:00:00Z",
                "new_week_end_at": "2026-07-27T00:00:00Z",
                "effective_reset_at_utc": AT,
                "observed_pre_credit_pct": 60.0,
                "account_key": "acct-a",
                "suppression": [],
            },
        ),
        journal.make_evt(
            kind="five_hour_credit",
            id="fhc:obsolete",
            at=AT,
            payload={
                "detected_at_utc": AT,
                "five_hour_window_key": stale_block_key,
                "prior_percent": 80.0,
                "post_percent": 10.0,
                "effective_reset_at_utc": AT,
                "account_key": "acct-a",
                "suppression": [],
            },
        ),
        journal.make_evt(
            kind="five_hour_block_close",
            id="fhbc:obsolete",
            at=AT,
            payload={
                "five_hour_window_key": stale_block_key,
                "five_hour_resets_at": "2026-07-25T15:00:00Z",
                "block_start_at": "2026-07-25T10:00:00Z",
                "first_observed_at_utc": "2026-07-25T10:00:00Z",
                "last_observed_at_utc": AT,
                "final_five_hour_percent": 80.0,
                "created_at_utc": "2026-07-25T10:00:00Z",
                "last_updated_at_utc": AT,
                "is_closed": 1,
                "total_cost_usd": 1.0,
                "account_key": "acct-a",
                "_models": [{
                    "five_hour_window_key": stale_block_key,
                    "model": "claude-opus-4",
                    "cost_usd": 1.0,
                    "entry_count": 1,
                    "account_key": "acct-a",
                }],
                "_projects": [{
                    "five_hour_window_key": stale_block_key,
                    "project_path": "/repo/obsolete",
                    "cost_usd": 1.0,
                    "entry_count": 1,
                    "account_key": "acct-a",
                }],
            },
        ),
        journal.make_evt(
            kind="budget",
            id="bm:claude-obsolete",
            at=AT,
            payload={
                "vendor": "claude",
                "period_start_at": "2026-07-01T00:00:00Z",
                "period": "monthly",
                "threshold": 90,
                "budget_usd": 100.0,
                "spent_usd": 90.0,
                "consumption_pct": 90.0,
                "crossed_at_utc": AT,
                "account_key": "*",
            },
        ),
        journal.make_evt(
            kind="projected",
            id="pjm:claude-obsolete",
            at=AT,
            payload={
                "week_start_at": "2026-07-20T00:00:00Z",
                "period": "weekly",
                "metric": "weekly_pct",
                "threshold": 90,
                "projected_value": 95.0,
                "denominator": 100.0,
                "crossed_at_utc": AT,
                "account_key": "acct-a",
            },
        ),
        journal.make_evt(
            kind="project_budget",
            id="pbm:claude-obsolete",
            at=AT,
            payload={
                "week_start_at": "2026-07-20T00:00:00Z",
                "project_key": "/repo/obsolete",
                "threshold": 90,
                "budget_usd": 10.0,
                "spent_usd": 9.0,
                "consumption_pct": 90.0,
                "crossed_at_utc": AT,
                "account_key": "*",
            },
        ),
    ]
    retained_codex = [
        journal.make_evt(
            kind="budget",
            id="bm:codex-keep",
            at=AT,
            payload={
                "vendor": "codex",
                "period_start_at": "2026-07-01T00:00:00Z",
                "period": "monthly",
                "threshold": 90,
                "budget_usd": 100.0,
                "spent_usd": 90.0,
                "consumption_pct": 90.0,
                "crossed_at_utc": AT,
                "account_key": "*",
            },
        ),
        journal.make_evt(
            kind="projected",
            id="pjm:codex-keep",
            at=AT,
            payload={
                "week_start_at": "2026-07-01T00:00:00Z",
                "period": "monthly",
                "metric": "codex_budget_usd",
                "threshold": 90,
                "projected_value": 95.0,
                "denominator": 100.0,
                "crossed_at_utc": AT,
                "account_key": "*",
            },
        ),
        journal.make_evt(
            kind="quota_alert_arming",
            id="qaa:codex-keep",
            at=AT,
            payload={
                "source": "codex",
                "source_root_key": "root-a",
                "logical_limit_key": "limit-primary",
                "observed_slot": "primary",
                "window_minutes": 300,
                "rule_fingerprint": "rules-v1",
                "activated_at_utc": AT,
                "account_key": "unattributed",
            },
        ),
    ]
    fixed = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
    for record in [
        account_observe,
        account_label,
        obs,
        *stale_events,
        *retained_codex,
    ]:
        runtime.append_record(record, now_utc=fixed)
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        update_quota_cache=False,
    )
    hwm7 = mod.APP_DIR / "hwm-7d"
    hwm5 = mod.APP_DIR / "hwm-5h"
    hwm7.write_bytes(b"sentinel-week 77\n")
    hwm5.write_bytes(b"sentinel-block 44\n")
    hwm_before = (hwm7.read_bytes(), hwm5.read_bytes())

    preview = mod.preview_db_rederive("claude-usage")
    dispositions = {
        (action.disposition, action.event_id)
        for action in preview.plan.actions
    }
    for event_id in (
        "sa:obsolete-identity",
        "wce:obsolete",
        "wr:obsolete",
        "fhc:obsolete",
        "fhbc:obsolete",
        "bm:claude-obsolete",
        "pjm:claude-obsolete",
        "pbm:claude-obsolete",
    ):
        assert ("tombstone", event_id) in dispositions
    assert any(
        action.disposition == "add"
        and (action.payload or {}).get("kind") == "snapshot_accept"
        for action in preview.plan.actions
    )

    assert mod.cmd_db_rederive(_args(yes=True)) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    # `hwm-7d` is still the sentinel: no rederive path derives or writes the
    # weekly projection, and the SQL weekly high-water clamp re-establishes it
    # on the next status-line tick.
    assert hwm7.read_bytes() == hwm_before[0]
    # `hwm-5h` is NOT, and that changed in #769 S11 (#824). An apply publishes
    # a LIVE stats index, and a live publication now rematerializes the
    # five-hour projection from the index it just published — because a
    # weekly-clamped tick's five-hour evidence lives only in a
    # `weekly_observation_held` row, and neither replay path reruns the live
    # pipeline that would otherwise write the file. Asserted against the index
    # rather than against a literal, so the assertion says what the rule is.
    conn = mod.open_db()
    try:
        window_key = conn.execute(
            "SELECT five_hour_window_key FROM weekly_usage_snapshots "
            "WHERE five_hour_window_key IS NOT NULL "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    # The percent is a literal, not a recomputation. An oracle that re-runs the
    # pass's own MAX agrees with that query whether or not it is correct, which
    # is exactly how an unfloored MAX survived to review (#769 S11 F1/F5). Only
    # the window key is read back, because it is a canonicalized derivation of
    # the seeded reset instant rather than a value this scenario states.
    assert hwm5.read_text() == f"{int(window_key)} 10.0\n"
    assert hwm5.read_bytes() != hwm_before[1], (
        "the sentinel survived, so the rematerialization never ran")
    conn = mod.open_db()
    try:
        assert tuple(conn.execute(
            "SELECT label, label_source FROM accounts WHERE account_key='acct-a'"
        ).fetchone()) == ("Primary", "user")
        assert conn.execute(
            "SELECT COUNT(*) FROM five_hour_blocks "
            "WHERE five_hour_window_key=?",
            (stale_block_key,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM five_hour_block_models "
            "WHERE five_hour_window_key=?",
            (stale_block_key,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM five_hour_block_projects "
            "WHERE five_hour_window_key=?",
            (stale_block_key,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM five_hour_blocks WHERE is_closed=0"
        ).fetchone()[0] >= 1
        assert conn.execute(
            "SELECT COUNT(*) FROM budget_milestones WHERE vendor='codex'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM projected_milestones "
            "WHERE metric='codex_budget_usd'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_alert_arming"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_blocks"
        ).fetchone()[0] >= 1
    finally:
        conn.close()

    independent = tmp_path / "family-independent.db"
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        target_path=independent,
        update_quota_cache=False,
    )
    assert _logical_dump(mod.DB_PATH) == _logical_dump(independent)


def test_apply_recovers_incomplete_batch_and_completed_batch_without_duplicate(
    tmp_path, monkeypatch, capsys
):
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    _seed_wrong_journal(mod)
    import _cctally_journal as runtime
    import _lib_journal as journal

    assert mod.cmd_db_rederive(_args()) == 0
    preview = json.loads(capsys.readouterr().out)
    plan = mod.preview_db_rederive("claude-usage")
    records = journal.make_correction_batch(
        batch_id=preview["batchId"],
        family="claude-usage",
        at=plan.generated_at,
        actions=plan.plan.to_correction_actions(),
    )
    fixed = dt.datetime(2026, 7, 25, 12, 1, tzinfo=dt.timezone.utc)
    for record in records[:-1]:
        runtime.append_record(record, now_utc=fixed)

    assert mod.cmd_db_rederive(_args(yes=True)) == 0
    recovered_incomplete = json.loads(capsys.readouterr().out)
    assert recovered_incomplete["status"] == "recovered"
    assert recovered_incomplete["batchId"] == preview["batchId"]
    records_now = mod.read_rederive_journal_prefix()[0]
    selection = journal.resolve_effective_events(records_now)
    assert preview["batchId"] in selection.completed_batches

    # A committed batch with an old index is recovery-only: no second append.
    mod2 = _isolated(tmp_path / "completed", monkeypatch)
    _seed_cache(mod2)
    _seed_wrong_journal(mod2)
    assert mod2.cmd_db_rederive(_args()) == 0
    preview2 = json.loads(capsys.readouterr().out)
    plan2 = mod2.preview_db_rederive("claude-usage")
    records2 = journal.make_correction_batch(
        batch_id=preview2["batchId"],
        family="claude-usage",
        at=plan2.generated_at,
        actions=plan2.plan.to_correction_actions(),
    )
    runtime.append_records(records2, now_utc=fixed)
    before_recovery = _journal_bytes(mod2)

    assert mod2.cmd_db_rederive(_args()) == 0
    recovery_preview = json.loads(capsys.readouterr().out)
    assert recovery_preview["status"] == "preview"
    assert recovery_preview["batchId"] == preview2["batchId"]
    assert recovery_preview["noOp"] is False
    assert _journal_bytes(mod2) == before_recovery

    assert mod2.cmd_db_rederive(_args(yes=True)) == 0
    recovered_complete = json.loads(capsys.readouterr().out)
    assert recovered_complete["status"] == "recovered"
    assert recovered_complete["batchId"] == preview2["batchId"]
    assert _journal_bytes(mod2) == before_recovery


def test_missing_source_and_protocol_conflict_are_exit_2_json(
    tmp_path, monkeypatch, capsys
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _lib_journal as journal

    runtime.append_record(
        _raw_obs(journal),
        now_utc=dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert mod.cmd_db_rederive(_args()) == 2
    missing = json.loads(capsys.readouterr().out)
    assert missing["status"] == "missing-source"
    assert missing["journalHighWater"]["offset"] > 0
    assert missing["dataGaps"]
    assert missing["conflicts"] == []

    _seed_cache(mod)
    # #374: divergent same-revision EVENTS no longer make the selector raise —
    # `db rederive` is now their remedy, not a refusal. A STRUCTURAL
    # correction-batch violation is what still lands in `conflicts` at exit 2.
    base = journal.make_evt(
        kind="snapshot_accept", id="sa:conflict", at=AT, payload={"value": 1}
    )
    batch = journal.make_correction_batch(
        batch_id="batch:tampered-cmd",
        family="claude-usage",
        at=AT,
        actions=[
            {
                "action": "replace",
                "id": "sa:conflict",
                "rev": 1,
                "at": AT,
                "payload": {"kind": "snapshot_accept", "value": 2},
            }
        ],
    )
    batch[1]["payload"]["value"] = 999  # tamper -> manifest hash mismatch
    fixed = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
    for record in [base, *batch]:
        runtime.append_record(record, now_utc=fixed)

    assert mod.cmd_db_rederive(_args()) == 2
    conflict = json.loads(capsys.readouterr().out)
    assert conflict["status"] == "conflict"
    assert conflict["journalHighWater"]["offset"] > missing["journalHighWater"]["offset"]
    assert conflict["conflicts"]
    assert conflict["dataGaps"] == []


def test_unsupported_family_is_handler_owned_exit_2_json(
    tmp_path, monkeypatch, capsys
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _lib_journal as journal

    runtime.append_record(
        _raw_obs(journal),
        now_utc=dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc),
    )
    args = argparse.Namespace(family="future-family", yes=False, json=True)
    assert mod.cmd_db_rederive(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schemaVersion"] == 1
    assert payload["status"] == "conflict"
    assert payload["family"] == "future-family"
    assert payload["journalHighWater"]["offset"] > 0
    assert payload["conflicts"] == [
        "unsupported rederive family: future-family"
    ]


def test_completed_batch_survives_rebuild_failure_and_retry_recovers(
    tmp_path, monkeypatch, capsys
):
    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    _seed_wrong_journal(mod)
    import _cctally_journal as runtime

    real_rebuild = runtime.rebuild_stats_index
    monkeypatch.setattr(
        runtime,
        "rebuild_stats_index",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("injected rebuild crash")),
    )
    assert mod.cmd_db_rederive(_args(yes=True)) == 3
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "failed"
    assert failed["batchId"]
    assert failed["actionCounts"]["supersede"] >= 2
    assert failed["conflicts"] == []
    assert "injected rebuild crash" in failed["errors"][0]
    failed_journal = _journal_bytes(mod)

    monkeypatch.setattr(runtime, "rebuild_stats_index", real_rebuild)
    assert mod.cmd_db_rederive(_args(yes=True)) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["status"] == "recovered"
    assert _journal_bytes(mod) == failed_journal


def test_atomic_group_revalidation_and_pinned_rebuild_leave_later_input_unread(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _lib_journal as journal

    fixed = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
    base = journal.make_evt(
        kind="snapshot_accept",
        id="sa:base",
        at=AT,
        payload={
            "captured_at_utc": AT,
            "week_start_date": "2026-07-21",
            "week_start_at": "2026-07-21T00:00:00Z",
            "weekly_percent": 10.0,
            "five_hour_percent": None,
            "five_hour_resets_at": None,
            "source": "fixture",
            "account_key": "acct-a",
        },
    )
    runtime.append_record(base, now_utc=fixed)
    planned_high_water = runtime.journal_high_water()
    later = _raw_obs(journal)
    runtime.append_record(later, now_utc=fixed)

    batch = journal.make_correction_batch(
        batch_id="batch:must-not-append",
        family="claude-usage",
        at=AT,
        actions=[],
    )
    with pytest.raises(runtime.JournalError, match="high-water changed"):
        runtime.append_records(
            batch,
            now_utc=fixed,
            expected_high_water=planned_high_water,
        )

    target = tmp_path / "pinned.db"
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        target_path=target,
        high_water=planned_high_water,
        update_quota_cache=False,
    )
    import sqlite3

    conn = sqlite3.connect(target)
    try:
        assert conn.execute(
            "SELECT segment, offset FROM journal_cursor WHERE id=1"
        ).fetchone() == planned_high_water
    finally:
        conn.close()


@pytest.mark.parametrize(
    "stage",
    (
        "after-batch-line-1",
        "after-batch-commit",
        "before-rebuild-swap",
    ),
)
def test_real_sigkill_recovery_converges_without_duplicate_batch(
    tmp_path, monkeypatch, stage
):
    case = tmp_path / stage
    mod = _isolated(case, monkeypatch)
    _seed_cache(mod)
    _seed_wrong_journal(mod)
    import _cctally_journal as runtime
    import _lib_journal as journal

    env = os.environ.copy()
    env.update(
        {
            "CCTALLY_DATA_DIR": str(mod.APP_DIR),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "CCTALLY_REDERIVE_TEST_MODE": "1",
            "CCTALLY_REDERIVE_TEST_CRASH_STAGE": stage,
            "NO_COLOR": "1",
            "TZ": "Etc/UTC",
        }
    )
    command = [
        str(pathlib.Path(__file__).parents[1] / "bin" / "cctally"),
        "db",
        "rederive",
        "--family",
        "claude-usage",
        "--yes",
        "--json",
    ]
    preview_env = dict(env)
    preview_env.pop("CCTALLY_REDERIVE_TEST_CRASH_STAGE")
    preview_command = [arg for arg in command if arg != "--yes"]
    # ONE budget for all three child runs, not one each. Thirty plus thirty
    # plus sixty is the whole 120-second pytest cap, so a rederive that hung
    # spent the cap across them and pytest-timeout killed the worker with a
    # generic message instead of a `TimeoutExpired` naming the command. These
    # are hang detectors rather than expected durations — each run finishes in
    # seconds — so sharing one budget makes each of them MORE generous than it
    # was while bounding what the three can spend together.
    deadline = time.monotonic() + _CHILD_RUNS_BUDGET_S
    preview_run = subprocess.run(
        preview_command,
        env=preview_env,
        capture_output=True,
        text=True,
        timeout=remaining(deadline),
        check=False,
    )
    assert preview_run.returncode == 0, preview_run.stderr
    preview = json.loads(preview_run.stdout)
    assert preview["status"] == "preview"
    assert preview["batchId"] is not None
    original_journal = _journal_bytes(mod)
    original_stats = mod.DB_PATH.read_bytes()
    killed = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=remaining(deadline),
        check=False,
    )
    assert killed.returncode == -signal.SIGKILL
    assert mod.DB_PATH.read_bytes() == original_stats
    after_kill = mod.read_rederive_journal_prefix()[0]
    selection_after_kill = journal.resolve_effective_events(after_kill)
    if stage == "after-batch-line-1":
        assert preview["batchId"] not in selection_after_kill.completed_batches
    else:
        assert preview["batchId"] in selection_after_kill.completed_batches

    retry_env = dict(env)
    retry_env.pop("CCTALLY_REDERIVE_TEST_CRASH_STAGE")
    recovered = subprocess.run(
        command,
        env=retry_env,
        capture_output=True,
        text=True,
        timeout=remaining(deadline),
        check=False,
    )
    assert recovered.returncode == 0, recovered.stderr
    payload = json.loads(recovered.stdout)
    assert payload["status"] == "recovered"
    assert payload["batchId"] == preview["batchId"]
    final_records = mod.read_rederive_journal_prefix()[0]
    final_selection = journal.resolve_effective_events(final_records)
    assert preview["batchId"] in final_selection.completed_batches
    assert sum(
        record.get("t") == "correction_batch"
        and record.get("phase") == "commit"
        and record.get("id") == preview["batchId"]
        for record in final_records
    ) == 1
    final_journal = _journal_bytes(mod)
    for name, data in original_journal.items():
        assert final_journal[name].startswith(data)
    independent = case / "independent.db"
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        target_path=independent,
        update_quota_cache=False,
    )
    assert _logical_dump(mod.DB_PATH) == _logical_dump(independent)


# ==========================================================================
# #374 — `db rederive` is the resolution path for quarantined conflicts
# ==========================================================================

def _seed_conflicted_journal(mod):
    """A correctly-derived journal PLUS one duplicated-then-drifted event line —
    the shape a crash between `append_record` and COMMIT leaves behind."""
    import _cctally_journal as runtime
    import _lib_journal as journal

    obs = _raw_obs(journal)
    cache = mod.open_cache_db()
    desired = mod.plan_claude_usage_rederive(
        [obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1),
    )
    cache.close()
    fixed = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
    runtime.append_record(obs, now_utc=fixed)
    conflicted_ids = []
    for action in desired.actions:
        payload = dict(action.payload or {})
        kind = payload.pop("kind")
        correct = journal.make_evt(
            kind=kind, id=action.event_id, at=action.at, payload=dict(payload))
        runtime.append_record(correct, now_utc=fixed)
        if kind == "snapshot_accept":
            # The retry's line: same id, same revision, drifted content.
            drifted = dict(payload)
            drifted["weekly_percent"] = 99.0
            runtime.append_record(
                journal.make_evt(kind=kind, id=action.event_id, at=action.at,
                                 payload=drifted),
                now_utc=fixed,
            )
            conflicted_ids.append(action.event_id)
    runtime.rebuild_stats_index(context=runtime.RebuildContext(trigger="test-fixture"))
    return conflicted_ids


def test_rederive_clears_quarantined_same_revision_conflicts(
    tmp_path, monkeypatch, capsys
):
    """Acceptance 3 end to end: the provisional winner already equals the
    desired re-derivation, so without the forced revision advance the planner
    would emit no action at all and the rev-0 group would live forever."""
    import _lib_journal as journal

    mod = _isolated(tmp_path, monkeypatch)
    _seed_cache(mod)
    conflicted_ids = _seed_conflicted_journal(mod)
    assert conflicted_ids

    before = journal.resolve_effective_events(
        mod.read_rederive_journal_prefix()[0])
    assert {c.event_id for c in before.conflicts} == set(conflicted_ids)

    assert mod.cmd_db_rederive(_args()) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "preview"
    assert preview["conflicts"] == [], "the legacy key keeps its meaning"
    assert {c["eventId"] for c in preview["journalConflicts"]} == set(conflicted_ids)
    assert preview["actionCounts"]["supersede"] >= len(conflicted_ids)

    # Repeated previews are byte-identical in action, plan hash and batch id.
    assert mod.cmd_db_rederive(_args()) == 0
    again = json.loads(capsys.readouterr().out)
    assert (again["planHash"], again["batchId"], again["actionCounts"]) == (
        preview["planHash"], preview["batchId"], preview["actionCounts"])

    assert mod.cmd_db_rederive(_args(yes=True)) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"

    after = journal.resolve_effective_events(
        mod.read_rederive_journal_prefix()[0])
    assert after.conflicts == (), (
        "after the batch the selector must report ZERO winning-revision "
        "conflicts")
    for event_id in conflicted_ids:
        assert after.by_id[event_id].rev == 1

    # A second run is a no-op.
    assert mod.cmd_db_rederive(_args()) == 0
    noop = json.loads(capsys.readouterr().out)
    assert noop["status"] == "no-op"
    assert noop["journalConflicts"] == []


# ==========================================================================
# #496 S5 — the streaming prefix conversion
#
# `read_rederive_journal_prefix` materialized every raw line and then every
# decoded record, and `_protocol_prefix_evidence` then walked the result again,
# making `journal_prefix_hash` re-read the whole prefix once per resolution op.
# The conversion folds all of that into one pinned streaming pass. Retention is
# deliberately unchanged: the planner needs every decoded record.
# ==========================================================================

#: `db rederive --family claude-usage --json` over the two S5 fixtures. The
#: clean plan is exercised through a fresh CLI process so the in-process test
#: loader cannot change its module bindings or payload hashes. Keep these as
#: literals: a value the code under test produced for both sides of an equality
#: proves only that it agrees with itself.
#:
#: #769 S11 (#824) moved `planHash` and `batchId`, and nothing else in either
#: baseline. `weekly_observation_held` joined `_USAGE_SNAPSHOT_COLUMNS`, so it
#: joins every `snapshot_accept` evt payload the planner derives, and the plan
#: hash is taken over those payloads. The move is the schema change being
#: visible where it should be; a MOVE WITHOUT a schema change would be the
#: regression this baseline exists to catch.
S5_CLEAN_BASELINE = {
    "actionCounts": {"add": 6, "retain": 0, "supersede": 0, "tombstone": 0},
    "batchId": (
        "rederive:claude-usage:4013432f6fe090d1f5d70689de0e51cad163d60006947"
        "c68edc846d6b1a32117"
    ),
    "conflicts": [],
    "dataGaps": [],
    "errors": [],
    "family": "claude-usage",
    "journalConflicts": [],
    "journalHighWater": {
        "offset": 248,
        "segment": "observations-2026-08.jsonl",
    },
    "noOp": False,
    "planHash": (
        "sha256:c9e5be76a1f8fd8f3017b245be78863dfca5ba8cf58a4b9ce73c348026bd2"
        "365"
    ),
    "preservedEventCount": 0,
    "rebuild": None,
    "schemaVersion": 1,
    "status": "preview",
}

S5_TAINTED_BASELINE = {
    "actionCounts": {"add": 0, "retain": 0, "supersede": 0, "tombstone": 0},
    "batchId": None,
    "conflicts": [
        "journal contains tainted correction batch(es): "
        "batch:unack:commit_without_begin, "
        "batch:ack-one:commit_without_begin, "
        "batch:ack-two:commit_without_begin"
    ],
    "dataGaps": [],
    "errors": [],
    "family": "claude-usage",
    "journalConflicts": [],
    "journalHighWater": {
        "offset": 1116,
        "segment": "observations-2026-09.jsonl",
    },
    "noOp": False,
    "planHash": None,
    "preservedEventCount": 0,
    "rebuild": None,
    "schemaVersion": 1,
    "status": "conflict",
}


def _s5_module(tmp_path, monkeypatch, builder):
    """The isolated CLI module over one S5 fixture, plus an open recorder."""
    import journal_fixture_496_s5 as S5

    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_core
    import _cctally_journal as jr

    built = builder(_cctally_core.APP_DIR)
    real = jr._open_segment_for_read
    opened: list[str] = []

    def record(seg_path):
        opened.append(pathlib.Path(seg_path).name)
        return real(seg_path)

    monkeypatch.setattr(jr, "_open_segment_for_read", record)
    return mod, opened, built, S5


def test_s5_rederive_plan_is_byte_identical_to_the_frozen_cli_baseline(
    tmp_path, monkeypatch, capsys
):
    import journal_fixture_496_s5 as S5

    mod, _opened, _built, _S5 = _s5_module(
        tmp_path, monkeypatch, S5.build_clean
    )
    capsys.readouterr()
    env = os.environ.copy()
    env.update({
        "CCTALLY_DATA_DIR": str(mod.APP_DIR),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "NO_COLOR": "1",
        "TZ": "Etc/UTC",
    })
    run = subprocess.run(
        [
            str(pathlib.Path(__file__).parents[1] / "bin" / "cctally"),
            "db", "rederive", "--family", "claude-usage", "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert payload.pop("planGuard") is None
    by_kind = payload.pop("actionCountsByEventKind")
    assert sum(counts["add"] for counts in by_kind.values()) == 6
    assert by_kind["five_hour_credit"] == {
        "retain": 0, "supersede": 0, "tombstone": 0, "add": 0,
    }
    assert payload == S5_CLEAN_BASELINE


def test_s5_rederive_refuses_the_tainted_prefix_identically(
    tmp_path, monkeypatch, capsys
):
    import journal_fixture_496_s5 as S5

    mod, _opened, _built, _S5 = _s5_module(
        tmp_path, monkeypatch, S5.build_tainted)
    assert mod.cmd_db_rederive(_args()) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload.pop("planGuard") is None
    by_kind = payload.pop("actionCountsByEventKind")
    assert all(all(value == 0 for value in counts.values())
               for counts in by_kind.values())
    assert payload == S5_TAINTED_BASELINE


def test_a_rederive_preview_opens_each_segment_exactly_once(
    tmp_path, monkeypatch, capsys
):
    """Before the conversion each of the fixture's two resolution ops made
    `journal_prefix_hash` re-read the prefix from byte zero, so this preview
    opened the three segments seven times rather than three."""
    import journal_fixture_496_s5 as S5

    mod, opened, _built, _S5 = _s5_module(
        tmp_path, monkeypatch, S5.build_tainted)
    opened.clear()
    assert mod.cmd_db_rederive(_args()) == 2
    capsys.readouterr()
    assert opened == list(S5.SEGMENTS), (
        "a rederive preview must open each segment through the seam exactly "
        "once, in canonical order"
    )


def test_rederive_keeps_every_decoded_record(tmp_path, monkeypatch):
    """The rebuild drops all but the decision records and substitutes `None`
    placeholders. Rederive must NOT: its planner reads the observations for
    cache validation and desired-event derivation, and walks the list in
    parallel with `record_ends`."""
    import journal_fixture_496_s5 as S5

    mod, _opened, built, _S5 = _s5_module(
        tmp_path, monkeypatch, S5.build_tainted)
    records, high_water, record_ends = mod.read_rederive_journal_prefix()[:3]
    assert high_water == built["high_water"]
    assert len(records) == len(built["records"])
    assert all(isinstance(record, dict) for record in records)
    assert [record["id"] for record in records] == [
        record["id"] for record in built["records"]
    ]
    assert len(record_ends) == len(records)
    # The last record ends at the pinned high-water, which is what makes
    # `record_ends` usable as a commit coordinate.
    assert record_ends[-1] == built["high_water"]


class _StubRebuildResult:
    """Just enough of a `RebuildResult` for `_rebuild_dict` to render."""

    segments_read = 0
    lines_folded = 0
    malformed = 0
    duration_s = 0.0
    rows_by_table: dict = {}


def test_a_rederive_preview_that_produces_a_plan_opens_each_segment_once(
    tmp_path, monkeypatch, capsys
):
    """The traversal test above drives the TAINTED fixture, where
    `plan_claude_usage` returns 2 straight after the read, so it never reaches
    the path that produces a plan. This one does."""
    import journal_fixture_496_s5 as S5

    mod, opened, built, _S5 = _s5_module(tmp_path, monkeypatch, S5.build_clean)
    opened.clear()
    assert mod.cmd_db_rederive(_args()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "preview"
    assert payload["actionCounts"]["add"] == 6, payload
    assert opened == list(built["segments"]), opened


def test_the_whole_rederive_apply_takes_one_prefix_traversal(
    tmp_path, monkeypatch, capsys
):
    """`apply_db_rederive` takes its OWN selection snapshot, which no preview
    test reaches. Counting the whole command is what the spec's criterion 6
    asks for, so this drives `--yes` end to end and counts every open.

    The rebuild is stubbed: it pins its own high-water and reads the prefix
    again by design, and leaving it in would attribute its opens to this
    command's prefix handling."""
    import journal_fixture_496_s5 as S5

    mod, opened, built, _S5 = _s5_module(tmp_path, monkeypatch, S5.build_clean)
    import _cctally_journal as jr

    rebuilt = []

    def _stub_rebuild(*, context, high_water, update_quota_cache,
                      before_swap=None):
        rebuilt.append(high_water)
        return _StubRebuildResult()

    monkeypatch.setattr(jr, "rebuild_stats_index", _stub_rebuild)
    opened.clear()
    assert mod.cmd_db_rederive(_args(yes=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "applied", payload
    assert len(rebuilt) == 1, rebuilt
    # One snapshot: the apply path's own. The correction append writes through
    # the leaf lock's read-write handle, which is deliberately not this seam.
    assert opened == list(built["segments"]), opened


# ==========================================================================
# #769 S11 (#824) — `db rebuild` and `db rederive` must converge on a held
# tick.
#
# The two replays are structurally different. `db rebuild` is apply-only and
# reconstructs open blocks from snapshot history; `db rederive` reruns the raw
# pipeline in a scratch epoch and compares what it derives against what the
# journal holds. A five-hour effect that existed only as a block write and
# never as a journalled snapshot decision would survive one and vanish in the
# other, and the S8 decision record requires anything the snapshot decision
# depends on to be reachable by both. Persisting the held tick as an ordinary
# `snapshot_accept` decision is what satisfies that, and these two tests are
# what say so.
# ==========================================================================

_HELD_WEEK_RESETS_AT = int(
    dt.datetime(2026, 7, 27, 0, 0, tzinfo=dt.timezone.utc).timestamp())
_HELD_5H_RESETS_AT = "2026-07-25T15:00:00+00:00"


def _held_obs(lib, *, at, weekly_percent, five_hour_percent):
    return lib.make_obs(
        at=at, src="record-usage", provider="claude", account="acct-a",
        payload={
            "captured_at": at,
            "source": "statusline",
            "weekly_percent": weekly_percent,
            "resets_at": _HELD_WEEK_RESETS_AT,
            "five_hour_percent": five_hour_percent,
            "five_hour_resets_at": _HELD_5H_RESETS_AT,
        },
    )


def _drive_held_sequence(mod):
    """t0 records a genuine weekly 63 with five-hour 20; t1 arrives in the same
    physical windows carrying a raw weekly of 60, which clamps, and a genuine
    five-hour rise to 25."""
    import _cctally_journal as runtime
    import _lib_journal as journal

    _seed_cache(mod)
    fixed = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
    for at, weekly, five_hour in (
        ("2026-07-25T12:00:00Z", 63.0, 20.0),
        ("2026-07-25T12:05:00Z", 60.0, 25.0),
    ):
        runtime.append_record(
            _held_obs(journal, at=at, weekly_percent=weekly,
                      five_hour_percent=five_hour),
            now_utc=fixed)
        assert runtime.run_stats_ingest(mode="authoritative").ran is True


def _held_logical_state(mod):
    """The logical oracle, row ids excluded because two builds legitimately
    disagree on them."""
    conn = mod.open_db()
    try:
        return {
            "snapshots": [tuple(r) for r in conn.execute(
                "SELECT weekly_percent, weekly_observation_held, "
                "       five_hour_percent, five_hour_window_key, "
                "       captured_at_utc, week_start_at, week_end_at, source "
                "FROM weekly_usage_snapshots "
                "ORDER BY captured_at_utc, weekly_observation_held")],
            "blocks": [tuple(r) for r in conn.execute(
                "SELECT five_hour_window_key, seven_day_pct_at_block_start, "
                "       seven_day_pct_at_block_end, final_five_hour_percent "
                "FROM five_hour_blocks ORDER BY five_hour_window_key")],
            "fiveHourMilestones": [tuple(r) for r in conn.execute(
                "SELECT five_hour_window_key, percent_threshold, "
                "       seven_day_pct_at_crossing, reset_event_id "
                "FROM five_hour_milestones "
                "ORDER BY five_hour_window_key, percent_threshold")],
            "weeklyMilestones": [tuple(r) for r in conn.execute(
                "SELECT week_start_date, percent_threshold, reset_event_id "
                "FROM percent_milestones "
                "ORDER BY week_start_date, percent_threshold")],
        }
    finally:
        conn.close()


def _hwm(mod, name):
    try:
        return (pathlib.Path(mod.APP_DIR) / name).read_text().strip()
    except OSError:
        return None


def test_a_held_tick_leaves_the_rederive_planner_with_nothing_to_correct(
    tmp_path, monkeypatch, capsys
):
    """The rederive half of convergence, and the stronger of the two.

    `db rederive` reruns the RAW observations through `_pipeline_claude_usage`
    in a scratch epoch and diffs what it derives against what the journal
    holds. A no-op therefore says the scratch rerun reproduced the held tick's
    `snapshot_accept` decision exactly — the same held flag, the same carried
    weekly value, the same boundary and the same effective five-hour value. If
    the held decision were not journalled, or were journalled in a form the
    rerun does not reproduce, the planner would report actions here.
    """
    mod = _isolated(tmp_path, monkeypatch)
    _drive_held_sequence(mod)
    capsys.readouterr()

    before = _held_logical_state(mod)
    # Non-vacuity: there IS a held row for the planner to disagree about.
    assert [row[1] for row in before["snapshots"]] == [0, 1], before["snapshots"]
    assert before["snapshots"][1][2] == 25.0

    assert mod.cmd_db_rederive(_args()) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "no-op", preview
    counts = preview["actionCounts"]
    assert (counts["add"], counts["supersede"], counts["tombstone"]) == (
        0, 0, 0), preview
    # `retain` is the non-vacuity: the planner DID rerun the pipeline and
    # reproduce every journalled event, rather than reporting a no-op because
    # it derived nothing at all.
    assert counts["retain"] > 0, preview
    assert _held_logical_state(mod) == before


def test_a_rebuild_reproduces_the_held_tick_and_agrees_with_the_projection(
    tmp_path, monkeypatch
):
    """The rebuild half, plus the projection the two replays must agree on.

    `hwm-5h` is rematerialized from the published index at a LIVE-target
    publication, so a rebuild leaves it describing the index it just published
    rather than whatever it happened to hold. `hwm-7d` has no such pass and
    must not gain one: the held weekly value is carried-forward evidence, and
    the SQL weekly high-water clamp re-establishes that file on the next tick.
    """
    import _cctally_journal as runtime

    mod = _isolated(tmp_path, monkeypatch)
    _drive_held_sequence(mod)

    before = _held_logical_state(mod)
    weekly_before = _hwm(mod, "hwm-7d")
    assert [row[1] for row in before["snapshots"]] == [0, 1], before["snapshots"]

    # Corrupt the five-hour projection so the rematerialization is observable.
    (pathlib.Path(mod.APP_DIR) / "hwm-5h").write_text("1 1.0\n")
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"))

    assert _held_logical_state(mod) == before
    assert _hwm(mod, "hwm-5h").split()[1] == "25.0"
    assert _hwm(mod, "hwm-7d") == weekly_before


# ==========================================================================
# #834 S1 (#835) Gate A R5 — the rederive half. Specification line 124 requires
# this module to be EXTENDED, not merely run. The credit's removal of a stale
# replica is journalled as a suppression list precisely so a rederive does not
# resurrect the poisoned row, and #835 changed which rows that list may name —
# so convergence now has to be asserted over a store where a held row survived
# a credit that removed its non-held twin.
# ==========================================================================

def _r5_drive_held_sequence_unattributed(mod):
    """`_drive_held_sequence`'s twin, with the observations UNATTRIBUTED.

    THE REASON IS THE FIXTURE, NOT THE COMMAND. `_isolated` pins `HOME` to
    `tmp_path`, which carries no `~/.claude.json`, so
    `_cctally_core._resolve_active_claude_identity()` returns a stably-absent read
    and `cmd_record_credit` stamps the `unattributed` sentinel. On a real store
    with an `oauthAccount` it stamps that account's own key — measured:
    `{'account_key': 'unattributed', 'status': 'stably_absent'}` under an empty
    HOME against `{'account_key': '875d…', 'status': 'identified'}` with one — and
    a torn read exits 2 rather than falling back. So the credit reaches
    `unattributed`'s rows here because the observations were driven under that
    same sentinel, and the stale-replica band therefore matches, which is what
    lets the suppression list this test is about get built at all.

    An earlier version of this docstring said `record-credit` "resolves its whole
    plan account-blind and stamps `unattributed` at the end". The second half is
    false, and the correction is on #837 itself.

    #837'S ACTUAL MECHANISM IS THE FIRST HALF, AND IT IS A WRONG-POPULATION DEFECT
    RATHER THAN AN EMPTY ONE. `plan.from_pct` and the floor are resolved
    account-blind while the stamp and the band's `account_key` predicate are the
    ACTIVE account's. On a multi-account store the band's CENTRE can therefore be
    another account's percentage, applied to the active account's rows: the band is
    scoped correctly and centred wrongly, so it can remove the wrong rows or none
    rather than simply matching nothing.

    TWO ROUTES REACH `from_pct` ACCOUNT-BLIND, and a correction of record: an
    earlier version of this docstring named only the first.

      * `_resolve_reset_aware_hwm(..., account_key=None)`, the `hwm` source, which
        is the default when the week carries no prior credit.
      * the `existing` floor lookup, which selects
        `weekly_credit_floors WHERE week_start_date = ?` with NO account predicate
        and feeds its `observed_pre_credit_pct` through as `from_source ==
        'prior_credit'`. That branch is PREFERRED over the HWM whenever a floor
        already exists, so on a completion or a `--force` re-record it is the route
        that actually runs. It appears twice in `bin/_cctally_record.py` — once in
        `cmd_record_credit`'s body and once in the revalidation helper that re-runs
        the plan under the write lock — and both copies are account-blind."""
    import _cctally_journal as runtime
    import _lib_journal as journal

    _seed_cache(mod)
    # The rederive planner refuses with `missing-source` when an account with
    # positive usage has no Claude `session_entries`, so the unattributed bucket
    # needs spend of its own. `_seed_cache` seeds `acct-a`'s.
    path = "/tmp/claude/projects/repo/session-unattributed.jsonl"
    conn = mod.open_cache_db()
    conn.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
        (path, 100, 1, 100, AT, "session-u", "/repo"),
    )
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, "
        " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (path, 0, "2026-07-25T11:00:00+00:00",
         "claude-3-5-sonnet-20241022", 0, 0, 100, 0, 40, "unattributed"),
    )
    conn.commit()
    conn.close()
    fixed = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
    for at, weekly, five_hour in (
        ("2026-07-25T12:00:00Z", 63.0, 20.0),
        ("2026-07-25T12:05:00Z", 60.0, 25.0),
    ):
        runtime.append_record(
            journal.make_obs(
                at=at, src="record-usage", provider="claude",
                payload={
                    "captured_at": at,
                    "source": "statusline",
                    "weekly_percent": weekly,
                    "resets_at": _HELD_WEEK_RESETS_AT,
                    "five_hour_percent": five_hour,
                    "five_hour_resets_at": _HELD_5H_RESETS_AT,
                }),
            now_utc=fixed)
        assert runtime.run_stats_ingest(mode="authoritative").ran is True


def _r5_credit_args(*, week, **over):
    args = dict(to=30.0, from_pct=63.0, at="2026-07-25T12:02:00Z", week=week,
                dry_run=False, yes=True, json=False, force=False)
    args.update(over)
    return argparse.Namespace(**args)


def _r5_week_start_date(mod):
    conn = mod.open_db()
    try:
        return conn.execute(
            "SELECT week_start_date FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def test_a_rederive_converges_over_a_credit_that_preserved_a_held_row(
    tmp_path, monkeypatch, capsys
):
    """The held row and the suppression list that spared it must both survive a
    rerun of the raw pipeline.

    The two seeded rows carry the same weekly 63.0 and differ only in
    `weekly_observation_held`, and the credit's effective instant precedes both, so
    they enter the stale-replica band together. The non-held one is removed through
    the journalled suppression list; the held one is preserved, because it is the
    only carrier of its tick's five-hour reading.

    A rederive then reruns the pipeline and must reproduce exactly that: it must
    not resurrect the suppressed row — which is the failure the suppression list
    exists to prevent, since the poisoned row is itself a retained
    `snapshot_accept` — and it must not drop the preserved held row either."""
    mod = _isolated(tmp_path, monkeypatch)
    _r5_drive_held_sequence_unattributed(mod)
    week = _r5_week_start_date(mod)

    seeded = _held_logical_state(mod)
    assert [row[1] for row in seeded["snapshots"]] == [0, 1], seeded["snapshots"]

    assert mod.cmd_record_credit(_r5_credit_args(week=week)) == 0
    # Drain `record-credit`'s own line so the rederive preview is the only thing
    # on stdout when it is parsed.
    capsys.readouterr()

    before = _held_logical_state(mod)
    held = [row for row in before["snapshots"] if row[1] == 1]
    assert len(held) == 1, (
        "the credit removed the held row the preservation rule keeps")
    assert held[0][2] == 25.0, (
        "the held row survived but lost the five-hour reading it exists to hold")
    assert not [row for row in before["snapshots"]
                if row[1] == 0 and row[0] == 63.0], (
        "non-vacuity: the NON-held stale replica must have been removed, or "
        "there is no suppression list for the rederive to honour")

    assert mod.cmd_db_rederive(_args()) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "no-op", preview
    counts = preview["actionCounts"]
    assert (counts["add"], counts["supersede"], counts["tombstone"]) == (
        0, 0, 0), preview
    assert counts["retain"] > 0, preview
    assert _held_logical_state(mod) == before
