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


# ---------------------------------------------------------------------------
# #875: five-hour milestones under a frozen close keep their block cost
# ---------------------------------------------------------------------------

_875_WINDOW = 1785088200
_875_ACCOUNT = "acct-875"
_875_CLOSE_ID = f"fhbc:{_875_ACCOUNT}:{_875_WINDOW}"
_875_CREDIT_REF = (
    f"fhc:{_875_ACCOUNT}:{_875_WINDOW}:2026-07-26T13:00:00+00:00"
)


def _875_close(journal, *, total, models, pricing=None, **overrides):
    payload = {
        "account_key": _875_ACCOUNT,
        "five_hour_window_key": _875_WINDOW,
        "five_hour_resets_at": "2026-07-26T16:30:00Z",
        "block_start_at": "2026-07-26T11:30:00Z",
        "first_observed_at_utc": "2026-07-26T12:00:00Z",
        "last_observed_at_utc": "2026-07-26T16:10:00Z",
        "final_five_hour_percent": 42.0,
        "is_closed": 1,
        "total_cost_usd": total,
        "total_input_tokens": 300,
        "_models": models,
        "_projects": [{"project_path": "/repo", "cost_usd": total}],
    }
    if pricing is not None:
        payload["_pricing"] = pricing
    payload.update(overrides)
    return journal.make_evt(
        kind="five_hour_block_close", id=_875_CLOSE_ID, at=AT,
        payload=payload,
    )


def _875_milestone(journal, threshold, block_cost, marginal, *,
                   reset_ref="0", tokens=None, alerted_at=None, at=AT,
                   captured=None, event_id=None):
    return journal.make_evt(
        kind="five_hour_milestone",
        id=event_id or (
            f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:{reset_ref}:{threshold}"
        ),
        at=at,
        payload={
            "account_key": _875_ACCOUNT,
            "five_hour_window_key": _875_WINDOW,
            "percent_threshold": threshold,
            "reset_event_ref": reset_ref,
            "captured_at_utc": captured or f"2026-07-26T12:{threshold:02d}:00Z",
            "usage_snapshot_ref": f"sa:o:875-{reset_ref}-{threshold}",
            "block_input_tokens": 100 * threshold if tokens is None else tokens,
            "block_output_tokens": 0,
            "block_cache_create_tokens": 0,
            "block_cache_read_tokens": 0,
            "block_cost_usd": block_cost,
            "marginal_cost_usd": marginal,
            "seven_day_pct_at_crossing": 1.0,
            "alerted_at": alerted_at,
        },
    )


def _875_priced_close(journal, total):
    return _875_close(
        journal, total=total,
        models=[{"model": "m", "cost_usd": total, "input_tokens": 300}],
    )


def _875_plan(rederive, journal, current, desired, *, preserved=(),
              **kwargs):
    return rederive.build_claude_usage_plan(
        selection=journal.resolve_effective_events(list(current)),
        desired_events=list(desired),
        journal_high_water=("observations-2026-07.jsonl", 10),
        cache_fingerprint="sha256:cache",
        config_fingerprint="sha256:config",
        preserved_events=tuple(preserved),
        **kwargs,
    )


def _875_milestone_actions(plan):
    return {
        action.event_id: action for action in plan.actions
        if action.kind == "five_hour_milestone"
    }


def _875_money(action):
    payload = action.payload or {}
    return payload["block_cost_usd"], payload["marginal_cost_usd"]


def _875_corrected_state(journal, current, plan):
    """The effective records after applying ``plan``, re-spelled at rev 0.

    A second plan over this state must be a no-op. Rev 0 keeps the check free
    of correction-batch machinery; only the payloads matter here.
    """
    by_id = {record["id"]: record for record in current}
    for action in plan.actions:
        if action.disposition == "tombstone":
            by_id.pop(action.event_id, None)
            continue
        payload = dict(action.payload or {})
        kind = payload.pop("kind")
        by_id[action.event_id] = journal.make_evt(
            kind=kind, id=action.event_id, at=action.at, payload=payload,
        )
    return list(by_id.values())


def _875_kept_close_kinds(rederive):
    """(current close kwargs, desired close kwargs) per kept-close kind."""
    priced_models = [{"model": "m", "cost_usd": 6.0, "input_tokens": 300}]
    repriced_models = [{"model": "m", "cost_usd": 14.0, "input_tokens": 300}]
    repriced = dict(
        total=14.0, models=repriced_models,
        pricing={"version": 1, "unpricedModels": []},
    )
    return {
        "genuinely-priced": (
            dict(total=6.0, models=priced_models), repriced,
        ),
        "unmarked-uncertain": (
            dict(total=0.0, models=[
                {"model": "m", "cost_usd": 0.0, "input_tokens": 300},
            ]),
            repriced,
        ),
        "partial-missing-card": (
            dict(total=6.0, models=[
                {"model": "m", "cost_usd": 6.0, "input_tokens": 200},
                {"model": "m2", "cost_usd": 0.0, "input_tokens": 100},
            ], pricing={"version": 1, "unpricedModels": ["m2"]}),
            dict(total=14.0, models=[
                {"model": "m", "cost_usd": 9.0, "input_tokens": 200},
                {"model": "m2", "cost_usd": 5.0, "input_tokens": 100},
            ], pricing={"version": 1, "unpricedModels": []}),
        ),
        "population-drifted": (
            dict(total=0.0, models=[
                {"model": "m", "cost_usd": 0.0, "input_tokens": 300},
            ], pricing={"version": 1, "unpricedModels": ["m"]}),
            dict(total=16.0, models=[
                {"model": "m", "cost_usd": 16.0, "input_tokens": 350},
            ], pricing={"version": 1, "unpricedModels": []}),
        ),
        "869-corrected": (
            dict(total=6.0, models=priced_models, pricing={
                "version": 1, "unpricedModels": [],
                "basis": rederive.RECORDED_CLOSE_CORRECTION_BASIS,
            }),
            repriced,
        ),
        "869-historical": (
            dict(total=6.0, models=priced_models, pricing={
                "version": 1, "unpricedModels": [],
                "basis": rederive.HISTORICAL_CLOSE_BASIS,
            }),
            repriced,
        ),
    }


@pytest.mark.parametrize("close_kind", [
    "genuinely-priced", "unmarked-uncertain", "partial-missing-card",
    "population-drifted", "869-corrected", "869-historical",
])
def test_875_price_only_edit_keeps_milestone_money_under_every_kept_close(
    cctally_module, close_kind,
):
    """Spec §6.1 test 1 (A1, A7): the close is kept, so both fields stay."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    current_kwargs, desired_kwargs = _875_kept_close_kinds(rederive)[close_kind]
    current = [
        _875_close(journal, **current_kwargs),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    desired = [
        _875_close(journal, **desired_kwargs),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    plan = _875_plan(rederive, journal, current, desired)

    assert plan.actions == (), [
        (a.disposition, a.event_id, a.payload) for a in plan.actions
    ]


def test_875_predecessor_identity_correction_updates_successor_marginal(
    cctally_module,
):
    """Spec §6.1 test 2 (A1, A6): the live 254-case chain, no card edit."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 20.0),
        _875_milestone(journal, 1, 5.0, None),
        _875_milestone(journal, 2, 8.1945, 3.1945, tokens=200),
        _875_milestone(journal, 3, 18.2246, 10.0301, tokens=300),
    ]
    desired = [
        _875_priced_close(journal, 20.0),
        _875_milestone(journal, 1, 5.0, None),
        _875_milestone(journal, 2, 8.3209, 3.3209, tokens=210),
        _875_milestone(journal, 3, 18.2246, 9.9037, tokens=300),
    ]
    actions = _875_milestone_actions(
        _875_plan(rederive, journal, current, desired))

    one, two, three = (
        f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:{n}" for n in (1, 2, 3)
    )
    assert one not in actions
    assert _875_money(actions[two]) == (8.3209, 8.3209 - 5.0)
    assert actions[two].payload["block_input_tokens"] == 210
    block, marginal = _875_money(actions[three])
    assert block == 18.2246
    assert marginal == 18.2246 - 8.3209
    assert abs(marginal - 9.9037) <= 1e-9


def test_875_card_edit_with_predecessor_correction_resolves_final_difference(
    cctally_module,
):
    """Spec §6.1 test 3 (A1, A6)."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 20.0),
        _875_milestone(journal, 1, 5.0, None),
        _875_milestone(journal, 2, 8.1945, 3.1945, tokens=200),
        _875_milestone(journal, 3, 18.2246, 10.0301, tokens=300),
    ]
    desired = [
        _875_priced_close(journal, 20.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 11.6, 4.6, tokens=210),
        _875_milestone(journal, 3, 25.5, 13.9, tokens=300),
    ]
    actions = _875_milestone_actions(
        _875_plan(rederive, journal, current, desired))

    one, two, three = (
        f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:{n}" for n in (1, 2, 3)
    )
    assert one not in actions
    assert _875_money(actions[two]) == (11.6, 11.6 - 5.0)
    assert _875_money(actions[three]) == (18.2246, 18.2246 - 11.6)


def test_875_closure_evidence_change_releases_milestones_to_replay(
    cctally_module,
):
    """Spec §6.1 test 4 (A4): a corrected close is not kept."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    moved = _875_close(
        journal, total=14.0,
        models=[{"model": "m", "cost_usd": 14.0, "input_tokens": 300}],
        final_five_hour_percent=43.0,
    )
    desired = [
        moved,
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    plan = _875_plan(rederive, journal, current, desired)
    actions = _875_milestone_actions(plan)

    assert any(a.event_id == _875_CLOSE_ID for a in plan.actions)
    assert _875_money(actions[f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:1"]) == (
        7.0, None)
    assert _875_money(actions[f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:2"]) == (
        14.0, 7.0)


@pytest.mark.parametrize("desired_marginal, expected_marginal", [
    (9.0, 16.0 - 3.0),
    (None, None),
])
def test_875_identity_corrected_candidate_takes_replay_money_and_presence(
    cctally_module, desired_marginal, expected_marginal,
):
    """Spec §6.1 test 5 (A4, spec §7.2)."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0, tokens=200),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 16.0, desired_marginal, tokens=250),
    ]
    actions = _875_milestone_actions(
        _875_plan(rederive, journal, current, desired))

    assert f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:1" not in actions
    corrected = actions[f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:2"]
    assert _875_money(corrected) == (16.0, expected_marginal)
    assert corrected.payload["block_input_tokens"] == 250


def test_875_alerted_milestone_under_kept_close_stays_frozen(cctally_module):
    """Spec §6.1 test 6 (A7; the alert-envelope row of A2)."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    alerted = "2026-07-26T12:02:05Z"
    current = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0, alerted_at=alerted),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0, alerted_at=None),
    ]
    plan = _875_plan(rederive, journal, current, desired)

    assert plan.actions == ()


