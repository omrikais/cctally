"""#372 Task B — deterministic, side-effect-free Claude usage rederive plans."""

from __future__ import annotations

import json
import datetime as dt
import sqlite3

import pytest


AT = "2026-07-25T12:00:00Z"


def _event(lib, event_id, kind, value, *, rev=0):
    return lib.make_evt(
        kind=kind,
        id=event_id,
        rev=rev,
        at=AT,
        payload={"value": value},
    )


def test_family_registry_exhaustively_classifies_current_journal_kinds(cctally_module):
    import _cctally_journal as journal
    import _lib_rederive as rederive

    report = rederive.validate_family_registry(
        evt_kinds=set(journal._EVT_SPECS),
        op_kinds=(
            set(journal.FOLD_APPLIERS)
            | set(journal._ACCOUNTS_MACHINERY_KINDS)
            | {"sync_week"}
        ),
    )

    assert report.family == "claude-usage"
    assert report.unclassified_evt_kinds == ()
    assert report.unclassified_op_kinds == ()
    assert report.classification_for_evt("snapshot_accept").mode == "rederived"
    assert report.classification_for_evt("quota_alert_arming").mode == "retained"
    assert report.classification_for_op("weekly_credit_floor").mode == "retained_input"
    assert report.classification_for_op("account_label").mode == "retained_input"
    assert report.classification_for_op("accounts_cutover").mode == "retained_input"


def test_plan_classifies_retain_supersede_tombstone_and_add_stably(cctally_module):
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _event(journal, "sa:retain", "snapshot_accept", 1),
        _event(journal, "sa:replace", "snapshot_accept", 1),
        _event(journal, "sa:remove", "snapshot_accept", 1),
        journal.make_evt(
            kind="quota_alert_arming",
            id="qaa:keep",
            at=AT,
            payload={"value": "provider-owned"},
        ),
    ]
    desired = [
        _event(journal, "sa:retain", "snapshot_accept", 1),
        _event(journal, "sa:replace", "snapshot_accept", 2),
        _event(journal, "sa:add", "snapshot_accept", 3),
    ]
    selection = journal.resolve_effective_events(current)

    first = rederive.build_claude_usage_plan(
        selection=selection,
        desired_events=desired,
        journal_high_water=("observations-2026-07.jsonl", 1234),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=(),
    )
    second = rederive.build_claude_usage_plan(
        selection=selection,
        desired_events=list(reversed(desired)),
        journal_high_water=("observations-2026-07.jsonl", 1234),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=(),
    )

    assert first.to_bytes() == second.to_bytes()
    assert first.counts == {
        "retain": 1,
        "supersede": 1,
        "tombstone": 1,
        "add": 1,
    }
    assert [(a.disposition, a.event_id, a.revision) for a in first.actions] == [
        ("add", "sa:add", 0),
        ("tombstone", "sa:remove", 1),
        ("supersede", "sa:replace", 1),
    ]
    decoded = json.loads(first.to_bytes())
    assert decoded["planHash"].startswith("sha256:")
    assert decoded["payloadHashes"] == sorted(decoded["payloadHashes"])
    assert "qaa:keep" not in {action.event_id for action in first.actions}


def test_percent_milestone_plan_preserves_non_derivable_alert_latch(
    cctally_module,
):
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = journal.make_evt(
        kind="percent_milestone",
        id="pm:acct:2026-07-20:0:6",
        at=AT,
        payload={"value": 1, "alerted_at": AT},
    )
    desired_same = journal.make_evt(
        kind="percent_milestone",
        id=current["id"],
        at=AT,
        payload={"value": 1, "alerted_at": None},
    )
    desired_corrected = journal.make_evt(
        kind="percent_milestone",
        id=current["id"],
        at=AT,
        payload={"value": 2, "alerted_at": None},
    )
    selection = journal.resolve_effective_events([current])
    kwargs = {
        "selection": selection,
        "journal_high_water": ("observations-2026-07.jsonl", 1234),
        "cache_fingerprint": "sha256:cache",
        "config_fingerprint": "sha256:config",
        "preserved_events": (),
    }

    retained = rederive.build_claude_usage_plan(
        desired_events=[desired_same], **kwargs
    )
    corrected = rederive.build_claude_usage_plan(
        desired_events=[desired_corrected], **kwargs
    )

    assert retained.actions == ()
    assert len(corrected.actions) == 1
    assert corrected.actions[0].disposition == "supersede"
    assert corrected.actions[0].payload["value"] == 2
    assert corrected.actions[0].payload["alerted_at"] == AT


def test_claude_family_preserves_codex_owned_budget_and_projection_events(
    cctally_module,
):
    import _lib_journal as journal
    import _lib_rederive as rederive

    codex_budget = journal.make_evt(
        kind="budget",
        id="budget:codex",
        at=AT,
        payload={"vendor": "codex", "threshold": 90},
    )
    codex_projection = journal.make_evt(
        kind="projected",
        id="projected:codex",
        at=AT,
        payload={"metric": "codex_weekly", "threshold": 90},
    )

    plan = rederive.build_claude_usage_plan(
        selection=journal.resolve_effective_events(
            [codex_budget, codex_projection]
        ),
        desired_events=[],
        journal_high_water=("observations-2026-07.jsonl", 100),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=(),
    )

    assert plan.actions == ()
    assert plan.retained_event_count == 2


