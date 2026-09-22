"""#372 Task B — deterministic, side-effect-free Claude usage rederive plans."""

from __future__ import annotations

import json
import argparse
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
    assert first.action_counts_by_event_kind["snapshot_accept"] == first.counts
    assert first.action_counts_by_event_kind["five_hour_credit"] == {
        "retain": 0, "supersede": 0, "tombstone": 0, "add": 0,
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


def _seed_live_debounce_state(mod, *, week_start_date, week_end_at,
                              baseline_pct, first_zero_at_utc):
    """Arm the LIVE index's #750 S3 debounce row.

    The pre-#750 form of these tests wrote `APP_DIR/pending-reset-zero-7d` and
    asserted the planner left the bytes alone. That file is retired; the state
    is now a `weekly_reset_debounce_state` row, and the planner's isolation
    property is that its scratch index carries the replayed state while the
    live row is untouched. Returns a reader for the live row.
    """
    import _cctally_record as rec
    conn = mod.open_db()
    try:
        rec._arm_reset_debounce_state(
            conn, "unattributed", week_start_date=week_start_date,
            week_end_at=week_end_at, baseline_pct=baseline_pct,
            first_zero_at_utc=first_zero_at_utc,
            first_zero_observation_id=None)
        conn.commit()
    finally:
        conn.close()


def _live_debounce_state(mod):
    import _cctally_record as rec
    conn = mod.open_db()
    try:
        return rec._read_reset_debounce_state(conn, "unattributed")
    finally:
        conn.close()


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


def test_weekly_cost_snapshot_equivalent_offset_spelling_is_retained(
    cctally_module,
):
    import _lib_journal as journal
    import _lib_rederive as rederive

    event_id = "wcs:o:cost-offset:2026-08-29"
    at = "2026-09-01T19:07:54Z"
    common = {
        "account_key": "acct-a",
        "captured_at_utc": at,
        "cost_usd": 735.6763515000001,
        "mode": "auto",
        "project": None,
        "week_end_at": "2026-09-05T05:00:00+00:00",
        "week_end_date": "2026-09-05",
        "week_start_at": "2026-08-29T05:00:00+00:00",
        "week_start_date": "2026-08-29",
    }
    current = journal.make_evt(
        kind="weekly_cost_snapshot", id=event_id, at=at,
        payload={
            **common,
            "range_start_iso": "2026-08-29T08:00:00+03:00",
            "range_end_iso": "2026-09-01T22:07:54+03:00",
        },
    )
    desired = journal.make_evt(
        kind="weekly_cost_snapshot", id=event_id, at=at,
        payload={
            **common,
            "range_start_iso": "2026-08-28T22:00:00-07:00",
            "range_end_iso": "2026-09-01T12:07:54-07:00",
        },
    )

    plan = rederive.build_claude_usage_plan(
        selection=journal.resolve_effective_events([current]),
        desired_events=[desired],
        journal_high_water=("observations-2026-09.jsonl", 1),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=(),
    )

    assert plan.actions == ()
    assert plan.counts == {
        "retain": 1, "supersede": 0, "tombstone": 0, "add": 0,
    }


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
    _seed_live_debounce_state(
        mod, week_start_date="2026-07-20",
        week_end_at="2026-07-27T00:00:00+00:00", baseline_pct=50.0,
        first_zero_at_utc="2026-07-25T12:30:00+00:00")
    hwm5 = mod.APP_DIR / "hwm-5h"
    hwm5.write_text("sentinel-window 99\n")
    before_state = _live_debounce_state(mod)
    before_hwm5 = hwm5.read_bytes()

    repeated = mod.plan_claude_usage_rederive(
        [first, credit],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 900),
    )

    assert repeated.to_bytes() == desired.to_bytes()
    assert _live_debounce_state(mod) == before_state
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
    _seed_live_debounce_state(
        mod, week_start_date="unrelated",
        week_end_at="2026-07-28T00:00:00+00:00", baseline_pct=99.0,
        first_zero_at_utc="2026-07-25T11:00:00+00:00")
    hwm5 = mod.APP_DIR / "hwm-5h"
    hwm5.write_text("sentinel-window 99\n")
    before_state = _live_debounce_state(mod)
    before_hwm5 = hwm5.read_bytes()

    first = mod.plan_claude_usage_rederive(
        records,
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1000),
    )
    _seed_live_debounce_state(
        mod, week_start_date="different",
        week_end_at="2026-07-29T00:00:00+00:00", baseline_pct=88.0,
        first_zero_at_utc="2026-07-25T10:00:00+00:00")
    changed_external_state = _live_debounce_state(mod)
    second = mod.plan_claude_usage_rederive(
        records,
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1000),
    )

    assert first.to_bytes() == second.to_bytes()
    assert {
        (action.payload or {}).get("kind") for action in first.actions
    } >= {"week_reset", "five_hour_credit"}
    assert _live_debounce_state(mod) == changed_external_state
    assert hwm5.read_bytes() == before_hwm5
    assert before_state != changed_external_state
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


def test_replay_of_repeated_high_low_observations_refuses_reset_burst(
    tmp_path, monkeypatch,
):
    """Exercise the real scratch pipeline, not a fabricated desired event set."""
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal
    import _lib_rederive as rederive

    cache = _seed_cache(mod)
    base_payload = dict(_raw_obs(journal)["payload"])
    records = []
    for minute, weekly in enumerate((60.0, 10.0, 60.0, 10.0, 60.0, 10.0)):
        captured = f"2026-07-25T12:{minute:02d}:00Z"
        payload = dict(base_payload)
        payload["captured_at"] = captured
        payload["weekly_percent"] = weekly
        records.append(journal.make_obs(
            at=captured,
            src="record-usage",
            provider="claude",
            account="acct-a",
            payload=payload,
        ))

    try:
        with pytest.raises(rederive.RederivePlanGuardConflict) as caught:
            mod.plan_claude_usage_rederive(
                records,
                cache_conn=cache,
                journal_high_water=("observations-2026-07.jsonl", 900),
            )
    finally:
        cache.close()
    assert caught.value.plan_guard["code"] == "week-reset-add-burst"
    assert caught.value.plan_guard["violations"] == [{
        "accountKey": "acct-a",
        "newWeekEndAt": "2026-07-27T00:00:00+00:00",
        "observedPreCreditPct": 60.0,
        "addCount": 3,
    }]