def test_875_predecessor_is_found_only_within_its_credit_segment(
    cctally_module,
):
    """Spec §6.1 test 7: each credit segment has its own predecessor chain.

    The post-credit segment reuses thresholds 1 and 2. With the segments
    merged, pre-credit threshold 2's predecessor would tie on threshold 1 and
    could resolve to the post-credit $10 crossing, giving a -$4 marginal; the
    rule keeps each segment's own $3 difference and $2 difference.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 12.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0),
        _875_milestone(journal, 1, 10.0, None, reset_ref=_875_CREDIT_REF),
        _875_milestone(journal, 2, 12.0, 2.0, reset_ref=_875_CREDIT_REF),
    ]
    desired = [
        _875_priced_close(journal, 12.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0),
        _875_milestone(journal, 1, 23.0, None, reset_ref=_875_CREDIT_REF),
        _875_milestone(journal, 2, 28.0, 5.0, reset_ref=_875_CREDIT_REF),
    ]
    plan = _875_plan(rederive, journal, current, desired)

    assert plan.actions == ()


def test_875_same_tick_thresholds_keep_a_null_marginal(cctally_module):
    """Spec §6.1 test 8: thresholds 3 and 4 crossed on one tick."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    tick = "2026-07-26T12:30:00Z"
    current = [
        _875_priced_close(journal, 9.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0),
        _875_milestone(journal, 3, 9.0, 3.0, captured=tick),
        _875_milestone(journal, 4, 9.0, None, captured=tick),
    ]
    desired = [
        _875_priced_close(journal, 9.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0),
        _875_milestone(journal, 3, 21.0, 7.0, captured=tick),
        _875_milestone(journal, 4, 21.0, None, captured=tick),
    ]
    plan = _875_plan(rederive, journal, current, desired)

    assert plan.actions == ()


@pytest.mark.parametrize("current_marginal, expected", [
    (3.0, None),
    # An inconsistent recorded marginal is re-resolved against the preserved
    # predecessor; without it in the final state the pair would be kept.
    (2.5, (6.0, 3.0)),
])
def test_875_preserved_predecessor_keeps_a_present_marginal(
    cctally_module, current_marginal, expected,
):
    """Spec §6.1 test 9 (A9): replay never sees a preserved b: crossing."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    preserved = _875_milestone(
        journal, 1, 3.0, None, event_id="b:five_hour_milestones:875")
    current = [
        _875_priced_close(journal, 6.0),
        preserved,
        _875_milestone(journal, 2, 6.0, current_marginal),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 2, 14.0, None),
    ]
    plan = _875_plan(
        rederive, journal, current, desired, preserved=[preserved])

    if expected is None:
        assert plan.actions == ()
    else:
        actions = _875_milestone_actions(plan)
        assert list(actions) == [f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:2"]
        assert _875_money(actions[f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:2"]) \
            == expected


def test_875_reviewed_hold_removed_predecessor_is_not_copied_from_replay(
    cctally_module,
):
    """Spec §6.1 test 10 (A9).

    Replay adds threshold 2 from an observation whose snapshot the reviewed
    hold keeps, and no accepted snapshot exists for it, so the hold removes it
    before the diff. Threshold 3's replayed marginal ($7, against the removed
    predecessor) must not be copied.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    held = _875_milestone(journal, 2, 14.0, 7.0)
    held["payload"]["usage_snapshot_ref"] = "sa:o:875-held"
    current = [
        _875_priced_close(journal, 9.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 3, 9.0, 6.0),
    ]
    desired = [
        _875_priced_close(journal, 9.0),
        _875_milestone(journal, 1, 7.0, None),
        held,
        _875_milestone(journal, 3, 21.0, 7.0),
    ]
    plan = _875_plan(
        rederive, journal, current, desired,
        reviewed_weekly_hold_ids=("o:875-held",),
    )

    assert plan.actions == ()


def test_875_present_marginal_without_predecessor_keeps_the_pair(
    cctally_module,
):
    """Spec §6.1 test 11 (A9): fail closed, never invent a marginal."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    plan = _875_plan(rederive, journal, current, desired)

    assert plan.actions == ()


def test_875_second_plan_over_the_corrected_state_is_a_noop(cctally_module):
    """Spec §6.1 test 12 (A3), over test 3's card-edit-plus-correction case."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 20.0),
        _875_milestone(journal, 1, 5.0, None),
        _875_milestone(journal, 2, 8.1945, 3.1945, tokens=200),
        _875_milestone(journal, 3, 18.2246, 10.0301, tokens=300),
    ]
    desired = [
        _875_priced_close(journal, 20.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 11.6, 4.6, tokens=210),
        _875_milestone(journal, 3, 25.5, 13.9, tokens=300),
    ]
    first = _875_plan(rederive, journal, current, desired)
    assert first.actions

    corrected = _875_corrected_state(journal, current, first)
    second = _875_plan(rederive, journal, corrected, desired)

    assert second.actions == (), [
        (a.disposition, a.event_id, a.payload) for a in second.actions
    ]


def test_875_at_corrected_predecessor_is_in_the_final_state(cctally_module):
    """Spec §6.1 test 13 (A1, A3).

    Threshold 1's crossing moved, so the planner supersedes it to its desired
    payload (its event id excludes the capture time). The unchanged threshold 2
    keeps its $6 block cost under the card edit and resolves its marginal
    against threshold 1's new $3.20.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    moved_at = "2026-07-25T12:05:00Z"
    current = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 3.2, None, at=moved_at,
                       captured="2026-07-26T12:01:30Z"),
        _875_milestone(journal, 2, 14.0, 10.8),
    ]
    first = _875_plan(rederive, journal, current, desired)
    actions = _875_milestone_actions(first)

    one = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:1"
    two = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:2"
    assert actions[one].at == moved_at
    assert _875_money(actions[one]) == (3.2, None)
    assert _875_money(actions[two]) == (6.0, 6.0 - 3.2)

    corrected = _875_corrected_state(journal, current, first)
    assert _875_plan(rederive, journal, corrected, desired).actions == ()


def test_875_marginal_within_tolerance_keeps_its_recorded_bytes(
    cctally_module,
):
    """Spec §4.3: 18.2246 - 8.3209 is 9.903699999999999, not 9.9037.

    The recorded 9.9037 is within 1e-9 of the recomputed difference, so it is
    kept byte-for-byte; without the snap every such milestone would churn.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    assert 18.2246 - 8.3209 != 9.9037
    current = [
        _875_priced_close(journal, 20.0),
        _875_milestone(journal, 2, 8.3209, None),
        _875_milestone(journal, 3, 18.2246, 9.9037),
    ]
    desired = [
        _875_priced_close(journal, 20.0),
        _875_milestone(journal, 2, 11.6, None),
        _875_milestone(journal, 3, 25.5, 13.9),
    ]
    plan = _875_plan(rederive, journal, current, desired)

    assert plan.actions == ()


def test_875_replay_addition_under_a_kept_close_resolves_its_chain(
    cctally_module,
):
    """Spec §4.2-§4.3, §7.3: an addition takes replay money and joins the chain.

    Replay adds threshold 2 at the new card ($14). It is a predecessor of the
    unchanged threshold 3, whose $9 block cost stays frozen, so its marginal is
    the final-cost difference 9 - 14 = -5 (the mixed-card chain the approved
    exclusions admit), and the addition's own marginal is 14 - 3 = 11.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 9.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 3, 9.0, 6.0),
    ]
    desired = [
        _875_priced_close(journal, 9.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0),
        _875_milestone(journal, 3, 21.0, 7.0),
    ]
    actions = _875_milestone_actions(
        _875_plan(rederive, journal, current, desired))

    one, two, three = (
        f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:{n}" for n in (1, 2, 3)
    )
    assert one not in actions
    assert actions[two].disposition == "add"
    assert _875_money(actions[two]) == (14.0, 14.0 - 3.0)
    assert actions[three].disposition == "supersede"
    assert _875_money(actions[three]) == (9.0, 9.0 - 14.0)


def test_875_tombstoned_milestone_is_not_a_predecessor(cctally_module):
    """Spec §4.2: a milestone the plan tombstones leaves the final state.

    Replay drops threshold 2, so threshold 3 resolves against threshold 1:
    9 - 3 = 6. Counting the tombstone would keep the recorded 9 - 6 = 3.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 9.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0),
        _875_milestone(journal, 3, 9.0, 3.0),
    ]
    desired = [
        _875_priced_close(journal, 9.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 3, 21.0, 14.0),
    ]
    actions = _875_milestone_actions(
        _875_plan(rederive, journal, current, desired))

    two = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:2"
    three = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:3"
    assert actions[two].disposition == "tombstone"
    assert actions[three].disposition == "supersede"
    assert _875_money(actions[three]) == (9.0, 9.0 - 3.0)