def test_applying_plan_through_task_a_seam_makes_next_plan_empty(cctally_module):
    import _lib_journal as journal
    import _lib_rederive as rederive

    base = [_event(journal, "sa:x", "snapshot_accept", 1)]
    desired = [_event(journal, "sa:x", "snapshot_accept", 2)]
    first = rederive.build_claude_usage_plan(
        selection=journal.resolve_effective_events(base),
        desired_events=desired,
        journal_high_water=("observations-2026-07.jsonl", 10),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=(),
    )
    correction = journal.make_correction_batch(
        batch_id="batch:test",
        family="claude-usage",
        at=AT,
        actions=first.to_correction_actions(),
    )
    corrected = journal.resolve_effective_events([*base, *correction])

    second = rederive.build_claude_usage_plan(
        selection=corrected,
        desired_events=desired,
        journal_high_water=("observations-2026-07.jsonl", 20),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=(),
    )

    assert second.actions == ()
    assert second.counts == {
        "retain": 1,
        "supersede": 0,
        "tombstone": 0,
        "add": 0,
    }


@pytest.mark.parametrize(
    ("tables", "message"),
    [
        ({}, "cache.db table session_entries"),
        (
            {
                "session_entries": {
                    "timestamp_utc",
                    "model",
                    "input_tokens",
                    "output_tokens",
                    "cache_create_tokens",
                    "cache_read_tokens",
                }
            },
            "cache_create_1h_tokens",
        ),
        (
            {
                "session_entries": {
                    "timestamp_utc",
                    "model",
                    "input_tokens",
                    "output_tokens",
                    "cache_create_tokens",
                    "cache_read_tokens",
                    "cache_create_1h_tokens",
                    "source_path",
                    "account_key",
                }
            },
            "cache.db table session_files",
        ),
    ],
)
def test_missing_rederive_source_data_fails_with_precise_gap(tables, message):
    import _lib_rederive as rederive

    with pytest.raises(rederive.RederiveDataGap, match=message):
        rederive.validate_claude_cache_contract(tables)


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
    return conn


def _raw_obs(lib):
    resets = int(dt.datetime(
        2026, 7, 27, 0, 0, tzinfo=dt.timezone.utc).timestamp())
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


def test_scratch_planner_reuses_current_derivation_and_cache_ttl_split(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_journal as journal_runtime
    import _lib_journal as journal

    cache = _seed_cache(mod)
    obs = _raw_obs(journal)
    before_cache = mod.CACHE_DB_PATH.read_bytes()
    journal_runtime.ALERT_DISPATCHER = lambda alerts: pytest.fail(
        f"planner dispatched alerts: {alerts}")

    first = mod.plan_claude_usage_rederive(
        [obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 500),
    )
    second = mod.plan_claude_usage_rederive(
        [obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 500),
    )

    assert first.to_bytes() == second.to_bytes()
    assert before_cache == mod.CACHE_DB_PATH.read_bytes()
    assert not mod.JOURNAL_DIR.exists()
    assert not (mod.APP_DIR / "hwm-7d").exists()
    assert not (mod.APP_DIR / "hwm-5h").exists()
    assert first.counts["add"] >= 3  # snapshot + cost snapshot + milestone
    cost_actions = [
        action for action in first.actions
        if (action.payload or {}).get("kind") == "weekly_cost_snapshot"
    ]
    assert cost_actions
    # 40 tokens at the derived 2x 1h rate + 60 at the 1.25x 5m rate.
    assert cost_actions[-1].payload["cost_usd"] == pytest.approx(0.000465)
    cache.close()


def test_wrong_snapshot_is_superseded_and_applied_plan_is_noop(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_journal as journal_runtime
    import _lib_journal as journal

    cache = _seed_cache(mod)
    obs = _raw_obs(journal)
    desired_plan = mod.plan_claude_usage_rederive(
        [obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 500),
    )
    desired_events = []
    for action in desired_plan.actions:
        payload = dict(action.payload or {})
        if payload.get("kind") == "snapshot_accept":
            payload["weekly_percent"] = 99.0  # deliberately wrong clamp/decision
        if payload.get("kind") == "weekly_cost_snapshot":
            payload["cost_usd"] += 5.0  # deliberately wrong downstream decision
        desired_events.append(journal.make_evt(
            kind=payload.pop("kind"),
            id=action.event_id,
            at=action.at,
            payload=payload,
        ))

    wrong = mod.plan_claude_usage_rederive(
        [obs, *desired_events],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 900),
    )
    superseded = [
        action for action in wrong.actions
        if action.disposition == "supersede"
        and (action.payload or {}).get("kind") == "snapshot_accept"
    ]
    assert len(superseded) == 1
    assert superseded[0].payload["weekly_percent"] == 10.0
    assert any(
        action.disposition == "supersede"
        and (action.payload or {}).get("kind") == "weekly_cost_snapshot"
        and action.payload["cost_usd"] == pytest.approx(0.000465)
        for action in wrong.actions
    )

    correction = journal.make_correction_batch(
        batch_id="batch:planner-acceptance",
        family="claude-usage",
        at=AT,
        actions=wrong.to_correction_actions(),
    )
    after = mod.plan_claude_usage_rederive(
        [obs, *desired_events, *correction],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1200),
    )
    assert after.actions == ()

    dispatched = []
    journal_runtime.ALERT_DISPATCHER = lambda alerts: dispatched.extend(alerts)
    fixed = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
    for record in [obs, *desired_events, *correction]:
        journal_runtime.append_record(record, now_utc=fixed)
    live_path = tmp_path / "live-rebuilt.db"
    independent_path = tmp_path / "independent-rebuilt.db"
    journal_runtime.rebuild_stats_index(
        context=journal_runtime.RebuildContext(trigger="test-fixture"),
        target_path=str(live_path),
    )
    journal_runtime.rebuild_stats_index(
        context=journal_runtime.RebuildContext(trigger="test-fixture"),
        target_path=str(independent_path),
    )
    live = mod.open_db(_target_path=str(live_path))
    independent = mod.open_db(_target_path=str(independent_path))
    try:
        drop_cols = {
            "id", "usage_snapshot_id", "cost_snapshot_id",
            "reset_event_id", "block_id",
        }
        tables = (
            "weekly_usage_snapshots",
            "weekly_cost_snapshots",
            "week_reset_events",
            "five_hour_reset_events",
            "weekly_credit_floors",
            "percent_milestones",
            "five_hour_milestones",
            "budget_milestones",
            "projected_milestones",
            "project_budget_milestones",
            "quota_alert_arming",
            "journal_effective_events",
            "journal_protocol_violations",
        )
        for table in tables:
            columns = [
                row[1] for row in live.execute(f"PRAGMA table_info({table})")
                if row[1] not in drop_cols
            ]
            query = f"SELECT {', '.join(columns)} FROM {table}"
            live_rows = sorted(
                [tuple(row) for row in live.execute(query)],
                key=lambda row: tuple(str(value) for value in row),
            )
            independent_rows = sorted(
                [tuple(row) for row in independent.execute(query)],
                key=lambda row: tuple(str(value) for value in row),
            )
            assert live_rows == independent_rows, table
        assert live.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = ?",
            (superseded[0].event_id,),
        ).fetchone()[0] == 10.0
    finally:
        live.close()
        independent.close()
    journal_bytes = b"".join(
        (mod.JOURNAL_DIR / segment).read_bytes()
        for segment in journal_runtime.list_segments()
    )
    wrong_snapshot = next(
        event for event in desired_events
        if (event.get("payload") or {}).get("kind") == "snapshot_accept"
    )
    assert journal.encode_line(wrong_snapshot) in journal_bytes
    assert dispatched == []
    cache.close()