def _reviewed_weekly_obs(journal, minute, weekly, week_end_day, five_hour):
    captured = f"2026-07-25T12:{minute:02d}:00Z"
    payload = dict(_raw_obs(journal)["payload"])
    payload.update({
        "captured_at": captured,
        "weekly_percent": weekly,
        "resets_at": int(dt.datetime(
            2026, 7, week_end_day, tzinfo=dt.timezone.utc).timestamp()),
        "five_hour_percent": five_hour,
        "five_hour_resets_at": "2026-07-25T16:00:00Z",
    })
    return journal.make_obs(
        at=captured, src="record-usage", provider="claude",
        account="acct-a", payload=payload,
    )


def test_reviewed_weekly_hold_stops_stale_storm_but_not_genuine_reset(
    tmp_path, monkeypatch,
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_rederive as rederive
    import _lib_journal as journal

    cache = _seed_cache(mod)
    records = [
        _reviewed_weekly_obs(journal, 0, 60, 27, 20),
        _reviewed_weekly_obs(journal, 1, 10, 28, 21),
        _reviewed_weekly_obs(journal, 2, 60, 27, 22),
        _reviewed_weekly_obs(journal, 3, 10, 28, 23),
        _reviewed_weekly_obs(journal, 4, 60, 27, 24),
        _reviewed_weekly_obs(journal, 5, 10, 28, 25),
        _reviewed_weekly_obs(journal, 6, 60, 28, 26),
        _reviewed_weekly_obs(journal, 7, 10, 29, 27),
    ]
    held_ids = frozenset(record["id"] for record in records[2:6])
    try:
        events = rederive._derive_desired_events(
            records, cache, tmp_path,
            held_weekly_observation_ids=held_ids,
        )
    finally:
        cache.close()

    resets = [event for event in events
              if (event.get("payload") or {}).get("kind") == "week_reset"]
    assert len(resets) == 2
    assert {event["payload"]["new_week_end_at"] for event in resets} == {
        "2026-07-28T00:00:00+00:00",
        "2026-07-29T00:00:00+00:00",
    }
    for record in records[2:6]:
        snapshot = next(event for event in events
                        if event["id"] == f"sa:{record['id']}")
        assert snapshot["payload"]["weekly_observation_held"] == 1
        assert snapshot["payload"]["weekly_percent"] == 10.0
        assert snapshot["payload"]["week_end_at"] == "2026-07-28T00:00:00+00:00"
        raw = json.loads(snapshot["payload"]["payload_json"])
        assert raw["rawWeeklyPercent"] == record["payload"]["weekly_percent"]
        assert raw["rawWeekEndAt"].startswith(
            f"2026-07-{27 if record['payload']['weekly_percent'] == 60 else 28}"
        )
        assert snapshot["payload"]["five_hour_percent"] == record["payload"]["five_hour_percent"]


def test_reviewed_weekly_high_retains_five_hour_credit_without_milestone(
    tmp_path, monkeypatch,
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_rederive as rederive
    import _lib_journal as journal

    cache = _seed_cache(mod)
    records = [
        _reviewed_weekly_obs(journal, 0, 10, 27, 20),
        _reviewed_weekly_obs(journal, 1, 60, 28, 5),
        _reviewed_weekly_obs(journal, 2, 60, 28, 5),
        _reviewed_weekly_obs(journal, 3, 60, 28, 6),
    ]
    try:
        events = rederive._derive_desired_events(
            records, cache, tmp_path,
            held_weekly_observation_ids=frozenset(
                record["id"] for record in records[1:]),
        )
    finally:
        cache.close()
    kinds = [(event.get("payload") or {}).get("kind") for event in events]
    assert "five_hour_credit" in kinds
    assert "week_reset" not in kinds
    assert not any(kind == "percent_milestone" and
                   event["payload"].get("percent_threshold", 0) > 10
                   for event, kind in zip(events, kinds))
    held = [event for event in events
            if event["id"] in {f"sa:{record['id']}" for record in records[1:]}]
    assert len(held) == 2
    assert f"sa:{records[1]['id']}" not in {event["id"] for event in events}
    assert all(event["payload"]["weekly_observation_held"] == 1 for event in held)
    assert all(json.loads(event["payload"]["payload_json"])["rawWeeklyPercent"] == 60
               for event in held)


def test_reviewed_weekly_hold_keeps_genuine_second_in_place_reset(
    tmp_path, monkeypatch,
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_rederive as rederive
    import _lib_journal as journal

    cache = _seed_cache(mod)
    records = [
        _reviewed_weekly_obs(journal, 0, 60, 27, 20),
        _reviewed_weekly_obs(journal, 1, 10, 27, 21),
        _reviewed_weekly_obs(journal, 2, 60, 27, 22),
        _reviewed_weekly_obs(journal, 3, 10, 27, 23),
        _reviewed_weekly_obs(journal, 4, 60, 27, 24),
        _reviewed_weekly_obs(journal, 5, 10, 27, 25),
    ]
    try:
        events = rederive._derive_desired_events(
            records, cache, tmp_path,
            held_weekly_observation_ids=frozenset(
                {records[2]["id"], records[3]["id"]}),
        )
    finally:
        cache.close()
    resets = [event for event in events
              if (event.get("payload") or {}).get("kind") == "week_reset"]
    assert len(resets) == 2
    assert {event["payload"]["origin_observation_id"] for event in resets} == {
        records[1]["id"], records[5]["id"],
    }
    assert all(event["payload"]["new_week_end_at"]
               == "2026-07-27T00:00:00+00:00" for event in resets)


def test_reviewed_weekly_low_does_not_mint_in_place_credit(
    tmp_path, monkeypatch,
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_rederive as rederive
    import _lib_journal as journal

    cache = _seed_cache(mod)
    first = _reviewed_weekly_obs(journal, 0, 60, 27, 20)
    held_low = _reviewed_weekly_obs(journal, 1, 10, 27, 21)
    try:
        events = rederive._derive_desired_events(
            [first, held_low], cache, tmp_path,
            held_weekly_observation_ids=frozenset({held_low["id"]}),
        )
    finally:
        cache.close()
    assert not any((event.get("payload") or {}).get("kind") == "week_reset"
                   for event in events)
    snapshot = next(event for event in events
                    if event["id"] == f"sa:{held_low['id']}")
    assert snapshot["payload"]["weekly_percent"] == 60.0
    assert snapshot["payload"]["five_hour_percent"] == 21.0
    assert json.loads(snapshot["payload"]["payload_json"])["rawWeeklyPercent"] == 10.0


def test_reviewed_weekly_hold_without_account_basis_fails_closed(
    tmp_path, monkeypatch,
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_rederive as rederive
    import _lib_journal as journal
    import _lib_rederive

    cache = _seed_cache(mod)
    record = _reviewed_weekly_obs(journal, 0, 60, 28, 5)
    try:
        with pytest.raises(_lib_rederive.RederiveConflict, match="weekly.*basis"):
            rederive._derive_desired_events(
                [record], cache, tmp_path,
                held_weekly_observation_ids=frozenset({record["id"]}),
            )
    finally:
        cache.close()


def test_reviewed_weekly_hold_requires_a_raw_weekly_capture(
    tmp_path, monkeypatch,
):
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_rederive as rederive
    import _lib_journal as journal
    import _lib_rederive

    cache = _seed_cache(mod)
    payload = dict(_reviewed_weekly_obs(journal, 0, 60, 28, 5)["payload"])
    del payload["weekly_percent"]
    record = journal.make_obs(
        at=AT, src="record-usage", provider="claude",
        account="acct-a", payload=payload,
    )
    try:
        with pytest.raises(_lib_rederive.RederiveConflict,
                           match="Claude raw observation"):
            rederive._derive_desired_events(
                [record], cache, tmp_path,
                held_weekly_observation_ids=frozenset({record["id"]}),
            )
    finally:
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


def _week_reset(journal, event_id, *, account="acct-a",
                week_end="2026-07-27T00:00:00Z", percent=60.0):
    return journal.make_evt(
        kind="week_reset", id=event_id, at=AT,
        payload={
            "account_key": account,
            "new_week_end_at": week_end,
            "observed_pre_credit_pct": percent,
        },
    )


def test_week_reset_add_burst_refuses_three_matching_adds_with_real_plan(
    cctally_module,
):
    import _lib_journal as journal
    import _lib_rederive as rederive

    desired = [_week_reset(journal, f"wr:{i}") for i in range(3)]
    selection = journal.resolve_effective_events([])

    with pytest.raises(rederive.RederivePlanGuardConflict) as caught:
        _plan(rederive, selection, desired)

    error = caught.value
    assert error.plan.counts["add"] == 3
    assert error.plan.plan_hash.startswith("sha256:")
    assert error.plan_guard == {
        "code": "week-reset-add-burst",
        "limit": 2,
        "violations": [{
            "accountKey": "acct-a",
            "newWeekEndAt": "2026-07-27T00:00:00Z",
            "observedPreCreditPct": 60.0,
            "addCount": 3,
        }],
    }
    assert error.plan.action_counts_by_event_kind["week_reset"] == {
        "retain": 0, "supersede": 0, "tombstone": 0, "add": 3,
    }
    assert error.plan.action_counts_by_event_kind["five_hour_credit"] == {
        "retain": 0, "supersede": 0, "tombstone": 0, "add": 0,
    }


def test_week_reset_add_guard_allows_two_and_distinct_groups(cctally_module):
    import _lib_journal as journal
    import _lib_rederive as rederive

    selection = journal.resolve_effective_events([])
    two = [_week_reset(journal, f"wr:{i}") for i in range(2)]
    assert _plan(rederive, selection, two).counts["add"] == 2

    mixed = [
        *two,
        _week_reset(journal, "wr:other-account", account="acct-b"),
        _week_reset(journal, "wr:other-week", week_end="2026-08-03T00:00:00Z"),
        _week_reset(journal, "wr:other-percent", percent=61.0),
        _week_reset(journal, "wr:missing-key", account=None),
        journal.make_evt(kind="five_hour_credit", id="fhc:1", at=AT,
                         payload={"account_key": "acct-a"}),
    ]
    plan = _plan(rederive, selection, mixed)
    assert plan.counts["add"] == 7
    assert plan.action_counts_by_event_kind["week_reset"]["add"] == 6
    assert plan.action_counts_by_event_kind["five_hour_credit"]["add"] == 1


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


def test_closed_block_keeps_its_frozen_money_when_current_ownership_differs(
    cctally_module,
):
    """A later ownership rule cannot reprice a closure already in the journal."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    payload = {
        "account_key": "acct-a",
        "five_hour_window_key": 1785088200,
        "five_hour_resets_at": "2026-07-26T16:30:00Z",
        "block_start_at": "2026-07-26T14:30:00+03:00",
        "first_observed_at_utc": "2026-07-26T12:00:00Z",
        "last_observed_at_utc": "2026-07-26T16:10:00Z",
        "final_five_hour_percent": 42.0,
        "is_closed": 1,
        "total_cost_usd": 22.7088175,
        "total_input_tokens": 100,
        "_models": [{"model": "claude-opus-4", "cost_usd": 22.7088175}],
        "_projects": [{"project_path": "/repo", "cost_usd": 22.7088175}],
    }
    current = journal.make_evt(
        kind="five_hour_block_close", id="fhbc:successor", at=AT,
        payload=payload,
    )
    recomputed = journal.make_evt(
        kind="five_hour_block_close", id=current["id"], at=AT,
        payload={**payload, "block_start_at": "2026-07-26T11:30:00+00:00",
                 "total_cost_usd": 0.0, "total_input_tokens": 0,
                 "_models": [], "_projects": []},
    )
    kwargs = dict(
        selection=journal.resolve_effective_events([current]),
        journal_high_water=("observations-2026-07.jsonl", 10),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=(),
    )
    stable = rederive.build_claude_usage_plan(
        desired_events=[recomputed], **kwargs,
    )
    assert stable.actions == ()
    assert stable.counts["retain"] == 1

    # A changed closure boundary is a real correction, even if the price also
    # differs. The frozen-money rule must not hide it.
    corrected = rederive.build_claude_usage_plan(
        desired_events=[{
            **recomputed,
            "payload": {**recomputed["payload"],
                        "block_start_at": "2026-07-26T11:31:00+00:00"},
        }],
        **kwargs,
    )
    assert [(a.disposition, a.event_id) for a in corrected.actions] == [
        ("supersede", current["id"]),
    ]


def test_reviewed_weekly_hold_changes_only_a_frozen_blocks_weekly_axes(
    cctally_module,
):
    import _lib_journal as journal
    import _lib_rederive as rederive

    raw_id = "o:held"
    snapshot = journal.make_evt(
        kind="snapshot_accept", id=f"sa:{raw_id}", at=AT,
        payload={
            "account_key": "acct-a", "captured_at_utc": AT,
            "weekly_percent": 63.0, "five_hour_percent": 61.0,
            "five_hour_window_key": 1788570000,
        },
    )
    current = journal.make_evt(
        kind="five_hour_block_close", id="fhbc:acct-a:1788570000", at=AT,
        payload={
            "account_key": "acct-a", "five_hour_window_key": 1788570000,
            "block_start_at": "2026-09-04T20:00:00Z",
            "last_observed_at_utc": "2026-09-04T23:59:33Z",
            "final_five_hour_percent": 61.0,
            "seven_day_pct_at_block_start": 63.0,
            "seven_day_pct_at_block_end": 63.0,
            "crossed_seven_day_reset": 0, "is_closed": 1,
            "total_cost_usd": 474.296814,
            "total_input_tokens": 5678,
            "_models": [{"model": "claude-opus-5", "cost_usd": 474.296814}],
            "_projects": [{"project_path": "/repo", "cost_usd": 474.296814}],
        },
    )
    replayed = journal.make_evt(
        kind="five_hour_block_close", id=current["id"], at=AT,
        payload={
            **current["payload"],
            "last_observed_at_utc": "2026-09-05T00:58:26Z",
            "final_five_hour_percent": 71.0,
            "seven_day_pct_at_block_end": 15.0,
            "crossed_seven_day_reset": 1,
            "total_cost_usd": 317.49326275,
            "total_input_tokens": 3862,
            "_models": [{"model": "claude-opus-5", "cost_usd": 317.49326275}],
            "_projects": [{"project_path": "/repo", "cost_usd": 317.49326275}],
        },
    )
    plan = rederive.build_claude_usage_plan(
        selection=journal.resolve_effective_events([snapshot, current]),
        desired_events=[snapshot, replayed],
        journal_high_water=("observations-2026-09.jsonl", 10),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=(), reviewed_weekly_hold_ids=(raw_id,),
    )

    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert (action.disposition, action.event_id) == ("supersede", current["id"])
    expected = dict(current["payload"])
    expected.update({
        "seven_day_pct_at_block_end": 15.0,
        "crossed_seven_day_reset": 1,
    })
    assert action.payload == expected


def test_raw_replay_keeps_accepted_equal_snapshot_identity(tmp_path, monkeypatch):
    """Exercise the real scratch fold and planner, not only synthetic events."""
    mod = _isolated(tmp_path, monkeypatch)
    import _lib_journal as journal

    cache = _seed_cache(mod)
    base = _raw_obs(journal)

    def reading(at):
        return journal.make_obs(
            at=at, src="record-usage", provider="claude", account="acct-a",
            payload={**base["payload"], "captured_at": at},
        )

    later = reading("2026-07-25T12:00:05Z")
    earlier = reading("2026-07-25T12:00:02Z")
    assert earlier["id"] != later["id"]

    baseline = mod.plan_claude_usage_rederive(
        [later], cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1),
    )
    selected = []
    for action in baseline.actions:
        payload = dict(action.payload or {})
        selected.append(journal.make_evt(
            kind=payload.pop("kind"), id=action.event_id,
            at=action.at, payload=payload,
        ))
    assert any((event["payload"] or {}).get("kind") == "snapshot_accept"
               for event in selected)
    assert any((event["payload"] or {}).get("kind") == "percent_milestone"
               for event in selected)

    plan = mod.plan_claude_usage_rederive(
        [earlier, later, *selected], cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 2),
    )
    snapshot_actions = [
        action for action in plan.actions
        if (action.payload or {}).get("kind") == "snapshot_accept"
        or action.event_id.startswith("sa:")
    ]
    assert snapshot_actions == []
    milestone_actions = [a for a in plan.actions if a.event_id.startswith("pm:")]
    assert milestone_actions == []
    assert not any(a.event_id.startswith("wcs:") for a in plan.actions)
    assert plan.actions == ()
    cache.close()

    # Materialize the exact same retained prefix, then exercise the command,
    # rebuild read-back and an idempotent second preview on the isolated store.
    import _cctally_journal as runtime

    appended_at = dt.datetime(2026, 7, 25, 12, 1, tzinfo=dt.timezone.utc)
    for record in [earlier, later, *selected]:
        runtime.append_record(record, now_utc=appended_at)
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
    )
    conn = mod.open_db()
    try:
        refs = [tuple(row) for row in conn.execute(
            "SELECT s.journal_id, c.journal_id FROM percent_milestones m "
            "JOIN weekly_usage_snapshots s ON s.id=m.usage_snapshot_id "
            "JOIN weekly_cost_snapshots c ON c.id=m.cost_snapshot_id"
        ).fetchall()]
        assert refs == [(
            next(e["id"] for e in selected if e["id"].startswith("sa:")),
            next(e["id"] for e in selected if e["id"].startswith("wcs:")),
        )]
    finally:
        conn.close()
    args = argparse.Namespace(family="claude-usage", yes=True, json=True)
    assert mod.cmd_db_rederive(args) == 0
    assert mod.preview_db_rederive("claude-usage").plan.actions == ()


def test_held_replay_does_not_replace_the_later_genuine_journal_decision(
    cctally_module,
):
    """Real #858 shape: held statusline 10% precedes accepted API 11%."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    week_end = int(dt.datetime(
        2026, 8, 29, 5, 0, tzinfo=dt.timezone.utc,
    ).timestamp())
    new_at = "2026-08-23T00:20:25Z"
    old_at = "2026-08-23T00:20:27Z"
    new_raw = journal.make_obs(
        at=new_at, src="record-usage", provider="claude",
        account="c719887886403b0a1e3004e967dbd20e",
        payload={
            "captured_at": new_at, "source": "statusline",
            "weekly_percent": 10.0, "resets_at": week_end,
            "five_hour_percent": 1.0,
            "five_hour_resets_at": "2026-08-23T04:50:00+00:00",
        },
    )
    old_raw = journal.make_obs(
        at=old_at, src="record-usage", provider="claude",
        account="c719887886403b0a1e3004e967dbd20e",
        payload={
            "captured_at": old_at, "source": "api",
            "weekly_percent": 11.0, "resets_at": week_end - 1,
            "five_hour_percent": 1.0,
            "five_hour_resets_at": "2026-08-23T04:49:59+00:00",
        },
    )
    new_raw = {**new_raw, "id": "o:f1c3e45eafe02d65"}
    old_raw = {**old_raw, "id": "o:b8db6f3413ca6dd0"}
    common = {
        "account_key": "c719887886403b0a1e3004e967dbd20e",
        "week_start_date": "2026-08-22",
        "week_end_date": "2026-08-29",
        "week_start_at": "2026-08-22T05:00:00+00:00",
        "week_end_at": "2026-08-29T05:00:00+00:00",
        "weekly_percent": 11.0, "five_hour_percent": 1.0,
        "five_hour_window_key": 1785978600, "page_url": None,
    }
    old = journal.make_evt(
        kind="snapshot_accept", id=f"sa:{old_raw['id']}", at=old_at,
        payload={
            **common, "captured_at_utc": old_at, "source": "api",
            "five_hour_resets_at": "2026-08-23T04:49:59+00:00",
            "payload_json": json.dumps({
                "source": "api", "capturedAt": old_at,
                "weeklyPercent": 11.0, "fiveHourPercent": 1.0,
            }),
        },
    )
    new = journal.make_evt(
        kind="snapshot_accept", id=f"sa:{new_raw['id']}", at=new_at,
        payload={
            **common, "captured_at_utc": new_at, "source": "statusline",
            "five_hour_resets_at": "2026-08-23T04:50:00+00:00",
            "weekly_observation_held": 1,
            "payload_json": json.dumps({
                "source": "statusline", "capturedAt": new_at,
                "weeklyPercent": 11.0, "rawWeeklyPercent": 10.0,
                "weeklyObservationHeld": True, "fiveHourPercent": 1.0,
            }),
        },
    )
    old_cost_id = f"wcs:{old_raw['id']}:2026-08-22"
    new_cost_id = f"wcs:{new_raw['id']}:2026-08-22"
    cost_payload = {
        "account_key": "c719887886403b0a1e3004e967dbd20e",
        "week_start_date": "2026-08-22",
        "week_end_date": "2026-08-29", "cost_usd": 1.25,
        "range_start_iso": "2026-08-22T05:00:00+00:00",
    }
    old_cost = journal.make_evt(
        kind="weekly_cost_snapshot", id=old_cost_id, at=old_at,
        payload={**cost_payload, "captured_at_utc": old_at,
                 "range_end_iso": old_at},
    )
    new_cost = journal.make_evt(
        kind="weekly_cost_snapshot", id=new_cost_id, at=new_at,
        payload={**cost_payload, "captured_at_utc": new_at,
                 "range_end_iso": new_at},
    )
    milestone_id = "pm:c719887886403b0a1e3004e967dbd20e:2026-08-22:0:11"
    old_milestone = journal.make_evt(
        kind="percent_milestone", id=milestone_id, at=old_at,
        payload={
            "account_key": "c719887886403b0a1e3004e967dbd20e",
            "week_start_date": "2026-08-22",
            "percent_threshold": 11, "captured_at_utc": old_at,
            "usage_snapshot_ref": old["id"],
            "cost_snapshot_ref": old_cost_id,
            "cumulative_cost_usd": 1.25,
        },
    )
    new_milestone = journal.make_evt(
        kind="percent_milestone", id=milestone_id, at=new_at,
        payload={
            **old_milestone["payload"], "captured_at_utc": new_at,
            "usage_snapshot_ref": new["id"],
            "cost_snapshot_ref": new_cost_id,
        },
    )
    old_five_hour_milestone = journal.make_evt(
        kind="five_hour_milestone",
        id="fhm:c719887886403b0a1e3004e967dbd20e:1785978600:0:1",
        at=old_at,
        payload={
            "account_key": "c719887886403b0a1e3004e967dbd20e",
            "five_hour_window_key": 1785978600,
            "percent_threshold": 1, "captured_at_utc": old_at,
            "usage_snapshot_ref": old["id"],
            "block_cost_usd": 2.5, "seven_day_pct_at_crossing": 11.0,
            "input_tokens": 111, "output_tokens": 222,
            "cache_create_tokens": 333, "cache_read_tokens": 444,
        },
    )
    new_five_hour_milestone = journal.make_evt(
        kind="five_hour_milestone", id=old_five_hour_milestone["id"],
        at=new_at,
        payload={
            **old_five_hour_milestone["payload"],
            "captured_at_utc": new_at,
            "usage_snapshot_ref": new["id"],
            "block_cost_usd": 2.0, "seven_day_pct_at_crossing": 10.0,
            "input_tokens": 999, "output_tokens": 999,
            "cache_create_tokens": 999, "cache_read_tokens": 999,
        },
    )
    kwargs = dict(
        selection=journal.resolve_effective_events(
            [old, old_cost, old_milestone, old_five_hour_milestone],
        ),
        journal_high_water=("observations-2026-08.jsonl", 500),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=(),
        raw_observations=[new_raw, old_raw],
    )
    stable = rederive.build_claude_usage_plan(
        desired_events=[
            new, new_cost, new_milestone, new_five_hour_milestone,
        ], **kwargs,
    )
    assert stable.actions == ()
    assert stable.counts["retain"] == 4

    # A changed week boundary is not the same accepted decision.
    different = rederive.build_claude_usage_plan(
        desired_events=[{
            **new, "payload": {
                **new["payload"],
                "week_end_at": "2026-08-29T06:00:00+00:00",
            },
        }, new_cost, new_milestone, new_five_hour_milestone],
        **kwargs,
    )
    assert {a.disposition for a in different.actions
            if a.event_id.startswith("sa:")} == {"add", "tombstone"}

    # A retained old origin whose genuine weekly reading is different from
    # the selected event cannot be preserved by proximity or matching display.
    distinct_raw = journal.make_obs(
        at=old_at, src="record-usage", provider="claude",
        account="c719887886403b0a1e3004e967dbd20e",
        payload={**old_raw["payload"], "weekly_percent": 12.0},
    )
    distinct_old = {**old, "id": f"sa:{distinct_raw['id']}"}
    distinct = rederive.build_claude_usage_plan(
        selection=journal.resolve_effective_events([distinct_old]),
        desired_events=[new],
        journal_high_water=("observations-2026-08.jsonl", 500),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=(),
        raw_observations=[new_raw, distinct_raw],
    )
    assert {a.disposition for a in distinct.actions} == {"add", "tombstone"}

    # An unfamiliar causal edge cannot be silently redirected to the old id.
    extra_effect = journal.make_evt(
        kind="weekly_credit_effects", id="wce:other", at=new_at,
        payload={"suppression": [new["id"]]},
    )
    unknown_edge = rederive.build_claude_usage_plan(
        desired_events=[new, new_cost, new_milestone,
                        new_five_hour_milestone, extra_effect],
        **kwargs,
    )
    assert {a.disposition for a in unknown_edge.actions
            if a.event_id.startswith("sa:")} == {"add", "tombstone"}

    repeated_origin = rederive.build_claude_usage_plan(
        desired_events=[new, new_cost, new_milestone,
                        new_five_hour_milestone],
        **{**kwargs, "raw_observations": [new_raw, old_raw, new_raw]},
    )
    assert {a.disposition for a in repeated_origin.actions
            if a.event_id.startswith("sa:")} == {"add", "tombstone"}

    credit_between = journal.make_op(
        at="2026-08-23T00:20:26Z", src="record-credit",
        payload={
            "kind": "weekly_credit_floor",
            "account_key": "c719887886403b0a1e3004e967dbd20e",
            "week_start_date": "2026-08-22", "floor_percent": 10.0,
        },
    )
    separated_by_credit = rederive.build_claude_usage_plan(
        desired_events=[new, new_cost, new_milestone,
                        new_five_hour_milestone],
        **{**kwargs, "raw_observations": [new_raw, credit_between, old_raw]},
    )
    assert {a.disposition for a in separated_by_credit.actions
            if a.event_id.startswith("sa:")} == {"add", "tombstone"}

    lower_between = journal.make_obs(
        at="2026-08-23T00:20:26Z", src="record-usage",
        provider="claude", account="c719887886403b0a1e3004e967dbd20e",
        payload={
            **new_raw["payload"],
            "captured_at": "2026-08-23T00:20:26Z",
            "weekly_percent": 3.0,
            "five_hour_percent": 2.0,
        },
    )
    separated_by_observation = rederive.build_claude_usage_plan(
        desired_events=[new, new_cost, new_milestone,
                        new_five_hour_milestone],
        **{**kwargs, "raw_observations": [new_raw, lower_between, old_raw]},
    )
    assert {a.disposition for a in separated_by_observation.actions
            if a.event_id.startswith("sa:")} == {"add", "tombstone"}

    # The production August ordering contained two same-account API readings
    # between the replay candidate and the accepted snapshot. Automatic #858
    # reconciliation remains fail-closed; an exact reviewed pair may override
    # only that intervening-observation reason.
    api_45 = journal.make_obs(
        at="2026-08-23T00:20:26Z", src="record-usage",
        provider="claude", account="c719887886403b0a1e3004e967dbd20e",
        payload={
            **new_raw["payload"],
            "captured_at": "2026-08-23T00:20:26Z",
            "source": "api", "weekly_percent": 45.0,
        },
    )
    api_0 = journal.make_obs(
        at="2026-08-23T00:20:26.500000Z", src="record-usage",
        provider="claude", account="c719887886403b0a1e3004e967dbd20e",
        payload={
            **new_raw["payload"],
            "captured_at": "2026-08-23T00:20:26.500000Z",
            "source": "api", "weekly_percent": 0.0,
        },
    )
    august_raw = [new_raw, api_45, api_0, old_raw]
    automatic = rederive.build_claude_usage_plan(
        desired_events=[new, new_cost, new_milestone,
                        new_five_hour_milestone],
        **{**kwargs, "raw_observations": august_raw},
    )
    assert {a.disposition for a in automatic.actions
            if a.event_id.startswith("sa:")} == {"add", "tombstone"}

    reviewed = rederive.build_claude_usage_plan(
        desired_events=[new, new_cost, new_milestone,
                        new_five_hour_milestone],
        snapshot_identity_decisions=((old_raw["id"], new_raw["id"]),),
        **{**kwargs, "raw_observations": august_raw},
    )
    assert reviewed.actions == ()
    assert reviewed.counts["retain"] == 4

    held_dependency = rederive.build_claude_usage_plan(
        desired_events=[new, new_cost, new_milestone,
                        new_five_hour_milestone],
        reviewed_weekly_hold_ids=(old_raw["id"],),
        **{**kwargs, "raw_observations": august_raw},
    )
    held_action_ids = {action.event_id for action in held_dependency.actions}
    assert old["id"] not in held_action_ids
    assert old_cost["id"] not in held_action_ids
    assert old_milestone["id"] not in held_action_ids
    assert old_five_hour_milestone["id"] not in held_action_ids

    introduced_milestone = journal.make_evt(
        kind="percent_milestone", id="pm:reviewed-reset:11", at=old_at,
        payload={
            **old_milestone["payload"],
            "usage_snapshot_ref": old["id"],
            "cost_snapshot_ref": old_cost["id"],
        },
    )
    dependency_kwargs = dict(
        selection=journal.resolve_effective_events([old, old_cost]),
        journal_high_water=kwargs["journal_high_water"],
        cache_fingerprint=kwargs["cache_fingerprint"],
        preserved_events=(), raw_observations=[old_raw],
        enforce_guard=False,
    )
    baseline_dependency = rederive.build_claude_usage_plan(
        desired_events=[
            {**old, "payload": {**old["payload"], "weekly_percent": 12.0}},
            {**old_cost, "payload": {
                **old_cost["payload"], "cost_usd": 2.0,
            }},
        ],
        config_fingerprint="sha256:baseline", **dependency_kwargs,
    )
    reviewed_dependency = rederive.build_claude_usage_plan(
        desired_events=[introduced_milestone],
        config_fingerprint="sha256:reviewed", **dependency_kwargs,
    )
    causal_dependency = rederive.causal_delta_plan(
        baseline_dependency, reviewed_dependency,
        current_events={old["id"]: old, old_cost["id"]: old_cost},
    )
    assert [action.event_id for action in causal_dependency.actions] == [
        introduced_milestone["id"],
    ]

    missing_dependency_kwargs = {
        **dependency_kwargs,
        "selection": journal.resolve_effective_events([]),
    }
    baseline_missing_dependency = rederive.build_claude_usage_plan(
        desired_events=[old, old_cost],
        config_fingerprint="sha256:baseline-missing",
        **missing_dependency_kwargs,
    )
    reviewed_missing_dependency = rederive.build_claude_usage_plan(
        desired_events=[old, old_cost, introduced_milestone],
        config_fingerprint="sha256:reviewed-missing",
        **missing_dependency_kwargs,
    )
    causal_missing_dependency = rederive.causal_delta_plan(
        baseline_missing_dependency, reviewed_missing_dependency,
        current_events={},
    )
    assert {action.event_id for action in causal_missing_dependency.actions} == {
        old["id"], old_cost["id"], introduced_milestone["id"],
    }

    sentinel_milestone = {
        **introduced_milestone,
        "id": "pm:reviewed-reset:sentinel",
        "payload": {
            **introduced_milestone["payload"],
            "cost_snapshot_ref": "0",
        },
    }
    sentinel_kwargs = {
        **dependency_kwargs,
        "selection": journal.resolve_effective_events([old]),
    }
    baseline_sentinel = rederive.build_claude_usage_plan(
        desired_events=[{
            **old, "payload": {**old["payload"], "weekly_percent": 12.0},
        }],
        config_fingerprint="sha256:baseline-sentinel", **sentinel_kwargs,
    )
    reviewed_sentinel = rederive.build_claude_usage_plan(
        desired_events=[sentinel_milestone],
        config_fingerprint="sha256:reviewed-sentinel", **sentinel_kwargs,
    )
    causal_sentinel = rederive.causal_delta_plan(
        baseline_sentinel, reviewed_sentinel,
        current_events={old["id"]: old},
    )
    assert [action.event_id for action in causal_sentinel.actions] == [
        sentinel_milestone["id"],
    ]

    held_sentinel = rederive.build_claude_usage_plan(
        selection=journal.resolve_effective_events([old, sentinel_milestone]),
        desired_events=[new],
        journal_high_water=kwargs["journal_high_water"],
        cache_fingerprint=kwargs["cache_fingerprint"],
        config_fingerprint=kwargs["config_fingerprint"],
        preserved_events=(), raw_observations=august_raw,
        reviewed_weekly_hold_ids=(old_raw["id"],),
    )
    assert sentinel_milestone["id"] not in {
        action.event_id for action in held_sentinel.actions
    }

    reset = journal.make_evt(
        kind="week_reset", id="wr:reviewed-held", at=old_at,
        payload={
            "account_key": "c719887886403b0a1e3004e967dbd20e",
            "origin_observation_id": old_raw["id"],
            "new_week_end_at": "2026-08-29T05:00:00+00:00",
            "observed_pre_credit_pct": 11.0,
        },
    )
    reset_milestone = {
        **old_milestone,
        "id": "pm:reviewed-held-reset:11",
        "payload": {
            **old_milestone["payload"],
            "reset_event_ref": reset["id"],
        },
    }
    reset_selection = journal.resolve_effective_events([
        old, old_cost, reset, reset_milestone,
    ])
    reset_kwargs = dict(
        selection=reset_selection,
        journal_high_water=kwargs["journal_high_water"],
        cache_fingerprint=kwargs["cache_fingerprint"],
        preserved_events=(), raw_observations=august_raw,
        enforce_guard=False,
    )
    baseline_reset = rederive.build_claude_usage_plan(
        desired_events=[old, old_cost, reset, reset_milestone],
        config_fingerprint="sha256:baseline-reset", **reset_kwargs,
    )
    reviewed_reset = rederive.build_claude_usage_plan(
        desired_events=[new],
        config_fingerprint="sha256:reviewed-reset",
        reviewed_weekly_hold_ids=(old_raw["id"],),
        **reset_kwargs,
    )
    assert reset["id"] not in {
        action.event_id for action in reviewed_reset.actions
    }
    causal_reset = rederive.causal_delta_plan(
        baseline_reset, reviewed_reset,
        current_events={
            record["id"]: record
            for record in (old, old_cost, reset, reset_milestone)
        },
    )
    assert reset["id"] not in {
        action.event_id for action in causal_reset.actions
    }

    with pytest.raises(rederive.RederiveConflict,
                       match="snapshot identity decision"):
        rederive.build_claude_usage_plan(
            desired_events=[{
                **new,
                "payload": {**new["payload"], "weekly_percent": 12.0},
            }, new_cost, new_milestone, new_five_hour_milestone],
            snapshot_identity_decisions=((old_raw["id"], new_raw["id"]),),
            **{**kwargs, "raw_observations": august_raw},
        )

    with pytest.raises(rederive.RederiveConflict,
                       match="snapshot identity decision"):
        rederive.build_claude_usage_plan(
            desired_events=[new, new_cost, new_milestone,
                            new_five_hour_milestone, extra_effect],
            snapshot_identity_decisions=((old_raw["id"], new_raw["id"]),),
            **{**kwargs, "raw_observations": august_raw},
        )

    dangling = journal.make_evt(
        kind="weekly_credit_effects", id="wce:dangling", at=old_at,
        payload={"suppression": [old["id"]]},
    )
    with pytest.raises(rederive.RederiveConflict,
                       match="snapshot identity decision"):
        rederive.build_claude_usage_plan(
            selection=journal.resolve_effective_events([
                old, old_cost, old_milestone, old_five_hour_milestone,
                dangling,
            ]),
            desired_events=[new, new_cost, new_milestone,
                            new_five_hour_milestone],
            journal_high_water=kwargs["journal_high_water"],
            cache_fingerprint=kwargs["cache_fingerprint"],
            config_fingerprint=kwargs["config_fingerprint"],
            preserved_events=(), raw_observations=august_raw,
            snapshot_identity_decisions=((old_raw["id"], new_raw["id"]),),
        )

    held_suppression = rederive.build_claude_usage_plan(
        selection=journal.resolve_effective_events([old, dangling]),
        desired_events=[new, dangling],
        journal_high_water=kwargs["journal_high_water"],
        cache_fingerprint=kwargs["cache_fingerprint"],
        config_fingerprint=kwargs["config_fingerprint"],
        preserved_events=(), raw_observations=august_raw,
        reviewed_weekly_hold_ids=(old_raw["id"],),
    )
    assert {
        action.event_id: action.disposition
        for action in held_suppression.actions
    } == {old["id"]: "tombstone", new["id"]: "add"}

    with pytest.raises(
        rederive.RederiveConflict,
        match="suppression would leave a dependent milestone",
    ):
        rederive.build_claude_usage_plan(
            selection=journal.resolve_effective_events([
                old, old_milestone, dangling,
            ]),
            desired_events=[new, old_milestone, dangling],
            journal_high_water=kwargs["journal_high_water"],
            cache_fingerprint=kwargs["cache_fingerprint"],
            config_fingerprint=kwargs["config_fingerprint"],
            preserved_events=(), raw_observations=august_raw,
            reviewed_weekly_hold_ids=(old_raw["id"],),
        )

    unfamiliar = journal.make_evt(
        kind="weekly_credit_effects", id="wce:unfamiliar", at=old_at,
        payload={"unfamiliar_snapshot_ref": old["id"]},
    )
    with pytest.raises(rederive.RederiveConflict,
                       match="reviewed weekly hold dependency"):
        rederive.build_claude_usage_plan(
            selection=journal.resolve_effective_events([old, unfamiliar]),
            desired_events=[new, unfamiliar],
            journal_high_water=kwargs["journal_high_water"],
            cache_fingerprint=kwargs["cache_fingerprint"],
            config_fingerprint=kwargs["config_fingerprint"],
            preserved_events=(), raw_observations=august_raw,
            reviewed_weekly_hold_ids=(old_raw["id"],),
        )

    with pytest.raises(rederive.RederiveConflict,
                       match="exactly once"):
        rederive.build_claude_usage_plan(
            desired_events=[new, new_cost, new_milestone,
                            new_five_hour_milestone],
            snapshot_identity_decisions=((old_raw["id"], new_raw["id"]),),
            **{**kwargs, "raw_observations": [*august_raw, new_raw]},
        )


def test_reviewed_causal_delta_excludes_three_older_block_closes(
    cctally_module,
):
    import _lib_journal as journal
    import _lib_rederive as rederive

    blocks = []
    for key in (1786389600, 1787239200, 1787406600):
        blocks.append(journal.make_evt(
            kind="five_hour_block_close",
            id=f"fhbc:acct-a:{key}", at="2026-08-20T00:00:00Z",
            payload={
                "account_key": "acct-a", "five_hour_window_key": key,
                "block_start_at": "2026-08-20T00:00:00+00:00",
                "total_cost_usd": 12.34, "total_input_tokens": 567,
                "_models": [{"model": "claude-opus-4", "cost_usd": 12.34}],
                "_projects": [{"project_path": "/kept", "cost_usd": 12.34}],
            },
        ))
    decision = journal.make_evt(
        kind="snapshot_accept", id="sa:o:reviewed", at=AT,
        payload={
            "account_key": "acct-a", "week_start_date": "2026-07-20",
            "week_start_at": "2026-07-20T00:00:00+00:00",
            "week_end_at": "2026-07-27T00:00:00+00:00",
            "weekly_percent": 10.0, "five_hour_percent": None,
            "five_hour_window_key": None, "weekly_observation_held": 0,
            "source": "statusline",
        },
    )
    kwargs = dict(
        selection=journal.resolve_effective_events([]),
        journal_high_water=("observations-2026-08.jsonl", 1),
        cache_fingerprint="sha256:cache",
        preserved_events=(), enforce_guard=False,
    )
    baseline = rederive.build_claude_usage_plan(
        desired_events=blocks, config_fingerprint="sha256:baseline", **kwargs,
    )
    reviewed = rederive.build_claude_usage_plan(
        desired_events=[*blocks, decision],
        config_fingerprint="sha256:reviewed", **kwargs,
    )
    causal = rederive.causal_delta_plan(
        baseline, reviewed, current_events={},
    )
    assert [action.event_id for action in causal.actions] == [decision["id"]]
    assert not any(action.event_id in {block["id"] for block in blocks}
                   for action in causal.actions)

    reset = journal.make_evt(
        kind="week_reset", id="wr:acct-a:origin:o:accepted", at=AT,
        payload={
            "account_key": "acct-a", "origin_observation_id": "o:accepted",
            "new_week_end_at": "2026-07-27T00:00:00+00:00",
            "observed_pre_credit_pct": 60.0,
        },
    )
    baseline_with_reset = rederive.build_claude_usage_plan(
        desired_events=[*blocks, reset],
        config_fingerprint="sha256:baseline", **kwargs,
    )
    reviewed_with_reset = rederive.build_claude_usage_plan(
        desired_events=[*blocks, reset, decision],
        config_fingerprint="sha256:reviewed", **kwargs,
    )
    authorized = rederive.causal_delta_plan(
        baseline_with_reset, reviewed_with_reset,
        current_events={},
        authorized_week_reset_origins={"o:accepted"},
    )
    assert [action.event_id for action in authorized.actions] == [
        decision["id"], reset["id"],
    ]