def test_875_empty_reset_ref_spellings_share_the_pre_credit_segment(
    cctally_module,
):
    """Spec §4.2: an empty reset_event_ref spelling is the "0" segment.

    The predecessor carries ``None``; the successor ``"0"``. Only the shared
    pre-credit segment gives the successor a predecessor, which re-resolves
    its inconsistent recorded 2.5 to 6 - 3 = 3; apart, the pair would be kept.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 3.0, None, reset_ref=None),
        _875_milestone(journal, 2, 6.0, 2.5),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 7.0, None, reset_ref=None),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    actions = _875_milestone_actions(
        _875_plan(rederive, journal, current, desired))

    two = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:2"
    assert list(actions) == [two]
    assert _875_money(actions[two]) == (6.0, 3.0)


def _875_action_summary(plan):
    """(disposition, event id, block cost, marginal) per action, for messages."""
    return [
        (
            action.disposition, action.event_id,
            (action.payload or {}).get("block_cost_usd"),
            (action.payload or {}).get("marginal_cost_usd"),
        )
        for action in plan.actions
    ]


def test_875_at_moved_milestone_after_a_retained_predecessor_converges(
    cctally_module,
):
    """Spec §6.1 test 14 (A3): the live non-convergence of spec §1.3.

    Threshold 1 is an unchanged crossing, so it keeps its frozen $20 under the
    card edit (replay repriced it to $10) and is never rewritten. Threshold 2's
    replayed crossing moved its ``at``, and replay's marginal 15 - 10 = 5
    disagrees with the final-state difference 15 - 20 = -5. Copying replay's 5
    left the second plan to supersede it to -5; resolving the at-moved
    crossing in the first plan makes the second plan a no-op.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    moved_at = "2026-07-25T12:05:00Z"
    current = [
        _875_priced_close(journal, 30.0),
        _875_milestone(journal, 1, 20.0, None),
        _875_milestone(journal, 2, 26.0, 6.0),
    ]
    desired = [
        _875_priced_close(journal, 30.0),
        _875_milestone(journal, 1, 10.0, None),
        _875_milestone(journal, 2, 15.0, 5.0, at=moved_at),
    ]
    first = _875_plan(rederive, journal, current, desired)
    corrected = _875_corrected_state(journal, current, first)
    second = _875_plan(rederive, journal, corrected, desired)
    residual = _875_action_summary(second)

    two = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:2"
    actions = _875_milestone_actions(first)
    assert list(actions) == [two], _875_action_summary(first)
    assert actions[two].disposition == "supersede"
    assert actions[two].at == moved_at
    assert _875_money(actions[two]) == (15.0, 15.0 - 20.0), residual
    assert second.actions == (), residual


def test_875_at_moved_milestone_without_a_predecessor_keeps_its_desired_pair(
    cctally_module,
):
    """Spec §6.1 test 15 (A3, A9).

    The lone crossing in its segment moved its ``at``, so it is a corrected
    crossing. With no final-state predecessor it keeps the desired pair
    (12, 3) whole, not the current (10, 2), and the second plan keeps that
    same pair from current. Copying replay reached the same payload, so this
    guards the no-predecessor pair rather than reproducing the §1.3 failure.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    moved_at = "2026-07-25T12:05:00Z"
    current = [
        _875_priced_close(journal, 12.0),
        _875_milestone(journal, 2, 10.0, 2.0),
    ]
    desired = [
        _875_priced_close(journal, 12.0),
        _875_milestone(journal, 2, 12.0, 3.0, at=moved_at),
    ]
    first = _875_plan(rederive, journal, current, desired)

    two = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:2"
    actions = _875_milestone_actions(first)
    assert list(actions) == [two], _875_action_summary(first)
    assert actions[two].disposition == "supersede"
    assert actions[two].at == moved_at
    assert _875_money(actions[two]) == (12.0, 3.0)

    corrected = _875_corrected_state(journal, current, first)
    second = _875_plan(rederive, journal, corrected, desired)
    assert second.actions == (), _875_action_summary(second)


_875_TWO = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:2"
_875_ONE = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:1"
_875_PRESERVED_CLOSE_ID = "b:five_hour_blocks:875"
#: A reading of the overlapping earlier window. A hold on it holds that window,
#: never this block's close.
_875_NEIGHBOUR_WINDOW = _875_WINDOW - 3600
#: A real card, so the historical-close rule can prove a $0 close (#869).
_875_PRICED_MODEL = "claude-3-5-sonnet-20241022"
#: sha256 of `git show 80edb5f24:bin/_lib_rederive.py`, the pre-#875 planner.
_875_PRE_875_SHA256 = (
    "fcd9a9b466ebf52842ae99bb25276d52d7096e478a22286c7c63d374c2e67e64"
)


def _875_moved_close(journal, total):
    """A replayed close whose closure evidence moved, so the plan corrects it."""
    return _875_close(
        journal, total=total,
        models=[{"model": "m", "cost_usd": total, "input_tokens": 300}],
        final_five_hour_percent=43.0,
    )


def _875_preserved_close(journal, total):
    """A cutover-exported close for the same block: preserved history."""
    return {**_875_priced_close(journal, total), "id": _875_PRESERVED_CLOSE_ID}


def _875_snapshot(journal, raw_id, *, window=_875_WINDOW):
    return journal.make_evt(
        kind="snapshot_accept", id=f"sa:{raw_id}", at=AT,
        payload={
            "account_key": _875_ACCOUNT, "captured_at_utc": AT,
            "weekly_percent": 63.0, "five_hour_percent": 41.0,
            "five_hour_window_key": window,
        },
    )


def _875_referencing(milestone, snapshot):
    """``milestone`` crossed on ``snapshot``'s observation."""
    milestone["payload"]["usage_snapshot_ref"] = snapshot["id"]
    return milestone


def _875_pre_875_planner():
    """The planner exactly as it was before #875, at ``80edb5f24``.

    ``tests/fixtures/rederive/pre_875_lib_rederive.py`` is a byte-for-byte copy
    of ``git show 80edb5f24:bin/_lib_rederive.py``, pinned by its sha256. It is
    committed because the remote runner receives a materialized tree that may
    carry no ``.git``. It loads under its own module name, never as
    ``_lib_rederive``, and it imports only the standard library and
    ``_lib_cost_provenance``.
    """
    import hashlib
    import importlib.util
    import sys
    from pathlib import Path

    name = "_lib_rederive_pre_875"
    module = sys.modules.get(name)
    if module is not None:
        return module
    path = (Path(__file__).parent / "fixtures" / "rederive"
            / "pre_875_lib_rederive.py")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == \
        _875_PRE_875_SHA256
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve string annotations through sys.modules.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _875_run_passes(planner, journal, records, desired, *, limit=5, **kwargs):
    """Plan, append the plan as a committed batch, and repeat until a no-op.

    Returns every plan up to and including the first one without an action.
    Each pass derives ``preserved_events`` from the journal records exactly as
    the command does, so a revived or tombstoned ``b:`` event behaves as it
    would on a real store.
    """
    records = list(records)
    plans = []
    for number in range(1, limit + 1):
        plan = planner.build_claude_usage_plan(
            selection=journal.resolve_effective_events(records),
            desired_events=list(desired),
            journal_high_water=("observations-2026-07.jsonl", 10),
            cache_fingerprint="sha256:cache",
            config_fingerprint="sha256:config",
            preserved_events=tuple(planner.preserved_history(
                records, evidence_retained=True).values()),
            **kwargs,
        )
        plans.append(plan)
        if not plan.actions:
            return plans
        records.extend(journal.make_correction_batch(
            batch_id=f"rederive:claude-usage:875-pass-{number}",
            family="claude-usage", at=AT,
            actions=plan.to_correction_actions(),
        ))
    raise AssertionError(
        f"no no-op within {limit} passes: "
        + repr([_875_action_summary(plan) for plan in plans])
    )


def _875_ids(plan):
    return [action.event_id for action in plan.actions]


def test_875_replaced_close_corrected_then_frozen_is_a_noop(cctally_module):
    """Spec §6.1 test 16 (A3): a corrected close, then the same close kept.

    The first plan corrects the close, so it replaces it: the milestone at
    threshold 2 is a corrected crossing with replay's $14 block cost, and its
    marginal is the final-state difference against the preserved ``b:``
    predecessor that replay never reproduces, 14 - 3 = 11, not replay's 7. The
    second plan keeps the close, finds the same (14, 11), and is a no-op.
    Revision 7 wrote replay's (14, 7), and its second plan superseded that to
    (14, 11) (spec §1.4).
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    preserved = _875_milestone(
        journal, 1, 3.0, None, event_id="b:five_hour_milestones:875")
    current = [
        _875_priced_close(journal, 6.0),
        preserved,
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    desired = [
        _875_moved_close(journal, 14.0), _875_milestone(journal, 2, 14.0, 7.0),
    ]
    first = _875_plan(
        rederive, journal, current, desired, preserved=[preserved])
    corrected = _875_corrected_state(journal, current, first)
    second = _875_plan(
        rederive, journal, corrected, desired, preserved=[preserved])
    summary = (_875_action_summary(first), _875_action_summary(second))

    assert _875_ids(first) == [_875_CLOSE_ID, _875_TWO], summary
    assert _875_money(_875_milestone_actions(first)[_875_TWO]) == (
        14.0, 11.0), summary
    assert second.actions == (), summary


def test_875_replaced_close_added_then_frozen_is_a_noop(cctally_module):
    """Spec §6.1 test 17 (A3): the close only replay holds, which the plan adds.

    Current holds no close for the block, so the first plan adds replay's
    close and resolves the milestone under it as a corrected crossing: (14,
    11) against the preserved predecessor. The second plan keeps the added
    close and is a no-op.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    preserved = _875_milestone(
        journal, 1, 3.0, None, event_id="b:five_hour_milestones:875")
    current = [preserved, _875_milestone(journal, 2, 6.0, 3.0)]
    desired = [
        _875_priced_close(journal, 14.0),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    first = _875_plan(
        rederive, journal, current, desired, preserved=[preserved])
    corrected = _875_corrected_state(journal, current, first)
    second = _875_plan(
        rederive, journal, corrected, desired, preserved=[preserved])
    summary = (_875_action_summary(first), _875_action_summary(second))

    assert [(a.disposition, a.event_id) for a in first.actions] == [
        ("add", _875_CLOSE_ID), ("supersede", _875_TWO),
    ], summary
    assert _875_money(_875_milestone_actions(first)[_875_TWO]) == (
        14.0, 11.0), summary
    assert second.actions == (), summary


def test_875_revived_predecessor_serves_the_marginal_chain(cctally_module):
    """Spec §6.1 test 18 (A9): a revived preserved milestone is a predecessor.

    This family's earlier batch retired the preserved ``b:`` crossing at
    threshold 1, so the plan revives it. Threshold 2's recorded marginal 2.5
    is inconsistent; only the revived predecessor gives it the final-state
    difference 6 - 3 = 3. Without the revival in the final state it has no
    predecessor and keeps its pair, and the plan would not touch it.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    retired = _875_milestone(
        journal, 1, 3.0, None, event_id="b:five_hour_milestones:875")
    records = [
        _875_priced_close(journal, 6.0),
        retired,
        *_tombstone_batch(
            journal, retired["id"],
            batch_id="rederive:claude-usage:875-retired",
        ),
        _875_milestone(journal, 2, 6.0, 2.5),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    plans = _875_run_passes(rederive, journal, records, desired)
    first = plans[0]
    actions = {action.event_id: action for action in first.actions}
    summary = [_875_action_summary(plan) for plan in plans]

    assert list(actions) == [retired["id"], _875_TWO], summary
    assert actions[retired["id"]].disposition == "supersede"
    assert actions[retired["id"]].revision == 2
    assert actions[retired["id"]].payload == retired["payload"]
    assert _875_money(actions[_875_TWO]) == (6.0, 3.0), summary
    assert len(plans) == 2, summary


_875_MALFORMED_COSTS = [
    "3.0", None, True, float("nan"), float("inf"),
    # An int too large for a float: ``math.isfinite`` raises on it.
    pytest.param(10 ** 400, id="int-beyond-float"),
]


@pytest.mark.parametrize("malformed", _875_MALFORMED_COSTS, ids=repr)
def test_875_malformed_nearest_predecessor_fails_closed(
    cctally_module, malformed,
):
    """Spec §6.1 test 19, first case (A9): no skipping past a malformed row.

    Threshold 2's block cost is unusable, but its threshold is not, so it
    still occupies threshold 2. Threshold 3 then has no usable predecessor and
    keeps its pair (9, 2.5). Skipping to threshold 1 would write 9 - 3 = 6.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 9.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, malformed, None),
        _875_milestone(journal, 3, 9.0, 2.5),
    ]
    desired = [
        _875_priced_close(journal, 9.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, None),
        _875_milestone(journal, 3, 21.0, 7.0),
    ]
    plan = _875_plan(rederive, journal, current, desired)

    assert plan.actions == (), _875_action_summary(plan)