def test_unknown_cache_ttl_split_refuses_before_any_planning_write(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal
    import _lib_rederive as rederive

    cache = _seed_cache(mod)
    cache.execute(
        "UPDATE session_entries SET cache_create_1h_tokens = NULL")
    cache.commit()
    before = mod.CACHE_DB_PATH.read_bytes()

    with pytest.raises(
        rederive.RederiveDataGap, match="cache_create_1h_tokens missing"
    ):
        mod.plan_claude_usage_rederive(
            [_raw_obs(journal)],
            cache_conn=cache,
            journal_high_water=("observations-2026-07.jsonl", 500),
        )

    assert before == mod.CACHE_DB_PATH.read_bytes()
    assert not mod.JOURNAL_DIR.exists()
    cache.close()


def test_positive_account_without_its_own_cache_rows_refuses_precisely(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal
    import _lib_rederive as rederive

    cache = _seed_cache(mod)
    obs = _raw_obs(journal)
    obs["account"] = "acct-b"

    with pytest.raises(
        rederive.RederiveDataGap,
        match="no Claude session_entries for positive usage account acct-b",
    ):
        mod.plan_claude_usage_rederive(
            [obs],
            cache_conn=cache,
            journal_high_water=("observations-2026-07.jsonl", 500),
        )

    assert not mod.JOURNAL_DIR.exists()
    cache.close()


def test_rederive_refuses_a_tainted_structural_batch_directly(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal
    import _lib_rederive as rederive

    cache = _seed_cache(mod)
    commit_without_begin = journal.make_correction_batch(
        batch_id="batch:rederive-taint",
        family="claude-usage",
        at=AT,
        actions=[],
    )[-1]

    with pytest.raises(
        rederive.RederiveConflict,
        match=(
            "journal contains tainted correction batch.*"
            "batch:rederive-taint:commit_without_begin"
        ),
    ):
        mod.plan_claude_usage_rederive(
            [_raw_obs(journal), commit_without_begin],
            cache_conn=cache,
            journal_high_water=("observations-2026-07.jsonl", 500),
        )

    assert not mod.JOURNAL_DIR.exists()
    cache.close()


def test_rederive_still_refuses_an_acknowledged_tainted_batch(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal
    import _lib_rederive as rederive

    cache = _seed_cache(mod)
    commit_without_begin = journal.make_correction_batch(
        batch_id="batch:rederive-acknowledged-taint",
        family="claude-usage",
        at=AT,
        actions=[],
    )[-1]
    records = [_raw_obs(journal), commit_without_begin]
    violation = journal.resolve_effective_events(
        records
    ).protocol_violations[0]
    audit = journal.make_protocol_resolution(
        at=AT,
        violations=[violation],
        journal_high_water=("observations-2026-07.jsonl", 500),
        journal_prefix_hash="sha256:" + ("6" * 64),
    )

    with pytest.raises(
        rederive.RederiveConflict,
        match=(
            "journal contains tainted correction batch.*"
            "batch:rederive-acknowledged-taint:commit_without_begin"
        ),
    ):
        mod.plan_claude_usage_rederive(
            [*records, audit],
            cache_conn=cache,
            journal_high_water=("observations-2026-07.jsonl", 900),
            protocol_prefix_evidence=[
                (
                    ("observations-2026-07.jsonl", 500),
                    "sha256:" + ("6" * 64),
                )
            ],
        )

    assert not mod.JOURNAL_DIR.exists()
    cache.close()


def test_multi_account_identity_change_tombstones_old_id_without_crossing(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    path_b = "/tmp/claude/projects/repo-b/session.jsonl"
    cache.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
        (path_b, 100, 2, 100, AT, "session-b", "/repo-b"),
    )
    cache.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, "
        " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            path_b, 0, "2026-07-25T11:30:00+00:00",
            "claude-3-5-sonnet-20241022", 100, 0, 0, 0, 0, "acct-b",
        ),
    )
    cache.commit()
    obs_a = _raw_obs(journal)
    payload_b = dict(obs_a["payload"])
    payload_b["weekly_percent"] = 20.0
    obs_b = journal.make_obs(
        at="2026-07-25T12:01:00Z",
        src="record-usage",
        provider="claude",
        account="acct-b",
        payload=payload_b,
    )
    desired = mod.plan_claude_usage_rederive(
        [obs_a, obs_b],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 700),
    )
    desired_events = []
    wrong_id = None
    for action in desired.actions:
        payload = dict(action.payload or {})
        event_id = action.event_id
        if (
            payload.get("kind") == "snapshot_accept"
            and payload.get("account_key") == "acct-a"
        ):
            wrong_id = "sa:wrong-account-a-identity"
            event_id = wrong_id
        desired_events.append(journal.make_evt(
            kind=payload.pop("kind"),
            id=event_id,
            at=action.at,
            payload=payload,
        ))

    plan = mod.plan_claude_usage_rederive(
        [obs_a, obs_b, *desired_events],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1100),
    )

    assert wrong_id is not None
    assert any(
        action.disposition == "tombstone" and action.event_id == wrong_id
        for action in plan.actions
    )
    added_snapshots = [
        action for action in plan.actions
        if action.disposition == "add"
        and (action.payload or {}).get("kind") == "snapshot_accept"
    ]
    assert len(added_snapshots) == 1
    assert added_snapshots[0].payload["account_key"] == "acct-a"
    assert {
        (action.payload or {}).get("account_key")
        for action in desired.actions
        if (action.payload or {}).get("kind") == "snapshot_accept"
    } == {"acct-a", "acct-b"}
    cache.close()


