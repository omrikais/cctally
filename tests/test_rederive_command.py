"""#372 Task C — fail-safe `cctally db rederive` orchestration."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import signal
import subprocess

import pytest


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
    runtime.rebuild_stats_index()
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
    runtime.rebuild_stats_index(target_path=independent)
    assert _logical_dump(mod.DB_PATH) == _logical_dump(independent)


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
    runtime.rebuild_stats_index(update_quota_cache=False)
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
    assert (hwm7.read_bytes(), hwm5.read_bytes()) == hwm_before
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
    preview_run = subprocess.run(
        preview_command,
        env=preview_env,
        capture_output=True,
        text=True,
        timeout=30,
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
        timeout=30,
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
        timeout=60,
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
    runtime.rebuild_stats_index()
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