@pytest.mark.parametrize("malformed", _875_MALFORMED_COSTS, ids=repr)
def test_875_malformed_candidate_cost_keeps_its_pair(cctally_module, malformed):
    """Spec §6.1 test 19, second case (A9): no subtraction from its own cost."""
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, malformed, 2.5),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    plan = _875_plan(rederive, journal, current, desired)

    assert plan.actions == (), _875_action_summary(plan)


@pytest.mark.parametrize("malformed", [True, 1.0, "1"], ids=repr)
def test_875_malformed_threshold_occupies_no_position(
    cctally_module, malformed,
):
    """Spec §6.1 test 19, third case (A9).

    A row whose threshold is a bool or not an ``int`` is no predecessor, even
    where ``int()`` would read it as 1. Threshold 2 then has no predecessor and
    keeps its pair (6, 2.5); reading the row as threshold 1 writes 6 - 3 = 3.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    def odd(block_cost):
        return _875_milestone(
            journal, malformed, block_cost, None, tokens=100,
            captured="2026-07-26T12:01:00Z",
            event_id=f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:malformed",
        )

    current = [
        _875_priced_close(journal, 6.0),
        odd(3.0),
        _875_milestone(journal, 2, 6.0, 2.5),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        odd(7.0),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    plan = _875_plan(rederive, journal, current, desired)

    assert plan.actions == (), _875_action_summary(plan)


@pytest.mark.parametrize("reference", ["n/a", "3.0", True], ids=repr)
def test_875_non_numeric_reference_marginal_writes_the_difference(
    cctally_module, reference,
):
    """Spec §6.1 test 19, fourth case (A9, §4.3).

    The recorded marginal is present but not a number, so it cannot be the
    snap reference. The plan is not aborted: it writes the numeric final-state
    difference 6 - 3 = 3, never the reference's bytes.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, reference),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    plan = _875_plan(rederive, journal, current, desired)
    actions = _875_milestone_actions(plan)

    assert list(actions) == [_875_TWO], _875_action_summary(plan)
    money = _875_money(actions[_875_TWO])
    assert money == (6.0, 3.0)
    assert type(money[1]) is float


_875_TIE_LESSER = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:1-a"
_875_TIE_GREATER = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:1-b"


def _875_tie_rows(journal, lesser_cost, greater_cost):
    """Two final-state rows that share threshold 1, by ascending event id."""
    return [
        _875_milestone(journal, 1, lesser_cost, None, event_id=_875_TIE_LESSER),
        _875_milestone(
            journal, 1, greater_cost, None, event_id=_875_TIE_GREATER),
    ]