def test_multi_account_reset_detection_never_uses_another_accounts_prior(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    path_b = "/tmp/claude/projects/repo-b/session.jsonl"
    cache.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
        (path_b, 100, 2, 100, AT, "session-b", "/repo-b"),
    )
    cache.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, "
        " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            path_b, 0, "2026-07-25T11:30:00+00:00",
            "claude-3-5-sonnet-20241022", 100, 0, 0, 0, 0, "acct-b",
        ),
    )
    cache.commit()
    payload_a = dict(_raw_obs(journal)["payload"])
    payload_a["weekly_percent"] = 80.0
    payload_a["five_hour_percent"] = 20.0
    payload_a["five_hour_resets_at"] = "2026-07-25T15:00:00Z"
    obs_a = journal.make_obs(
        at=AT,
        src="record-usage",
        provider="claude",
        account="acct-a",
        payload=payload_a,
    )
    payload_b = dict(payload_a)
    payload_b["captured_at"] = "2026-07-25T12:01:00Z"
    payload_b["weekly_percent"] = 10.0
    payload_b["five_hour_percent"] = 5.0
    obs_b = journal.make_obs(
        at="2026-07-25T12:01:00Z",
        src="record-usage",
        provider="claude",
        account="acct-b",
        payload=payload_b,
    )

    plan = mod.plan_claude_usage_rederive(
        [obs_a, obs_b],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 800),
    )

    assert not any(
        (action.payload or {}).get("kind") == "week_reset"
        and (action.payload or {}).get("account_key") == "acct-b"
        for action in plan.actions
    )
    assert not any(
        (action.payload or {}).get("kind") == "five_hour_credit"
        and (action.payload or {}).get("account_key") == "acct-b"
        for action in plan.actions
    )
    cache.close()


def test_delayed_cache_entry_is_excluded_from_earlier_as_of_cost(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    cache.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, "
        " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "/tmp/claude/projects/repo/session.jsonl",
            50,
            "2026-07-25T12:30:00+00:00",
            "claude-3-5-sonnet-20241022",
            1000,
            0,
            0,
            0,
            0,
            "acct-a",
        ),
    )
    cache.commit()
    first_obs = _raw_obs(journal)
    second_payload = dict(first_obs["payload"])
    second_payload["captured_at"] = "2026-07-25T13:00:00Z"
    second_payload["weekly_percent"] = 11.0
    second_obs = journal.make_obs(
        at="2026-07-25T13:00:00Z",
        src="record-usage",
        provider="claude",
        account="acct-a",
        payload=second_payload,
    )

    plan = mod.plan_claude_usage_rederive(
        [first_obs, second_obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 800),
    )
    costs = sorted(
        (
            action.at,
            action.payload["cost_usd"],
        )
        for action in plan.actions
        if (action.payload or {}).get("kind") == "weekly_cost_snapshot"
    )

    assert costs[0][1] == pytest.approx(0.000465)
    assert costs[-1][1] == pytest.approx(0.003465)
    cache.close()


def test_record_credit_effect_identity_change_is_dependency_closed(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    first_payload = dict(_raw_obs(journal)["payload"])
    first_payload["weekly_percent"] = 50.0
    first = journal.make_obs(
        at=AT,
        src="record-usage",
        provider="claude",
        account="acct-a",
        payload=first_payload,
    )
    at_dt = dt.datetime(2026, 7, 25, 13, 0, tzinfo=dt.timezone.utc)
    credit_plan = mod._build_credit_plan(
        week_start_date="2026-07-20",
        week_start_at="2026-07-20T00:00:00+00:00",
        week_end_at="2026-07-27T00:00:00+00:00",
        from_pct=50.0,
        from_source="explicit",
        to_pct=40.0,
        at_dt=at_dt,
        now=at_dt,
    )
    credit = journal.make_op(
        at="2026-07-25T13:00:00Z",
        src="record-credit",
        payload={
            "kind": "weekly_credit_floor",
            "week_start_date": credit_plan.week_start_date,
            "effective_at_utc": credit_plan.effective_iso,
            "observed_pre_credit_pct": credit_plan.from_pct,
            "applied_at_utc": "2026-07-25T13:00:00Z",
            "plan": dict(vars(credit_plan)),
            "five_hour": [None, None, None],
            "forced": False,
            "account_key": "acct-a",
        },
    )
    desired = mod.plan_claude_usage_rederive(
        [first, credit],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 900),
    )
    desired_events = []
    wrong_effect_id = None
    for action in desired.actions:
        payload = dict(action.payload or {})
        event_id = action.event_id
        if payload.get("kind") == "weekly_credit_effects":
            wrong_effect_id = "wce:wrong-credit-identity"
            event_id = wrong_effect_id
        desired_events.append(journal.make_evt(
            kind=payload.pop("kind"),
            id=event_id,
            at=action.at,
            payload=payload,
        ))

    correction_plan = mod.plan_claude_usage_rederive(
        [first, credit, *desired_events],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1400),
    )

    assert wrong_effect_id is not None
    dispositions = {
        (action.disposition, action.event_id)
        for action in correction_plan.actions
    }
    assert ("tombstone", wrong_effect_id) in dispositions
    assert any(
        action.disposition == "add"
        and (action.payload or {}).get("kind") == "weekly_credit_effects"
        for action in correction_plan.actions
    )
    # The synthetic post-credit snapshot and its dependent milestone/cost facts
    # are retained or corrected in the same family plan, never left outside it.
    family_kinds = {
        (action.payload or {}).get("kind") for action in desired.actions
    }
    assert {"weekly_credit_effects", "snapshot_accept"} <= family_kinds
    marker = mod.APP_DIR / "pending-reset-zero-7d"
    marker.write_text(
        "2026-07-20 2026-07-27T00:00:00+00:00 50.0 "
        "2026-07-25T12:30:00+00:00\n"
    )
    hwm5 = mod.APP_DIR / "hwm-5h"
    hwm5.write_text("sentinel-window 99\n")
    before_marker = marker.read_bytes()
    before_hwm5 = hwm5.read_bytes()

    repeated = mod.plan_claude_usage_rederive(
        [first, credit],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 900),
    )

    assert repeated.to_bytes() == desired.to_bytes()
    assert marker.read_bytes() == before_marker
    assert hwm5.read_bytes() == before_hwm5
    cache.close()


def test_historical_reset_marker_and_five_hour_hwm_are_replayed_in_memory(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    base_payload = dict(_raw_obs(journal)["payload"])
    base_payload["weekly_percent"] = 20.0
    base_payload["five_hour_percent"] = 20.0
    base_payload["five_hour_resets_at"] = "2026-07-25T15:00:00Z"

    def obs(at, weekly, five_hour):
        payload = dict(base_payload)
        payload["captured_at"] = at
        payload["weekly_percent"] = weekly
        payload["five_hour_percent"] = five_hour
        return journal.make_obs(
            at=at,
            src="record-usage",
            provider="claude",
            account="acct-a",
            payload=payload,
        )

    records = [
        obs("2026-07-25T12:00:00Z", 20.0, 20.0),
        obs("2026-07-25T12:01:00Z", 0.0, 5.0),
        obs("2026-07-25T12:02:00Z", 0.0, 5.0),
    ]
    marker = mod.APP_DIR / "pending-reset-zero-7d"
    marker.write_text(
        "unrelated 2026-07-28T00:00:00+00:00 99.0 "
        "2026-07-25T11:00:00+00:00\n"
    )
    hwm5 = mod.APP_DIR / "hwm-5h"
    hwm5.write_text("sentinel-window 99\n")
    before_marker = marker.read_bytes()
    before_hwm5 = hwm5.read_bytes()

    first = mod.plan_claude_usage_rederive(
        records,
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1000),
    )
    marker.write_text(
        "different 2026-07-29T00:00:00+00:00 88.0 "
        "2026-07-25T10:00:00+00:00\n"
    )
    changed_external_marker = marker.read_bytes()
    second = mod.plan_claude_usage_rederive(
        records,
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1000),
    )

    assert first.to_bytes() == second.to_bytes()
    assert {
        (action.payload or {}).get("kind") for action in first.actions
    } >= {"week_reset", "five_hour_credit"}
    assert marker.read_bytes() == changed_external_marker
    assert hwm5.read_bytes() == before_hwm5
    assert before_marker != changed_external_marker
    cache.close()


def test_automatic_week_reset_is_rederived_with_dependent_snapshot(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    first_payload = dict(_raw_obs(journal)["payload"])
    first_payload["weekly_percent"] = 60.0
    first_payload["resets_at"] = int(dt.datetime(
        2026, 7, 26, 0, 0, tzinfo=dt.timezone.utc).timestamp())
    first = journal.make_obs(
        at=AT,
        src="record-usage",
        provider="claude",
        account="acct-a",
        payload=first_payload,
    )
    second_payload = dict(first_payload)
    second_payload["captured_at"] = "2026-07-25T13:00:00Z"
    second_payload["weekly_percent"] = 10.0
    second_payload["resets_at"] = int(dt.datetime(
        2026, 7, 27, 0, 0, tzinfo=dt.timezone.utc).timestamp())
    second = journal.make_obs(
        at="2026-07-25T13:00:00Z",
        src="record-usage",
        provider="claude",
        account="acct-a",
        payload=second_payload,
    )

    desired = mod.plan_claude_usage_rederive(
        [first, second],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 900),
    )
    kinds = {
        (action.payload or {}).get("kind") for action in desired.actions
    }

    assert "week_reset" in kinds
    assert "snapshot_accept" in kinds
    assert "weekly_cost_snapshot" in kinds
    cache.close()