def test_875_shared_nearest_threshold_takes_the_greater_event_id(
    cctally_module,
):
    """Spec §6.1 test 19, fifth case, both rows usable (A9, §4.3).

    Two rows occupy threshold 1 under a kept $6 close. The predecessor is
    the one with the greater event id, whose $2 is the SMALLER cost, so
    threshold 2's marginal becomes 6 - 2 = 4. The lesser id, or the greater
    cost, would give 6 - 4 = 2.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    current = [
        _875_priced_close(journal, 6.0),
        *_875_tie_rows(journal, 4.0, 2.0),
        _875_milestone(journal, 2, 6.0, 1.0),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        *_875_tie_rows(journal, 9.0, 5.0),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    plan = _875_plan(rederive, journal, current, desired)
    actions = _875_milestone_actions(plan)

    assert list(actions) == [_875_TWO], _875_action_summary(plan)
    assert _875_money(actions[_875_TWO]) == (6.0, 4.0)


@pytest.mark.parametrize("malformed", _875_MALFORMED_COSTS, ids=repr)
@pytest.mark.parametrize("malformed_row", ["lesser", "greater"])
def test_875_shared_nearest_threshold_with_an_unusable_row_fails_closed(
    cctally_module, malformed_row, malformed,
):
    """Spec §6.1 test 19, fifth case, either row unusable (A9, §4.3).

    One of the two rows at threshold 1 has an unusable block cost, so
    threshold 2 has no usable predecessor and keeps its chosen pair (6, 1).
    Taking the other row would write 6 - 4 = 2 or 6 - 2 = 4.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    lesser, greater = (
        (malformed, 2.0) if malformed_row == "lesser" else (4.0, malformed)
    )
    current = [
        _875_priced_close(journal, 6.0),
        *_875_tie_rows(journal, lesser, greater),
        _875_milestone(journal, 2, 6.0, 1.0),
    ]
    desired = [
        _875_priced_close(journal, 6.0),
        *_875_tie_rows(journal, 9.0, 5.0),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    plan = _875_plan(rederive, journal, current, desired)

    assert plan.actions == (), _875_action_summary(plan)


def _875_reconciled_predecessor(journal):
    """The #858 reconciliation shape, with a five-hour crossing that cites it.

    A held statusline replay candidate precedes the genuine API acceptance
    the journal recorded. Reconciliation keeps the accepted snapshot and
    carries its dependent five-hour milestone into ``desired`` whole, at its
    recorded $2.50 rather than replay's $2.00.
    """
    account = "c719887886403b0a1e3004e967dbd20e"
    window = 1785978600
    week_end = int(dt.datetime(
        2026, 8, 29, 5, 0, tzinfo=dt.timezone.utc,
    ).timestamp())
    new_at = "2026-08-23T00:20:25Z"
    old_at = "2026-08-23T00:20:27Z"
    new_raw = {**journal.make_obs(
        at=new_at, src="record-usage", provider="claude", account=account,
        payload={
            "captured_at": new_at, "source": "statusline",
            "weekly_percent": 10.0, "resets_at": week_end,
            "five_hour_percent": 1.0,
            "five_hour_resets_at": "2026-08-23T04:50:00+00:00",
        },
    ), "id": "o:f1c3e45eafe02d65"}
    old_raw = {**journal.make_obs(
        at=old_at, src="record-usage", provider="claude", account=account,
        payload={
            "captured_at": old_at, "source": "api",
            "weekly_percent": 11.0, "resets_at": week_end - 1,
            "five_hour_percent": 1.0,
            "five_hour_resets_at": "2026-08-23T04:49:59+00:00",
        },
    ), "id": "o:b8db6f3413ca6dd0"}
    common = {
        "account_key": account,
        "week_start_date": "2026-08-22", "week_end_date": "2026-08-29",
        "week_start_at": "2026-08-22T05:00:00+00:00",
        "week_end_at": "2026-08-29T05:00:00+00:00",
        "weekly_percent": 11.0, "five_hour_percent": 1.0,
        "five_hour_window_key": window, "page_url": None,
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
    predecessor_id = f"fhm:{account}:{window}:0:1"
    old_predecessor = journal.make_evt(
        kind="five_hour_milestone", id=predecessor_id, at=old_at,
        payload={
            "account_key": account, "five_hour_window_key": window,
            "percent_threshold": 1, "captured_at_utc": old_at,
            "usage_snapshot_ref": old["id"],
            "block_cost_usd": 2.5, "marginal_cost_usd": None,
            "seven_day_pct_at_crossing": 11.0,
            "input_tokens": 111, "output_tokens": 222,
            "cache_create_tokens": 333, "cache_read_tokens": 444,
        },
    )
    new_predecessor = journal.make_evt(
        kind="five_hour_milestone", id=predecessor_id, at=new_at,
        payload={
            **old_predecessor["payload"], "captured_at_utc": new_at,
            "usage_snapshot_ref": new["id"],
            "block_cost_usd": 2.0, "seven_day_pct_at_crossing": 10.0,
            "input_tokens": 999, "output_tokens": 999,
            "cache_create_tokens": 999, "cache_read_tokens": 999,
        },
    )
    close = journal.make_evt(
        kind="five_hour_block_close", id=f"fhbc:{account}:{window}", at=AT,
        payload={
            **_875_priced_close(journal, 20.0)["payload"],
            "account_key": account, "five_hour_window_key": window,
        },
    )

    def successor(at, block_cost, marginal):
        return journal.make_evt(
            kind="five_hour_milestone", id=f"fhm:{account}:{window}:0:2",
            at=at,
            payload={
                "account_key": account, "five_hour_window_key": window,
                "percent_threshold": 2, "reset_event_ref": "0",
                "captured_at_utc": "2026-08-23T01:00:00Z",
                "usage_snapshot_ref": "sa:o:875-successor",
                "block_cost_usd": block_cost, "marginal_cost_usd": marginal,
                "seven_day_pct_at_crossing": 11.0,
            },
        )

    return dict(
        old=old, new=new, old_raw=old_raw, new_raw=new_raw,
        old_predecessor=old_predecessor, new_predecessor=new_predecessor,
        close=close, successor=successor,
    )


def test_875_reconciliation_carried_predecessor_serves_the_chain(
    cctally_module,
):
    """Spec §6.1 test 20 (A3): the live shape of spec §1.3.

    Reconciliation keeps the accepted snapshot and carries the crossing that
    cites it whole, at $2.50. The successor's replayed crossing moved its
    ``at``, and replay's marginal 14 - 2 = 12 is against a predecessor the
    final state does not hold. The first plan writes 14 - 2.5 = 11.5, and the
    second plan, which reconciles the same way, is a no-op.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    fx = _875_reconciled_predecessor(journal)
    moved_at = "2026-08-23T01:00:05Z"
    successor_id = fx["successor"](AT, 0, 0)["id"]
    current = [
        fx["old"], fx["old_predecessor"], fx["close"],
        fx["successor"](AT, 6.0, 4.0),
    ]
    desired = [
        fx["new"], fx["new_predecessor"], fx["close"],
        fx["successor"](moved_at, 14.0, 12.0),
    ]
    raw = [fx["new_raw"], fx["old_raw"]]

    first = _875_plan(
        rederive, journal, current, desired, raw_observations=raw)
    unreconciled = _875_plan(rederive, journal, current, desired)
    corrected = _875_corrected_state(journal, current, first)
    second = _875_plan(
        rederive, journal, corrected, desired, raw_observations=raw)
    summary = [_875_action_summary(plan)
               for plan in (first, unreconciled, second)]

    # The reconciliation happened: without the raw origins the same inputs
    # swap the snapshots and correct the predecessor to replay's $2.00.
    assert {(a.disposition, a.event_id) for a in unreconciled.actions} >= {
        ("tombstone", fx["old"]["id"]), ("add", fx["new"]["id"]),
        ("supersede", fx["old_predecessor"]["id"]),
    }, summary
    assert _875_money(next(
        a for a in unreconciled.actions if a.event_id == successor_id
    )) == (14.0, 12.0), summary
    # With them, only the successor moves, against the carried $2.50.
    assert _875_ids(first) == [successor_id], summary
    assert first.actions[0].at == moved_at
    assert _875_money(first.actions[0]) == (14.0, 14.0 - 2.5), summary
    assert second.actions == (), summary


def _875_flip_fixture(journal, variant, successor_marginal):
    """(records, desired, plan kwargs) for one spec §6.1 test 21 variant."""
    held = _875_snapshot(journal, "o:875-held")
    closed = _875_preserved_close(journal, 6.0)
    replay_marginal = None if successor_marginal is None else 7.0
    milestones = [
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, successor_marginal),
    ]
    desired = [
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, replay_marginal),
    ]
    retired = _tombstone_batch(
        journal, closed["id"], batch_id="rederive:claude-usage:875-retired")
    if variant == "held-then-preserved":
        # Held through a snapshot the same plan tombstones (spec §1.6).
        return (
            [closed, held, *milestones], desired,
            {"reviewed_weekly_hold_ids": ("o:875-held",)},
        )
    if variant == "revived-then-held":
        # Replay keeps the held snapshot, so the hold outlives the revival.
        return (
            [closed, *retired, held, *milestones], [held, *desired],
            {"reviewed_weekly_hold_ids": ("o:875-held",)},
        )
    assert variant == "revived-then-preserved"
    return [closed, *retired, *milestones], desired, {}


@pytest.mark.parametrize("successor_marginal", [3.0, None])
@pytest.mark.parametrize("variant", [
    "held-then-preserved", "revived-then-held", "revived-then-preserved",
])
def test_875_kept_and_preserved_flips_freeze_the_same_block_cost(
    cctally_module, variant, successor_marginal,
):
    """Spec §6.1 test 21 (A3; spec §1.6, §4.1).

    A ``b:`` close moves between the kept and the preserved roles from one
    plan to the next. Both are frozen, so the unchanged crossings keep their
    current $3 and $6 in both plans and neither plan takes a milestone
    action. Revision 7 froze only kept closes: it repriced them to replay's
    $7 and $14 whenever the close was preserved.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    records, desired, kwargs = _875_flip_fixture(
        journal, variant, successor_marginal)
    plans = _875_run_passes(rederive, journal, records, desired, **kwargs)
    summary = [_875_action_summary(plan) for plan in plans]
    first = plans[0]

    assert len(plans) == 2, summary
    assert _875_milestone_actions(first) == {}, summary
    if variant == "held-then-preserved":
        # Kept through hold injection first (not preserved), then preserved.
        assert _875_ids(first) == ["sa:o:875-held"], summary
        assert first.actions[0].disposition == "tombstone"
        assert [plan.preserved_event_count for plan in plans] == [0, 1]
    else:
        assert _875_ids(first) == [_875_PRESERVED_CLOSE_ID], summary
        assert first.actions[0].revision == 2
        expected = [1, 0] if variant == "revived-then-held" else [1, 1]
        assert [plan.preserved_event_count for plan in plans] == expected


@pytest.mark.parametrize("successor_marginal", [3.0, 2.0])
def test_875_close_corrected_again_takes_no_milestone_pass_of_its_own(
    cctally_module, successor_marginal,
):
    """Spec §6.1 test 22 (A3, §7.7): weekly-axis correction, then the marker.

    An unmarked, historically provable $0 close sits in a held window. The
    reviewed hold first corrects only its weekly axes, and the next plan's
    #869 historical-marker branch corrects the same close again: the close's
    own decision takes a second pass. Every plan gets the same normally
    derived markers and the same replay, and the held snapshot stays
    supported throughout, because the predecessor that cites it keeps it
    alive. The hold persists, so both corrections keep the held payload's $0
    total: the close is frozen on both passes (spec §4.1, revision 13). The
    successor is an unchanged crossing, so it keeps its $6 block cost
    (replay says $14), and its marginal is 6 - 3 = 3 against the held
    predecessor, which replay prices at $7. Copying replay would write 7,
    and revision 11, which replaced the close, wrote (14, 11). With a
    recorded marginal of 3 the first plan takes no milestone action; with 2
    it writes (6, 3). The second plan's only action is the marker, and the
    third is a no-op. The pre-#875 planner needs no fewer passes on the same
    sequence.
    """
    import types

    import _cctally_rederive as command
    import _lib_journal as journal
    import _lib_rederive as rederive

    close = _875_close(
        journal, total=0.0,
        models=[{"model": _875_PRICED_MODEL, "cost_usd": 0.0,
                 "input_tokens": 300}],
        seven_day_pct_at_block_start=60.0, seven_day_pct_at_block_end=63.0,
        crossed_seven_day_reset=0,
    )
    replayed_close = _875_close(
        journal, total=14.0,
        models=[{"model": _875_PRICED_MODEL, "cost_usd": 14.0,
                 "input_tokens": 300}],
        pricing={"version": 1, "unpricedModels": []},
        seven_day_pct_at_block_start=60.0, seven_day_pct_at_block_end=15.0,
        crossed_seven_day_reset=1,
    )
    held = _875_snapshot(journal, "o:875-held")
    records = [
        close, held,
        _875_referencing(_875_milestone(journal, 1, 3.0, None), held),
        _875_milestone(journal, 2, 6.0, successor_marginal),
    ]
    replayed_successor = _875_milestone(journal, 2, 14.0, 7.0)
    desired = [
        replayed_close,
        _875_referencing(_875_milestone(journal, 1, 7.0, None), held),
        replayed_successor,
    ]
    entry = types.SimpleNamespace(
        source_account_key=_875_ACCOUNT, source_line_offset=0,
        source_path="/tmp/875.jsonl",
        timestamp=dt.datetime(2026, 7, 26, 12, 0, tzinfo=dt.timezone.utc),
        model=_875_PRICED_MODEL, cost_usd=None, speed=None,
        input_tokens=300, output_tokens=0, cache_creation_tokens=0,
        cache_1h_tokens=None, cache_read_tokens=0,
    )
    markers = command._historical_close_markers(
        journal.resolve_effective_events(records), desired,
        {(_875_ACCOUNT, _875_WINDOW): [entry]},
    )
    assert list(markers) == [_875_CLOSE_ID]
    kwargs = dict(
        reviewed_weekly_hold_ids=("o:875-held",),
        historical_close_markers=markers,
    )

    fixed = _875_run_passes(rederive, journal, records, desired, **kwargs)
    pre = _875_run_passes(
        _875_pre_875_planner(), journal, records, desired, **kwargs)
    summary = {
        "fixed": [_875_action_summary(plan) for plan in fixed],
        "pre-875": [_875_action_summary(plan) for plan in pre],
    }

    # The held snapshot stays supported: no plan retires it, or the
    # predecessor whose citation keeps it alive.
    for plan in fixed:
        assert not {held["id"], _875_ONE} & set(_875_ids(plan)), summary
    # First pass: the weekly-axis correction keeps the $0 total, so the
    # close is frozen and the successor keeps its $6.
    assert _875_ids(fixed[0])[0] == _875_CLOSE_ID, summary
    weekly = fixed[0].actions[0].payload
    assert (weekly["seven_day_pct_at_block_end"], weekly["total_cost_usd"],
            "_pricing" in weekly) == (15.0, 0.0, False)
    milestones = _875_milestone_actions(fixed[0])
    if successor_marginal == 3.0:
        assert milestones == {}, summary
    else:
        assert list(milestones) == [_875_TWO], summary
        money = _875_money(milestones[_875_TWO])
        assert money == (6.0, 6.0 - 3.0), summary
        assert money[1] != replayed_successor["payload"]["marginal_cost_usd"]
    # Second pass: the marker is the only change, the $0 total stays, and
    # the frozen block takes no milestone action.
    assert _875_ids(fixed[1]) == [_875_CLOSE_ID], summary
    marked = fixed[1].actions[0].payload
    assert marked["_pricing"] == markers[_875_CLOSE_ID]
    assert marked["total_cost_usd"] == 0.0

    def unmarked(payload):
        return {key: value for key, value in payload.items()
                if key not in {"_pricing", "pricing_provenance_json"}}

    assert unmarked(marked) == unmarked(weekly), summary
    assert _875_milestone_actions(fixed[1]) == {}, summary
    assert len(fixed) == 3, summary
    # The same close takes both passes under the pre-#875 planner too.
    assert [_875_CLOSE_ID in _875_ids(plan) for plan in pre[:2]] == [
        True, True], summary
    assert len(pre) >= len(fixed), summary
    # §7.7 evidence: both planners reach the no-op at the third plan.
    assert (len(pre), len(fixed)) == (3, 3), summary


def _875_causal_plans(rederive, journal, records, baseline_desired,
                      reviewed_desired, *, preserved=(),
                      reviewed_hold_ids=(), conflicted_event_ids=()):
    """Baseline and reviewed plans over one current graph, and their subset."""
    selection = journal.resolve_effective_events(list(records))
    common = dict(
        selection=selection,
        journal_high_water=("observations-2026-07.jsonl", 10),
        cache_fingerprint="sha256:cache",
        preserved_events=tuple(preserved), enforce_guard=False,
        conflicted_event_ids=frozenset(conflicted_event_ids),
    )
    baseline = rederive.build_claude_usage_plan(
        desired_events=list(baseline_desired),
        config_fingerprint="sha256:baseline", **common,
    )
    reviewed = rederive.build_claude_usage_plan(
        desired_events=list(reviewed_desired),
        config_fingerprint="sha256:reviewed",
        reviewed_weekly_hold_ids=tuple(reviewed_hold_ids), **common,
    )
    current_events = {
        event_id: selected.record
        for event_id, selected in selection.by_id.items()
        if selected.status == "active" and selected.record is not None
    }
    causal = rederive.causal_delta_plan(
        baseline, reviewed, current_events=current_events)
    return baseline, reviewed, causal, current_events


def _875_applied(current_events, actions, event_ids):
    """(at, payload) per id in ``event_ids`` after applying ``actions``."""
    state = {
        event_id: (record.get("at"), record.get("payload"))
        for event_id, record in current_events.items()
    }
    for action in actions:
        if action.disposition == "tombstone":
            state.pop(action.event_id, None)
        else:
            state[action.event_id] = (action.at, action.payload)
    return {event_id: state.get(event_id) for event_id in event_ids}


_875_PRICING_STATES = [1.0, 1.5]


def _875_close_total_moves(plan, current_total):
    """True when ``plan``'s close action moves the total by over 1e-9 USD.

    A correction within that tolerance is frozen under spec revision 12
    (§4.1) and would leave the causal closure unexercised.
    """
    close = next(
        action for action in plan.actions
        if action.kind == "five_hour_block_close"
    )
    return abs(close.payload["total_cost_usd"] - current_total) > 1e-9


@pytest.mark.parametrize("card", _875_PRICING_STATES)
def test_875_causal_subset_applies_a_replaced_close_block_whole(
    cctally_module, card,
):
    """Spec §6.1 test 23, replaced close in both plans (A12; spec §1.5).

    Both plans correct the close identically, so the B/R filter drops that
    correction. The reviewed plan's hold keeps the predecessor whole at $3
    while the baseline reprices it, so only the successor's target differs.
    The block closure brings the close correction back, and the applied block
    equals the reviewed target instead of repricing the successor under a
    close left at $6.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    held = _875_snapshot(
        journal, "o:875-held", window=_875_NEIGHBOUR_WINDOW)
    crossing = _875_snapshot(journal, "o:875-0-2")
    records = [
        _875_priced_close(journal, 6.0), held, crossing,
        _875_referencing(_875_milestone(journal, 1, 3.0, None), held),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    desired = [
        _875_moved_close(journal, 14.0 * card), held, crossing,
        _875_referencing(_875_milestone(journal, 1, 7.0 * card, None), held),
        _875_milestone(journal, 2, 14.0 * card, 7.0 * card),
    ]
    baseline, reviewed, causal, current = _875_causal_plans(
        rederive, journal, records, desired, desired,
        reviewed_hold_ids=("o:875-held",),
    )
    block = [_875_CLOSE_ID, _875_ONE, _875_TWO]
    summary = [_875_action_summary(plan)
               for plan in (baseline, reviewed, causal)]

    assert _875_ids(baseline) == block, summary
    assert _875_ids(reviewed) == [_875_CLOSE_ID, _875_TWO], summary
    assert baseline.actions[0].to_correction_action() == \
        reviewed.actions[0].to_correction_action()
    # A replaced close: both plans change its money (spec §4.1).
    assert _875_close_total_moves(baseline, 6.0), summary
    assert _875_close_total_moves(reviewed, 6.0), summary
    assert _875_money(reviewed.actions[1]) == (
        14.0 * card, 14.0 * card - 3.0), summary
    assert _875_ids(causal) == [_875_CLOSE_ID, _875_TWO], summary
    assert _875_applied(current, causal.actions, block) == \
        _875_applied(current, reviewed.actions, block)


@pytest.mark.parametrize("current_marginal", [3.0, 2.0])
@pytest.mark.parametrize("card", _875_PRICING_STATES)
def test_875_causal_subset_keeps_frozen_block_cost_under_a_preserved_close(
    cctally_module, card, current_marginal,
):
    """Spec §6.1 test 23, retained preserved close (A12; spec §1.6, §4.5).

    The baseline corrects the predecessor's crossing at today's card, while
    the reviewed plan's hold keeps it whole at $3. The successor is an
    unchanged crossing under a frozen close, so its block cost is $6 in both
    plans and in both pricing states. With a recorded marginal of 3 the
    reviewed plan takes no action; with 2 it takes a marginal-only action,
    which the subset may keep. Neither reprices the successor.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    held = _875_snapshot(
        journal, "o:875-held", window=_875_NEIGHBOUR_WINDOW)
    crossing = _875_snapshot(journal, "o:875-0-2")
    closed = _875_preserved_close(journal, 6.0)
    records = [
        closed, held, crossing,
        _875_referencing(_875_milestone(journal, 1, 3.0, None), held),
        _875_milestone(journal, 2, 6.0, current_marginal),
    ]
    desired = [
        held, crossing,
        _875_referencing(
            _875_milestone(journal, 1, 7.0 * card, None, tokens=110), held),
        _875_milestone(journal, 2, 14.0 * card, 7.0 * card),
    ]
    baseline, reviewed, causal, current = _875_causal_plans(
        rederive, journal, records, desired, desired, preserved=[closed],
        reviewed_hold_ids=("o:875-held",),
    )
    block = [_875_PRESERVED_CLOSE_ID, _875_ONE, _875_TWO]
    summary = [_875_action_summary(plan)
               for plan in (baseline, reviewed, causal)]

    assert _875_money(_875_milestone_actions(baseline)[_875_TWO]) == (
        6.0, 6.0 - 7.0 * card), summary
    reviewed_actions = _875_milestone_actions(reviewed)
    causal_actions = _875_milestone_actions(causal)
    if current_marginal == 3.0:
        assert reviewed_actions == {}, summary
        assert causal_actions == {}, summary
    else:
        assert list(reviewed_actions) == [_875_TWO], summary
        assert _875_money(reviewed_actions[_875_TWO]) == (6.0, 3.0), summary
        assert list(causal_actions) == [_875_TWO], summary
        assert _875_money(causal_actions[_875_TWO]) == (6.0, 3.0), summary
    assert _875_applied(current, causal.actions, block) == \
        _875_applied(current, reviewed.actions, block)


@pytest.mark.parametrize("card", _875_PRICING_STATES)
def test_875_causal_subset_leaves_an_unchanged_kept_close_block_alone(
    cctally_module, card,
):
    """Spec §6.1 test 23, unchanged kept-close control (A12).

    A card edit alone keeps the close, so neither plan acts on the block and
    neither does the subset: the block keeps its frozen amounts.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    held = _875_snapshot(
        journal, "o:875-held", window=_875_NEIGHBOUR_WINDOW)
    crossing = _875_snapshot(journal, "o:875-0-2")
    records = [
        _875_priced_close(journal, 6.0), held, crossing,
        _875_referencing(_875_milestone(journal, 1, 3.0, None), held),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    desired = [
        _875_priced_close(journal, 14.0 * card), held, crossing,
        _875_referencing(_875_milestone(journal, 1, 7.0 * card, None), held),
        _875_milestone(journal, 2, 14.0 * card, 7.0 * card),
    ]
    baseline, reviewed, causal, current = _875_causal_plans(
        rederive, journal, records, desired, desired,
        reviewed_hold_ids=("o:875-held",),
    )
    block = [_875_CLOSE_ID, _875_ONE, _875_TWO]
    summary = [_875_action_summary(plan)
               for plan in (baseline, reviewed, causal)]

    for plan in (baseline, reviewed, causal):
        assert not set(_875_ids(plan)) & set(block), summary
    assert _875_applied(current, causal.actions, block) == \
        _875_applied(current, (), block)


@pytest.mark.parametrize("card", _875_PRICING_STATES)
def test_875_causal_closure_reads_a_milestone_tombstones_block_from_current(
    cctally_module, card,
):
    """Spec §6.1 test 23, a milestone tombstone under a replaced close (A12).

    Only the reviewed replay drops threshold 2, so the filter keeps its
    tombstone alone. A tombstone has no payload; its block comes from its
    current record, and the closure then brings the identical close and
    threshold-1 corrections along, so the block lands whole.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    snapshots = [
        _875_snapshot(journal, "o:875-0-1"), _875_snapshot(journal, "o:875-0-2"),
    ]
    records = [
        _875_priced_close(journal, 6.0), *snapshots,
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    reviewed_desired = [
        _875_moved_close(journal, 14.0 * card), *snapshots,
        _875_milestone(journal, 1, 7.0 * card, None),
    ]
    baseline_desired = [
        *reviewed_desired,
        _875_milestone(journal, 2, 14.0 * card, 7.0 * card),
    ]
    baseline, reviewed, causal, current = _875_causal_plans(
        rederive, journal, records, baseline_desired, reviewed_desired,
    )
    block = [_875_CLOSE_ID, _875_ONE, _875_TWO]
    summary = [_875_action_summary(plan)
               for plan in (baseline, reviewed, causal)]

    assert [(a.disposition, a.event_id) for a in reviewed.actions] == [
        ("supersede", _875_CLOSE_ID), ("supersede", _875_ONE),
        ("tombstone", _875_TWO),
    ], summary
    assert [a.to_correction_action() for a in baseline.actions[:2]] == [
        a.to_correction_action() for a in reviewed.actions[:2]
    ], summary
    # A replaced close: both plans change its money (spec §4.1).
    assert _875_close_total_moves(baseline, 6.0), summary
    assert _875_close_total_moves(reviewed, 6.0), summary
    assert _875_ids(causal) == block, summary
    assert causal.actions[2].disposition == "tombstone"
    assert _875_applied(current, causal.actions, block) == \
        _875_applied(current, reviewed.actions, block)


def test_875_hold_supported_close_tombstoned_next_takes_no_extra_pass(
    cctally_module,
):
    """Spec §6.1 test 24 (A3, §7.7): the second pre-existing close sequence.

    An ordinary close is kept only because the reviewed hold injects it from
    a held snapshot the same plan tombstones. The next plan no longer sees
    the snapshot, so it tombstones the close: the close's own decision takes
    a second pass. Its milestones are frozen while the close is kept and
    follow replay once it is gone, so their actions accompany that close
    tombstone and #875 adds no pass. The pre-#875 planner, which never froze
    them, reaches its no-op after the same number of plans.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    held = _875_snapshot(journal, "o:875-held")
    records = [
        _875_priced_close(journal, 6.0), held,
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    desired = [
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    kwargs = {"reviewed_weekly_hold_ids": ("o:875-held",)}

    fixed = _875_run_passes(rederive, journal, records, desired, **kwargs)
    pre = _875_run_passes(
        _875_pre_875_planner(), journal, records, desired, **kwargs)
    summary = {
        "fixed": [_875_action_summary(plan) for plan in fixed],
        "pre-875": [_875_action_summary(plan) for plan in pre],
    }

    assert _875_ids(fixed[0]) == [held["id"]], summary
    assert [(a.disposition, a.event_id) for a in fixed[1].actions] == [
        ("tombstone", _875_CLOSE_ID),
        ("supersede", _875_ONE), ("supersede", _875_TWO),
    ], summary
    assert [_875_money(a) for a in fixed[1].actions[1:]] == [
        (7.0, None), (14.0, 7.0),
    ], summary
    assert [(a.disposition, a.event_id) for a in pre[1].actions] == [
        ("tombstone", _875_CLOSE_ID),
    ], summary
    # §7.7 evidence: both planners reach the no-op at the third plan.
    assert (len(pre), len(fixed)) == (3, 3), summary


def _875_axis_close(journal, total, *, end, crossed, **overrides):
    """A priced close with explicit weekly axes (spec §1.7)."""
    return _875_close(
        journal, total=total,
        models=[{"model": "m", "cost_usd": total, "input_tokens": 300}],
        seven_day_pct_at_block_start=60.0, seven_day_pct_at_block_end=end,
        crossed_seven_day_reset=crossed, **overrides,
    )


@pytest.mark.parametrize("successor_marginal", [3.0, 2.0])
def test_875_money_preserving_close_correction_freezes_its_block(
    cctally_module, successor_marginal,
):
    """Spec §6.1 test 25, base case (§1.7; A1, A3).

    A genuinely priced $6 close sits in a held window. The reviewed hold
    merges only replay's weekly axes into it and keeps its payload, money
    included, so the plan corrects the close without moving its total: a
    money-preserving correction, which is frozen. The unchanged crossing at
    threshold 2 keeps its $6 block cost although replay prices it at $14,
    and its marginal follows the final chain, 6 - 3 = 3 against the held
    predecessor. With a recorded marginal of 3 that takes no action; with 2
    it writes (6, 3). Revision 11 classified the close as replaced because
    its content changed, and repriced the crossing to (14, 11) above its
    unchanged $6 close. The second plan is a no-op.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    held = _875_snapshot(journal, "o:875-held")
    records = [
        _875_axis_close(journal, 6.0, end=63.0, crossed=0), held,
        _875_referencing(_875_milestone(journal, 1, 3.0, None), held),
        _875_milestone(journal, 2, 6.0, successor_marginal),
    ]
    desired = [
        _875_axis_close(journal, 14.0, end=15.0, crossed=1),
        _875_referencing(_875_milestone(journal, 1, 7.0, None), held),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    plans = _875_run_passes(
        rederive, journal, records, desired,
        reviewed_weekly_hold_ids=("o:875-held",),
    )
    summary = [_875_action_summary(plan) for plan in plans]
    first = plans[0]

    assert _875_ids(first)[0] == _875_CLOSE_ID, summary
    close = first.actions[0].payload
    assert (close["total_cost_usd"], close["seven_day_pct_at_block_end"],
            close["crossed_seven_day_reset"]) == (6.0, 15.0, 1), summary
    milestones = _875_milestone_actions(first)
    if successor_marginal == 3.0:
        assert milestones == {}, summary
    else:
        assert list(milestones) == [_875_TWO], summary
        assert _875_money(milestones[_875_TWO]) == (6.0, 3.0), summary
    assert len(plans) == 2, summary


@pytest.mark.parametrize("current_total, desired_total, frozen", [
    (6.0, 6.0, True),
    (6.0, 6.0 + 5e-10, True),
    (6.0, 6.0 - 5e-10, True),
    (6.0, 6.0 + 2e-9, False),
    (6.0, 6.0 - 2e-9, False),
    (6.0, 14.0, False),
    (None, 6.0, False),
    (6.0, "6.0", False),
    (True, 1.0, False),
], ids=repr)
def test_875_close_correction_is_frozen_only_while_it_keeps_the_money(
    cctally_module, current_total, desired_total, frozen,
):
    """Spec §6.1 test 25, tolerance pair (§4.1; A1, A3).

    Replay moves the close's final five-hour reading, closure evidence the
    keep rule does not absorb, so the plan corrects the close. That
    correction is frozen only when both totals are usable (a finite ``int``
    or ``float``, not a ``bool``) and within 1e-9 USD of each other: the
    unchanged crossings then keep their $3 and $6. Otherwise it is replaced,
    and every matched milestone takes replay's (7, None) and (14, 7). Either
    way the second plan is a no-op.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    models = [{"model": "m", "cost_usd": 6.0, "input_tokens": 300}]
    current = [
        _875_close(journal, total=current_total, models=models),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    desired = [
        _875_close(journal, total=desired_total, models=models,
                   final_five_hour_percent=43.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    first = _875_plan(rederive, journal, current, desired)
    corrected = _875_corrected_state(journal, current, first)
    second = _875_plan(rederive, journal, corrected, desired)
    summary = (_875_action_summary(first), _875_action_summary(second))

    assert _875_ids(first)[0] == _875_CLOSE_ID, summary
    close = first.actions[0].payload
    assert close["final_five_hour_percent"] == 43.0
    total = close["total_cost_usd"]
    assert (total, type(total)) == (desired_total, type(desired_total))
    if frozen:
        assert _875_ids(first) == [_875_CLOSE_ID], summary
    else:
        assert _875_ids(first) == [_875_CLOSE_ID, _875_ONE, _875_TWO], summary
        assert [_875_money(action) for action in first.actions[1:]] == [
            (7.0, None), (14.0, 7.0),
        ], summary
    assert second.actions == (), summary


def test_875_open_close_corrected_to_closed_is_replaced(cctally_module):
    """Spec §6.1 test 25, open to closed (§4.1; A1, A3).

    Current holds the block's close open, and replay closes it at the same
    $6 total. The current record is not closed, so this plan writes the
    close's money and the close is replaced although the totals are equal:
    both matched milestones take replay's (7, None) and (14, 7). The second
    plan keeps the close and is a no-op.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    models = [{"model": "m", "cost_usd": 6.0, "input_tokens": 300}]
    current = [
        _875_close(journal, total=6.0, models=models, is_closed=0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    desired = [
        _875_close(journal, total=6.0, models=models),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    first = _875_plan(rederive, journal, current, desired)
    corrected = _875_corrected_state(journal, current, first)
    second = _875_plan(rederive, journal, corrected, desired)
    summary = (_875_action_summary(first), _875_action_summary(second))

    assert _875_ids(first) == [_875_CLOSE_ID, _875_ONE, _875_TWO], summary
    close = first.actions[0].payload
    assert (close["is_closed"], close["total_cost_usd"]) == (1, 6.0)
    assert [_875_money(action) for action in first.actions[1:]] == [
        (7.0, None), (14.0, 7.0),
    ], summary
    assert second.actions == (), summary


@pytest.mark.parametrize("card", _875_PRICING_STATES)
def test_875_causal_closure_restores_an_open_to_closed_close_correction(
    cctally_module, card,
):
    """Spec §6.1 test 25, open to closed, causal variant (§4.5; A12).

    Both plans close the open close at its unchanged $6 total, so the B/R
    filter drops that identical correction. The reviewed plan's hold keeps
    the predecessor at $3 through test 23's neighbouring-window hold, while
    the baseline reprices it, so the baseline targets the successor at
    (14, 7) and the reviewed plan at (14, 11). The close is replaced, so the
    closure restores the dropped close correction beside the successor.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    held = _875_snapshot(
        journal, "o:875-held", window=_875_NEIGHBOUR_WINDOW)
    crossing = _875_snapshot(journal, "o:875-0-2")
    models = [{"model": "m", "cost_usd": 6.0, "input_tokens": 300}]
    records = [
        _875_close(journal, total=6.0, models=models, is_closed=0),
        held, crossing,
        _875_referencing(_875_milestone(journal, 1, 3.0, None), held),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    desired = [
        _875_close(journal, total=6.0, models=models), held, crossing,
        _875_referencing(_875_milestone(journal, 1, 7.0 * card, None), held),
        _875_milestone(journal, 2, 14.0 * card, 7.0 * card),
    ]
    baseline, reviewed, causal, current = _875_causal_plans(
        rederive, journal, records, desired, desired,
        reviewed_hold_ids=("o:875-held",),
    )
    block = [_875_CLOSE_ID, _875_ONE, _875_TWO]
    summary = [_875_action_summary(plan)
               for plan in (baseline, reviewed, causal)]

    assert _875_ids(baseline) == block, summary
    assert _875_ids(reviewed) == [_875_CLOSE_ID, _875_TWO], summary
    assert baseline.actions[0].to_correction_action() == \
        reviewed.actions[0].to_correction_action()
    assert (reviewed.actions[0].payload["is_closed"],
            reviewed.actions[0].payload["total_cost_usd"]) == (1, 6.0)
    assert _875_money(baseline.actions[2]) == (
        14.0 * card, 7.0 * card), summary
    assert _875_money(reviewed.actions[1]) == (
        14.0 * card, 14.0 * card - 3.0), summary
    assert _875_ids(causal) == [_875_CLOSE_ID, _875_TWO], summary
    assert _875_applied(current, causal.actions, block) == \
        _875_applied(current, reviewed.actions, block)


def test_875_frozen_close_wins_a_block_with_a_replaced_close(cctally_module):
    """Spec §6.1 test 25, frozen wins (§4.1; A1, A3).

    The block holds a preserved ``b:`` close the plan retains, and replay
    adds a natural close for the same block. A block holding a frozen close
    is frozen, so the unchanged crossings keep their $3 and $6 instead of
    following the added close to replay's (7, 14), and the plan's only
    action is the add. The second plan is a no-op.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    records = [
        _875_preserved_close(journal, 6.0),
        _875_milestone(journal, 1, 3.0, None),
        _875_milestone(journal, 2, 6.0, 3.0),
    ]
    desired = [
        _875_priced_close(journal, 14.0),
        _875_milestone(journal, 1, 7.0, None),
        _875_milestone(journal, 2, 14.0, 7.0),
    ]
    plans = _875_run_passes(rederive, journal, records, desired)
    summary = [_875_action_summary(plan) for plan in plans]

    assert [(a.disposition, a.event_id) for a in plans[0].actions] == [
        ("add", _875_CLOSE_ID),
    ], summary
    assert plans[0].preserved_event_count == 1
    assert len(plans) == 2, summary


_875_THREE = f"fhm:{_875_ACCOUNT}:{_875_WINDOW}:0:3"


def _875_frozen_sibling_fixture(journal, current_close, desired_close, card):
    """(records, desired) for a frozen block with a divergent successor.

    The neighbouring-window hold keeps threshold 1 at $3 in the reviewed
    plan only, while the baseline corrects its crossing (``tokens=110``) at
    today's card. Threshold 2 is then the one milestone whose targets
    differ; threshold 3 follows threshold 2's frozen $6 identically in both
    plans, so the B/R filter drops it.
    """
    held = _875_snapshot(
        journal, "o:875-held", window=_875_NEIGHBOUR_WINDOW)
    crossings = [
        _875_snapshot(journal, "o:875-0-2"), _875_snapshot(journal, "o:875-0-3"),
    ]
    records = [
        current_close, held, *crossings,
        _875_referencing(_875_milestone(journal, 1, 3.0, None), held),
        _875_milestone(journal, 2, 6.0, 2.0),
        _875_milestone(journal, 3, 9.0, 2.5),
    ]
    desired = [
        desired_close, held, *crossings,
        _875_referencing(
            _875_milestone(journal, 1, 7.0 * card, None, tokens=110), held),
        _875_milestone(journal, 2, 14.0 * card, 7.0 * card),
        _875_milestone(journal, 3, 21.0 * card, 7.0 * card),
    ]
    return records, desired


def _875_assert_only_the_successor_is_causal(baseline, reviewed, causal,
                                             current, dropped, card):
    """The filter drops ``dropped`` and the closure never brings it back."""
    summary = [_875_action_summary(plan)
               for plan in (baseline, reviewed, causal)]
    baseline_by_id = {
        action.event_id: action.to_correction_action()
        for action in baseline.actions
    }
    reviewed_by_id = {action.event_id: action for action in reviewed.actions}

    for event_id in dropped:
        assert baseline_by_id[event_id] == \
            reviewed_by_id[event_id].to_correction_action(), summary
    assert _875_money(reviewed_by_id[_875_THREE]) == (9.0, 3.0), summary
    assert _875_money(reviewed_by_id[_875_TWO]) == (6.0, 3.0), summary
    assert _875_money(next(
        action for action in baseline.actions if action.event_id == _875_TWO
    )) == (6.0, 6.0 - 7.0 * card), summary
    assert _875_ids(causal) == [_875_TWO], summary
    block = [_875_ONE, _875_TWO, _875_THREE]
    assert _875_applied(current, causal.actions, block)[_875_TWO][1][
        "block_cost_usd"] == 6.0


@pytest.mark.parametrize("card", _875_PRICING_STATES)
def test_875_causal_closure_never_replaces_a_frozen_block(cctally_module, card):
    """Spec §6.1 test 25, frozen wins, causal variant (§4.5; A12).

    The block holds a retained preserved ``b:`` close, and both plans add
    the same natural close, a replaced close, beside it. The block is
    frozen, so the subset's one milestone action, threshold 2's marginal
    correction, pulls in neither the added close's action nor threshold 3's
    identical sibling correction, both of which the filter dropped.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    closed = _875_preserved_close(journal, 6.0)
    records, desired = _875_frozen_sibling_fixture(
        journal, closed, _875_priced_close(journal, 14.0 * card), card)
    baseline, reviewed, causal, current = _875_causal_plans(
        rederive, journal, records, desired, desired, preserved=[closed],
        reviewed_hold_ids=("o:875-held",),
    )

    assert [(a.disposition, a.event_id) for a in reviewed.actions] == [
        ("add", _875_CLOSE_ID), ("supersede", _875_TWO),
        ("supersede", _875_THREE),
    ], _875_action_summary(reviewed)
    _875_assert_only_the_successor_is_causal(
        baseline, reviewed, causal, current, [_875_CLOSE_ID, _875_THREE],
        card)


@pytest.mark.parametrize("card", _875_PRICING_STATES)
def test_875_causal_closure_skips_a_money_preserving_close_correction(
    cctally_module, card,
):
    """Spec §4.5 (A12), the tolerance pair's causal counterpart.

    Both plans correct the close's closure evidence at its unchanged $6
    total, a money-preserving correction the filter drops as identical. The
    block is frozen, so the subset's one milestone action, threshold 2's
    marginal correction, pulls in neither that close correction nor
    threshold 3's identical sibling correction.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    records, desired = _875_frozen_sibling_fixture(
        journal, _875_priced_close(journal, 6.0),
        _875_moved_close(journal, 6.0), card)
    baseline, reviewed, causal, current = _875_causal_plans(
        rederive, journal, records, desired, desired,
        reviewed_hold_ids=("o:875-held",),
    )

    assert _875_ids(reviewed) == [
        _875_CLOSE_ID, _875_TWO, _875_THREE,
    ], _875_action_summary(reviewed)
    assert not _875_close_total_moves(reviewed, 6.0)
    _875_assert_only_the_successor_is_causal(
        baseline, reviewed, causal, current, [_875_CLOSE_ID, _875_THREE],
        card)


@pytest.mark.parametrize("successor_marginal", [3.0, 2.0])
@pytest.mark.parametrize("card", _875_PRICING_STATES)
def test_875_causal_subset_keeps_a_weekly_axis_corrected_close_frozen(
    cctally_module, card, successor_marginal,
):
    """Spec §6.1 test 25, causal weekly-axis variant (§4.5; A12).

    The reviewed plan's hold corrects only the close's weekly axes and keeps
    its $6, while the baseline, which holds nothing, reprices the close and
    both milestones at today's card. The close is frozen in the reviewed
    plan, so its unchanged crossing is targeted at its current $6, and the
    subset never changes that block cost: with a recorded marginal of 3 it
    takes no milestone action, and with 2 only the marginal-only (6, 3).
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    held = _875_snapshot(journal, "o:875-held")
    crossing = _875_snapshot(journal, "o:875-0-2")
    records = [
        _875_axis_close(journal, 6.0, end=63.0, crossed=0), held, crossing,
        _875_referencing(_875_milestone(journal, 1, 3.0, None), held),
        _875_milestone(journal, 2, 6.0, successor_marginal),
    ]
    desired = [
        _875_axis_close(journal, 14.0 * card, end=15.0, crossed=1),
        held, crossing,
        _875_referencing(_875_milestone(journal, 1, 7.0 * card, None), held),
        _875_milestone(journal, 2, 14.0 * card, 7.0 * card),
    ]
    baseline, reviewed, causal, current = _875_causal_plans(
        rederive, journal, records, desired, desired,
        reviewed_hold_ids=("o:875-held",),
    )
    block = [_875_CLOSE_ID, _875_ONE, _875_TWO]
    summary = [_875_action_summary(plan)
               for plan in (baseline, reviewed, causal)]

    assert _875_ids(baseline) == block, summary
    assert baseline.actions[0].payload["total_cost_usd"] == 14.0 * card
    assert _875_money(baseline.actions[2]) == (14.0 * card, 7.0 * card)
    weekly = reviewed.actions[0].payload
    assert (weekly["total_cost_usd"], weekly["seven_day_pct_at_block_end"],
            weekly["crossed_seven_day_reset"]) == (6.0, 15.0, 1), summary
    causal_milestones = _875_milestone_actions(causal)
    if successor_marginal == 3.0:
        assert causal_milestones == {}, summary
    else:
        assert list(causal_milestones) == [_875_TWO], summary
        assert _875_money(causal_milestones[_875_TWO]) == (6.0, 3.0), summary
    applied = _875_applied(current, causal.actions, block)
    assert applied[_875_TWO][1]["block_cost_usd"] == 6.0, summary
    assert applied == _875_applied(current, reviewed.actions, block)


@pytest.mark.parametrize("card", _875_PRICING_STATES)
def test_875_causal_closure_skips_a_reaffirmed_close_with_an_unusable_total(
    cctally_module, card,
):
    """Spec §4.1, §4.5 (A12): a forced-conflict reaffirmation is a kept close.

    The kept close's total is unusable, so the money rule alone cannot call
    its reaffirmation frozen, yet it re-writes the current content and the
    resolver froze its block. Both plans reaffirm it identically, so the
    filter drops it; the subset's one milestone action, threshold 2's
    marginal correction, must pull in neither the reaffirmation nor threshold
    3's identical sibling correction.
    """
    import _lib_journal as journal
    import _lib_rederive as rederive

    models = [{"model": "m", "cost_usd": 6.0, "input_tokens": 300}]
    records, desired = _875_frozen_sibling_fixture(
        journal, _875_close(journal, total=None, models=models),
        _875_priced_close(journal, 14.0 * card), card)
    baseline, reviewed, causal, current = _875_causal_plans(
        rederive, journal, records, desired, desired,
        reviewed_hold_ids=("o:875-held",),
        conflicted_event_ids=(_875_CLOSE_ID,),
    )

    assert _875_ids(reviewed) == [
        _875_CLOSE_ID, _875_TWO, _875_THREE,
    ], _875_action_summary(reviewed)
    reaffirmed = reviewed.actions[0]
    assert (reaffirmed.disposition, reaffirmed.payload) == (
        "supersede", current[_875_CLOSE_ID]["payload"])
    _875_assert_only_the_successor_is_causal(
        baseline, reviewed, causal, current, [_875_CLOSE_ID, _875_THREE],
        card)