def test_legacy_cutover_account_normalizes_unstamped_claude_history(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _lib_journal as journal

    cache = _seed_cache(mod)
    legacy_account = "acct-a"
    cutover = journal.make_op(
        at="2026-07-25T11:00:00Z",
        src="accounts-cutover",
        payload={
            "kind": "accounts_cutover",
            "claude_legacy_account": legacy_account,
        },
    )
    cutover["id"] = runtime.CUTOVER_OP_ID
    legacy_obs = _raw_obs(journal)
    legacy_obs.pop("account")

    plan = mod.plan_claude_usage_rederive(
        [cutover, legacy_obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 700),
    )

    snapshots = [
        action.payload for action in plan.actions
        if (action.payload or {}).get("kind") == "snapshot_accept"
    ]
    assert snapshots
    assert {payload["account_key"] for payload in snapshots} == {legacy_account}
    cache.close()


def test_future_custom_sync_window_is_bounded_by_retained_op_time(
    tmp_path, monkeypatch
):
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    cache.execute(
        "UPDATE session_entries SET timestamp_utc = ?",
        ("2026-07-21T12:00:00+00:00",),
    )
    cache.commit()
    sync = journal.make_op(
        at="2026-07-01T12:00:00Z",
        src="sync-week",
        payload={
            "kind": "sync_week",
            "week_start": "2026-07-20",
            "week_end": "2026-07-26",
            "week_start_name": None,
            "mode": "auto",
            "offline": True,
            "project": None,
            "account_key": "acct-a",
        },
    )

    plan = mod.plan_claude_usage_rederive(
        [sync],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 500),
    )
    costs = [
        action.payload for action in plan.actions
        if (action.payload or {}).get("kind") == "weekly_cost_snapshot"
    ]

    assert len(costs) == 1
    assert costs[0]["cost_usd"] == 0.0
    assert dt.datetime.fromisoformat(
        costs[0]["range_end_iso"]
    ).astimezone(dt.timezone.utc) == dt.datetime(
        2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc
    )
    cache.close()


def _fingerprint_cache(rows):
    import _cctally_rederive as eager

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE session_entries ("
        "id INTEGER PRIMARY KEY, source_path TEXT, line_offset INTEGER, "
        "timestamp_utc TEXT, model TEXT, input_tokens INTEGER, "
        "output_tokens INTEGER, cache_create_tokens INTEGER, "
        "cache_read_tokens INTEGER, cache_create_1h_tokens INTEGER, "
        "cost_usd_raw REAL, speed TEXT, account_key TEXT)"
    )
    conn.execute(
        "CREATE TABLE session_files ("
        "path TEXT PRIMARY KEY, session_id TEXT, project_path TEXT)"
    )
    for source_path, line_offset in rows:
        conn.execute(
            "INSERT OR IGNORE INTO session_files VALUES (?,?,?)",
            (source_path, source_path + "-session", source_path + "-project"),
        )
        conn.execute(
            "INSERT INTO session_entries ("
            "source_path, line_offset, timestamp_utc, model, input_tokens, "
            "output_tokens, cache_create_tokens, cache_read_tokens, "
            "cache_create_1h_tokens, cost_usd_raw, speed, account_key"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_path, line_offset, "2026-07-25T12:00:00+00:00",
                "claude-3-5-sonnet-20241022", 1, 2, 3, 4, 2, None, None,
                "acct-a",
            ),
        )
    fingerprint = eager._cache_fingerprint(conn)
    conn.close()
    return fingerprint


def test_cache_fingerprint_ignores_surrogate_insertion_order(cctally_module):
    rows = [("/project/a.jsonl", 0), ("/project/b.jsonl", 10)]

    assert _fingerprint_cache(rows) == _fingerprint_cache(list(reversed(rows)))


# ==========================================================================
# #374 — forced supersede for quarantined same-revision groups
#
# `build_claude_usage_plan` returns `retain` and continues when current and
# desired are semantically equal, BEFORE `revision = selected.rev + 1` is
# reached. With the lowest-sequence provisional winner, a quarantined group
# whose winner already matches the desired re-derivation therefore emitted no
# action at all and the rev-0 conflict survived in the append-only journal
# forever. The planner now forces a revision advance for those ids.
# ==========================================================================

def _conflicted_selection(journal, event_id, kind="snapshot_accept"):
    """A selection carrying a quarantined rev-0 group whose PROVISIONAL winner
    is semantically identical to what the planner will re-derive."""
    first = _event(journal, event_id, kind, 1)
    second = _event(journal, event_id, kind, 2)
    selection = journal.resolve_effective_events([first, second])
    assert [c.event_id for c in selection.conflicts] == [event_id]
    return selection, first


def _plan(rederive, selection, desired, conflicted=frozenset()):
    return rederive.build_claude_usage_plan(
        selection=selection,
        desired_events=desired,
        journal_high_water=("observations-2026-07.jsonl", 1234),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=(),
        conflicted_event_ids=conflicted,
    )


def test_forced_supersede_clears_a_semantically_identical_conflict(cctally_module):
    import _lib_journal as journal
    import _lib_rederive as rederive

    selection, winner = _conflicted_selection(journal, "sa:deadbeef")

    without = _plan(rederive, selection, [winner])
    assert without.counts == {"retain": 1, "supersede": 0, "tombstone": 0, "add": 0}

    forced = _plan(rederive, selection, [winner],
                   conflicted=frozenset({"sa:deadbeef"}))

    assert forced.counts == {"retain": 0, "supersede": 1, "tombstone": 0, "add": 0}
    action = next(a for a in forced.actions if a.event_id == "sa:deadbeef")
    assert action.disposition == "supersede"
    assert action.revision == 1
    assert action.payload == dict(winner["payload"])


def test_forced_supersede_moves_exactly_the_conflicted_count(cctally_module):
    import _lib_journal as journal
    import _lib_rederive as rederive

    conflicted = [_event(journal, f"sa:c{i}", "snapshot_accept", 1) for i in range(3)]
    divergent = [_event(journal, f"sa:c{i}", "snapshot_accept", 9) for i in range(3)]
    clean = [_event(journal, "sa:clean", "snapshot_accept", 1)]
    selection = journal.resolve_effective_events([*conflicted, *divergent, *clean])
    desired = [*conflicted, *clean]

    base = _plan(rederive, selection, desired)
    forced = _plan(rederive, selection, desired,
                   conflicted=frozenset(c["id"] for c in conflicted))

    assert base.counts["retain"] == 4
    assert forced.counts["retain"] == base.counts["retain"] - 3
    assert forced.counts["supersede"] == base.counts["supersede"] + 3


def test_forced_supersede_previews_are_byte_identical_and_idempotent(cctally_module):
    import _lib_journal as journal
    import _lib_rederive as rederive

    selection, winner = _conflicted_selection(journal, "sa:deadbeef")
    conflicted = frozenset({"sa:deadbeef"})

    first = _plan(rederive, selection, [winner], conflicted=conflicted)
    second = _plan(rederive, selection, [winner], conflicted=conflicted)

    assert first.to_bytes() == second.to_bytes()
    assert first.plan_hash == second.plan_hash
    assert [a.to_dict() for a in first.actions] == [
        a.to_dict() for a in second.actions
    ]

    # After the batch lands, the rev-1 winner is unconflicted and the plan is a
    # no-op — a second run must not append another correction.
    batch = journal.make_correction_batch(
        batch_id="rederive:test:abc",
        family="claude-usage",
        at=AT,
        actions=first.to_correction_actions(),
    )
    settled = journal.resolve_effective_events(
        [_event(journal, "sa:deadbeef", "snapshot_accept", 1),
         _event(journal, "sa:deadbeef", "snapshot_accept", 2),
         *batch]
    )
    assert settled.conflicts == ()
    assert settled.by_id["sa:deadbeef"].rev == 1

    after = _plan(rederive, settled, [winner], conflicted=frozenset())
    assert after.counts == {"retain": 1, "supersede": 0, "tombstone": 0, "add": 0}


def test_post_batch_selector_reports_zero_winning_revision_conflicts(cctally_module):
    """Acceptance 3's end state, including for a group whose provisional winner
    already matched the desired state."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    first = _event(journal, "sa:deadbeef", "snapshot_accept", 1)
    second = _event(journal, "sa:deadbeef", "snapshot_accept", 2)
    selection = journal.resolve_effective_events([first, second])
    plan = _plan(rederive, selection, [first],
                 conflicted=frozenset({"sa:deadbeef"}))
    batch = journal.make_correction_batch(
        batch_id="rederive:test:abc",
        family="claude-usage",
        at=AT,
        actions=plan.to_correction_actions(),
    )

    settled = journal.resolve_effective_events([first, second, *batch])

    assert settled.conflicts == ()


def test_forced_supersede_ignores_conflicts_owned_by_another_family(cctally_module):
    import _lib_journal as journal
    import _lib_rederive as rederive

    first = journal.make_evt(
        kind="quota_alert_arming", id="qaa:foreign", at=AT,
        payload={"value": 1, "journal_identity_version": 2})
    second = journal.make_evt(
        kind="quota_alert_arming", id="qaa:foreign", at=AT,
        payload={"value": 2, "journal_identity_version": 2})
    selection = journal.resolve_effective_events([first, second])
    assert [c.event_id for c in selection.conflicts] == ["qaa:foreign"]

    plan = _plan(rederive, selection, [],
                 conflicted=frozenset({"qaa:foreign"}))

    assert plan.actions == ()
    assert plan.counts == {"retain": 0, "supersede": 0, "tombstone": 0, "add": 0}


def test_forced_supersede_leaves_a_tombstone_disposition_alone(cctally_module):
    """A conflicted id the re-derivation no longer produces already advances via
    the tombstone branch; forcing must not double-count it."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    selection, _winner = _conflicted_selection(journal, "sa:gone")

    plan = _plan(rederive, selection, [], conflicted=frozenset({"sa:gone"}))

    assert plan.counts == {"retain": 0, "supersede": 0, "tombstone": 1, "add": 0}
    assert plan.actions[0].revision == 1


# ==========================================================================
# #426 — pre-cutover history the family cannot re-derive
#
# The journal cutover exports the pre-journal stats rows as `b:<table>:<rowid>`
# evt lines. Nothing behind them is retained: Claude usage observations only
# start being journaled AT the cutover, so a scratch replay of retained
# observations can never reproduce them. The plan diffed them anyway, so every
# exported row landed in the "current but not desired" branch and was
# TOMBSTONED — one `db rederive --yes` retired months of weekly usage/cost
# history (27 weeks -> 2 on the reporter's install) and every later rebuild
# faithfully replayed the tombstones.
# ==========================================================================

HISTORY_AT = "2026-03-09T18:20:42.723Z"


def _bootstrap_snapshot(journal, rowid, *, at=HISTORY_AT, weekly_percent=27.0):
    """One cutover-exported `weekly_usage_snapshots` row, as the real bootstrap
    segment writes it."""
    return journal.make_evt(
        kind="snapshot_accept",
        id=journal.bootstrap_id("weekly_usage_snapshots", rowid),
        at=at,
        payload={
            "captured_at_utc": at,
            "five_hour_percent": None,
            "five_hour_resets_at": None,
            "five_hour_window_key": None,
            "page_url": "https://claude.ai/settings/usage",
            "payload_json": "{}",
            "source": "tampermonkey",
            "week_start_date": "2026-03-06",
            "week_end_date": "2026-03-13",
            "week_start_at": "2026-03-06T10:00:00+02:00",
            "week_end_at": "2026-03-13T10:00:00+02:00",
            "weekly_percent": weekly_percent,
        },
    )


def _tombstone_batch(journal, event_id, *, batch_id, at=AT, rev=1):
    return journal.make_correction_batch(
        batch_id=batch_id,
        family="claude-usage",
        at=at,
        actions=[{
            "action": "tombstone",
            "id": event_id,
            "rev": rev,
            "at": HISTORY_AT,
            "payload": None,
        }],
    )


def test_pre_cutover_history_is_preserved_not_tombstoned(tmp_path, monkeypatch):
    """The reporter's bug: history exported at cutover must survive a rederive."""
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    history = _bootstrap_snapshot(journal, 16)
    obs = _raw_obs(journal)

    plan = mod.plan_claude_usage_rederive(
        [history, obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 500),
    )

    dispositions = {a.event_id: a.disposition for a in plan.actions}
    assert history["id"] not in dispositions
    assert plan.counts["tombstone"] == 0
    assert plan.counts["retain"] >= 1
    # The retained-observation window still re-derives normally.
    assert plan.counts["add"] >= 3
    cache.close()


def test_derivable_window_events_still_retire_when_no_longer_derived(
    tmp_path, monkeypatch
):
    """The preservation rule is scoped to what the family cannot re-derive: a
    stale event INSIDE the retained window must still tombstone."""
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    obs = _raw_obs(journal)
    stale = journal.make_evt(
        kind="snapshot_accept",
        id="sa:acct-a:stale",
        at="2026-07-26T00:00:00Z",  # after the retained-observation floor
        payload={"weekly_percent": 99.0},
    )

    plan = mod.plan_claude_usage_rederive(
        [obs, stale],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 500),
    )

    assert [a.disposition for a in plan.actions if a.event_id == "sa:acct-a:stale"] == [
        "tombstone"
    ]
    cache.close()


def test_history_retired_by_a_prior_rederive_batch_is_revived(
    tmp_path, monkeypatch
):
    """Recovery leg: the append-only journal keeps the wrongly-tombstoned
    history, so a plan built by the fixed planner restores it at rev + 1."""
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    history = _bootstrap_snapshot(journal, 16)
    batch = _tombstone_batch(
        journal, history["id"],
        batch_id="rederive:claude-usage:430771d9",
    )
    obs = _raw_obs(journal)

    plan = mod.plan_claude_usage_rederive(
        [history, *batch, obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 500),
    )

    revive = next(a for a in plan.actions if a.event_id == history["id"])
    assert revive.disposition == "supersede"
    assert revive.revision == 2
    assert revive.at == history["at"]
    # Every original field comes back untouched; the #341 legacy-account stamp
    # is the one addition, and it is exactly what a rebuild fold would apply.
    assert {
        key: value for key, value in revive.payload.items()
        if key in history["payload"]
    } == dict(history["payload"])
    assert revive.payload["account_key"] == "unattributed"

    settled = journal.resolve_effective_events([
        history,
        *batch,
        *journal.make_correction_batch(
            batch_id="rederive:claude-usage:recovery",
            family="claude-usage",
            at=AT,
            actions=plan.to_correction_actions(),
        ),
    ])
    selected = settled.by_id[history["id"]]
    assert selected.status == "active"
    assert selected.record["payload"] == revive.payload
    cache.close()


def test_history_retired_by_another_family_is_left_alone(tmp_path, monkeypatch):
    """Only this family's own destructive batches are undone — a deliberate
    operator retirement stays retired."""
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    history = _bootstrap_snapshot(journal, 16)
    batch = _tombstone_batch(
        journal, history["id"], batch_id="operator:retire-duplicate",
    )
    obs = _raw_obs(journal)

    plan = mod.plan_claude_usage_rederive(
        [history, *batch, obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 500),
    )

    assert history["id"] not in {a.event_id for a in plan.actions}
    cache.close()


def test_operator_records_alone_are_not_re_derivation_evidence(
    tmp_path, monkeypatch
):
    """Evidence is counted in observations. An operator record is replay INPUT
    but derives nothing on its own — and the cutover re-emits some of them with
    their original historical timestamps — so a journal carrying only operator
    records can produce no desired set, and must plan nothing destructive."""
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    credit_op = journal.make_op(
        at="2026-06-19T09:22:43Z",
        src="bootstrap",
        payload={
            "kind": "weekly_credit_floor",
            "week_start_date": "2026-06-18",
            "effective_at_utc": "2026-06-19T09:22:43Z",
            "observed_pre_credit_pct": 46.0,
            "account_key": "acct-a",
        },
    )
    # A family-minted id, so preservation here can only come from the
    # no-evidence rail — not from the cutover-export id rule.
    derived = journal.make_evt(
        kind="snapshot_accept",
        id="sa:acct-a:no-evidence",
        at="2026-07-04T09:00:00Z",
        payload={"weekly_percent": 42.0},
    )

    plan = mod.plan_claude_usage_rederive(
        [credit_op, derived],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 500),
    )

    assert plan.actions == ()
    assert plan.counts["tombstone"] == 0
    assert plan.preserved_event_count == 1
    cache.close()


def test_a_journal_without_retained_evidence_plans_no_destruction(cctally_module):
    """Kernel form of the same rail, over a cutover-exported event."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    history = _bootstrap_snapshot(journal, 16)
    selection = journal.resolve_effective_events([history])

    preserved = rederive.preserved_history([history], evidence_retained=False)
    assert set(preserved) == {history["id"]}
    # ... and the cutover-export rule alone preserves it even WITH evidence.
    assert set(
        rederive.preserved_history([history], evidence_retained=True)
    ) == {history["id"]}

    plan = rederive.build_claude_usage_plan(
        selection=selection,
        desired_events=[],
        journal_high_water=("observations-2026-07.jsonl", 1234),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=preserved.values(),
    )

    assert plan.counts == {"retain": 1, "supersede": 0, "tombstone": 0, "add": 0}
    assert plan.actions == ()
    assert plan.preserved_event_count == 1
