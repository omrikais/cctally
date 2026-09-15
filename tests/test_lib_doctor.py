"""Unit tests for bin/_lib_doctor.py. See plan Tasks 1-11."""
import sys
import pathlib

# Add bin/ to path so `import _lib_doctor` resolves.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import dataclasses as dc
import datetime as dt
import pathlib

import pytest

import _lib_doctor as L


def test_doctor_iso_z_naive_means_utc_and_keeps_microseconds():
    # #279 S6 W4 (gate F12): doctor's _iso_z deliberately DIVERGES from the
    # canonical _lib_json_envelope._iso_z. It treats a naive datetime as UTC
    # (replace(tzinfo=utc), NOT astimezone — which would reinterpret it as
    # host-local) and preserves microseconds (isoformat, not %S). The doctor
    # goldens use aware whole-second timestamps and cannot prove this, so it is
    # pinned directly here; the divergence is why doctor keeps its own name.
    naive = dt.datetime(2026, 7, 10, 12, 0, 5, 123456)
    assert L._iso_z(naive) == "2026-07-10T12:00:05.123456Z"


def test_doctor_state_has_required_fields():
    fields = {f.name for f in dc.fields(L.DoctorState)}
    expected = {
        "symlink_state", "path_includes_local_bin",
        "legacy_snippet", "legacy_bespoke",
        "claude_settings", "hook_counts", "log_activity_24h",
        "oauth_token_present",
        "stats_db_status", "cache_db_status",
        "latest_snapshot_at",
        "cache_entries_count", "cache_last_entry_at", "claude_jsonl_present",
        "codex_entries_count", "codex_last_entry_at", "codex_jsonl_present",
        # #312: safe all-history Codex project-metadata health evidence.
        "codex_project_metadata_health", "codex_project_metadata_error",
        # public #5: the hook's budgeted-ingest backlog record.
        "codex_ingest_backlog",
        "dashboard_bind_stored", "runtime_bind",
        "config_json_error",
        "update_state", "update_state_error",
        "update_suppress", "update_suppress_error",
        "effective_update_available", "effective_update_reason",
        "now_utc", "cctally_version",
        "forked_bucket_counts",
        "credited_weeks",
        "dev_mode", "app_dir", "is_dev_checkout",
        # Issue #119: availability-aware install checks.
        "cctally_reachable_on_path", "symlinks_path_pinned",
        "install_is_brew",
        # Pricing-freshness check (spec 2026-05-29).
        "pricing_coverage",
        # Conversation viewer (Plan 2, spec §5): LAN transcript opt-in.
        "expose_transcripts",
        # Conversation-sessions rollup consistency (#217 S1 / U9).
        "conv_sessions_rollup_count",
        "conv_messages_distinct_sessions",
        "conv_rollup_sync_in_progress",
        # Preview channel (CCTALLY_CHANNEL=preview): surfaced in install.mode.
        "channel",
        # Anonymous install-count telemetry (spec 2026-07-07): opt-out state.
        "telemetry_enabled", "telemetry_reason",
        # #279 S2 F5: parse-health / deep quick_check / lock-state probes.
        "parse_health_claude", "parse_health_codex",
        "stats_db_quick_check", "cache_db_quick_check",
        "conversations_db_quick_check",
        "locks_held",
        # #297: read-only cache.db WAL-size backstop.
        "cache_db_wal_bytes",
        # #583 S4 / F39: the same backstop for the transcript store.
        "conversations_db_wal_bytes",
        # #315: read-only cache.db free-page evidence.
        "cache_db_page_count", "cache_db_freelist_count",
        "conversations_db_page_count", "conversations_db_freelist_count",
        # #780: the durable transcript reclaim backlog, absent on every store
        # that has never fallen behind.
        "conversations_reclaim_pending",
        # #344 Task B: privacy-safe repair-owner classification.
        "cache_repair_marker",
        # #411: file-level backup/sync classification.
        "backup_sync_state",
        # #294 S2: root-qualified Codex quota/lifecycle doctor inputs.
        "codex_quota_windows", "codex_hook_roots",
        "codex_lifecycle_activity_24h",
        # #719 §2.5: normalized Codex hook success-marker evidence.
        "codex_hook_liveness",
        # public #5: 24h outcomes for the detached `_codex-quota-verify`
        # worker, which every hook-side whole-history pass now depends on. The
        # lifecycle counts cannot cover it — worker lines carry no
        # `source_root_key`, so that parser's root filter drops all of them.
        "codex_quota_verify_activity",
        # #311: precomputed statusLine.refreshInterval classification.
        "statusline_refresh_state",
        # #318: passive statusline candidate/selected pipeline evidence.
        "statusline_pipeline",
        # Beta-channel (spec 2026-07-21 §3): configured update (release) channel.
        "update_channel",
        # DB journal redesign §9: append-only journal doctor legs.
        "journal_present", "journal_appendable", "journal_segment_count",
        "journal_has_bytes",
        "journal_malformed_count", "journal_torn_tail_count",
        "journal_cursor_lag_bytes", "journal_hw_segment",
        "journal_cursor_segment", "journal_heal_incidents",
        # #386: the stats sole-writer guard log leg (journal.writer_guard).
        "journal_writer_guard",
        # #374/#402: event conflicts, whole-batch taint, selector failure.
        "journal_conflicts", "journal_protocol_violations",
        "journal_protocol_acknowledged",
        "journal_protocol_error",
        # #496 S5b §4.7: the durable incomplete-quota-projection flag the
        # published generation carries, read by journal.quota_projection. The
        # flag can stay set indefinitely — only a reconciliation or a later
        # complete rebuild clears it — so a read-only leg has to name the cause.
        "stats_quota_projection_incomplete",
        # #341 Task 3: multi-account health legs (accounts.identity/registry/
        # freshness/attribution) all read off this single gathered snapshot.
        "accounts_state",
        # #416 review B4: durable record of a torn Codex auth.json halting
        # ingest (accounts.codex_identity).
        "codex_torn_deferred",
        # The byte-zero Codex replay stall signal (data.codex_replay): the
        # pending marker plus the record a completed-but-unsuccessful whole-tree
        # walk writes when it cannot consume it.
        # public #5: plus the record a BUDGETED tick writes when it declines
        # the replay outright — the only stall signal a hook-only install
        # produces, since that decline never reaches the walk.
        "codex_replay_pending", "codex_replay_blocked", "codex_replay_deferred",
        # #485: privacy-safe unsafe-orphan-prune refusal records.
        "codex_prune_refusals",
        # #496 S6 §7.2: the durable heal ring, read as DETECTIONS. A detection
        # is not an incident — a declined or coalesced one writes a ring entry
        # and no quarantine directory at all, and only the ring reports a rate.
        "journal_heal_detections",
        # #496 S6 §7.3: the read-only retention scan and plan behind
        # db.retained_artifacts.
        "retained_artifacts",
        # #661 S2 §7: the `quota` category's two inputs, gathered through the
        # non-mutating calibration reader.
        "quota_calibration",
        "quota_rate_change",
        # #705: the conversation rollup's refused-pricing-write record, plus
        # the stored fingerprint that separates an ordinary refusal from a
        # value no version can parse. TWO stores carry both, because the
        # ordered-write guard runs on the cache.db connection in `sync_cache`
        # and on the conversations.db connection in
        # `sync_claude_conversations`.
        "conversation_rollup_pricing_refusal",
        "conversation_rollup_pricing_fp",
        "cache_rollup_pricing_refusal",
        "cache_rollup_pricing_fp",
        # #728: the four-state observation of each store's fingerprint read.
        # The fingerprint alone cannot say whether the read happened, so a
        # store doctor failed to read was indistinguishable from one that
        # recorded nothing.
        "conversation_rollup_pricing_observation",
        "cache_rollup_pricing_observation",
    }
    assert fields == expected, fields ^ expected


def test_check_result_severity_is_string():
    r = L.CheckResult(
        id="x", title="X", severity="ok", summary="all good",
        remediation=None, details={},
    )
    assert r.severity == "ok"


def test_category_result_holds_tuple_of_checks():
    r = L.CategoryResult(id="install", title="Install", severity="ok", checks=())
    assert isinstance(r.checks, tuple)


def test_doctor_report_overall_fields():
    rep = L.DoctorReport(
        schema_version=1,
        generated_at=dt.datetime(2026, 5, 13, 14, 22, 31, tzinfo=dt.timezone.utc),
        cctally_version="1.6.3",
        overall_severity="ok",
        counts={"ok": 1, "warn": 0, "fail": 0},
        categories=(),
    )
    assert rep.schema_version == 1


def test_codex_quota_doctor_registers_stable_contract_checks():
    """S2 doctor IDs are additive and remain stable JSON-contract keys."""
    check_ids = {
        check.id
        for category in L.run_checks(_state()).categories
        for check in category.checks
    }

    assert {
        "data.codex_quota",
        "hooks.codex_installed",
        "hooks.codex_recent_activity",
        "hooks.codex_liveness_7d",
    } <= check_ids


def _state(**overrides) -> L.DoctorState:
    """Helper: build a happy-path DoctorState with overrides."""
    base = dict(
        symlink_state=[("cctally", "ok")],
        path_includes_local_bin=True,
        legacy_snippet=None,
        legacy_bespoke={"detected": False, "settings_entries": [], "files": []},
        claude_settings={},
        hook_counts={"PostToolBatch": 1, "Stop": 1, "SubagentStop": 1},
        log_activity_24h={"fires": 4, "errors": 0,
                          "by_event": {"PostToolBatch": 2, "Stop": 1, "SubagentStop": 1},
                          "last_fire_ago_s": 4, "oauth_ok": 4, "throttled": 0},
        oauth_token_present=True,
        stats_db_status={"path": "/tmp/x.db", "user_version": 7, "registry_size": 7,
                         "migrations": [{"seq": 1, "name": "001_x", "status": "applied"}]},
        cache_db_status={"path": "/tmp/c.db", "user_version": 3, "registry_size": 3,
                         "migrations": []},
        latest_snapshot_at=dt.datetime(2026, 5, 13, 14, 22, 29, tzinfo=dt.timezone.utc),
        cache_entries_count=1_000,
        cache_last_entry_at=dt.datetime(2026, 5, 13, 14, 22, 27, tzinfo=dt.timezone.utc),
        claude_jsonl_present=True,
        # Happy path: stats.db is clean — no forked-bucket rows.
        forked_bucket_counts={"usage": 0, "cost": 0, "milestones": 0},
        # Happy path: no credited weeks yet.
        credited_weeks=[],
        codex_entries_count=0,
        codex_last_entry_at=None,
        codex_jsonl_present=False,
        dashboard_bind_stored="loopback",
        runtime_bind=None,
        config_json_error=None,
        update_state={"current_version": "1.6.3", "latest_version": "1.6.3"},
        update_state_error=None,
        # Canonical default record per bin/cctally:9731 (_load_update_suppress).
        update_suppress={"_schema": 1, "skipped_versions": [], "remind_after": None},
        update_suppress_error=None,
        # Defaults match the happy-path update_state (cur == lat → "no_newer");
        # individual tests can override these alongside `update_state` to drive
        # the warn / skipped / reminded paths.
        effective_update_available=False,
        effective_update_reason="no_newer",
        now_utc=dt.datetime(2026, 5, 13, 14, 22, 31, tzinfo=dt.timezone.utc),
        cctally_version="1.6.3",
        # Dev-instance isolation: happy path is the installed (prod) instance.
        dev_mode=False,
        app_dir="/home/u/.local/share/cctally",
        # Conversation-sessions rollup consistency (#217 S1 / U9): happy path is
        # an equal, quiescent rollup.
        conv_sessions_rollup_count=10,
        conv_messages_distinct_sessions=10,
        conv_rollup_sync_in_progress=False,
        statusline_pipeline=None,
    )
    base.update(overrides)
    return L.DoctorState(**base)


def test_statusline_pipeline_warns_when_timer_fresh_selection_stale():
    result = L._check_statusline_pipeline(_state(statusline_pipeline={
        "transport_age_seconds": 10,
        "selected_age_seconds": 301,
        "active_candidate_count": 2,
        "control_db_agrees": True,
        "tombstones": {"fiveHour": "absent", "sevenDay": "committed"},
    }))

    assert result.id == "data.statusline_pipeline"
    assert result.severity == "warn"
    assert result.summary == "timer active; selected usage stale"


def test_statusline_pipeline_stale_transport_is_informational():
    result = L._check_statusline_pipeline(_state(statusline_pipeline={
        "transport_age_seconds": 91,
        "selected_age_seconds": 500,
        "active_candidate_count": 0,
        "control_db_agrees": True,
        "tombstones": {"fiveHour": "absent", "sevenDay": "absent"},
    }))

    assert result.severity == "ok"
    assert result.summary == "no recent regular-pool timer observed"


def test_statusline_pipeline_warns_for_invalid_authority_or_control_drift():
    invalid = L._check_statusline_pipeline(_state(statusline_pipeline={
        "transport_age_seconds": None,
        "selected_age_seconds": None,
        "active_candidate_count": 0,
        "control_db_agrees": True,
        "tombstones": {"fiveHour": "invalid", "sevenDay": "absent"},
    }))
    divergent = L._check_statusline_pipeline(_state(statusline_pipeline={
        "transport_age_seconds": 1,
        "selected_age_seconds": 1,
        "active_candidate_count": 1,
        "control_db_agrees": False,
        "tombstones": {"fiveHour": "absent", "sevenDay": "committed"},
    }))

    assert invalid.summary == "authoritative state needs repair"
    assert divergent.severity == "warn"
    assert divergent.summary == "selected control disagrees with database"


def _codex_identity(root: str, limit: str, slot: str, minutes: int) -> dict:
    return {
        "source": "codex",
        "source_root_key": root,
        "logical_limit_key": limit,
        "observed_slot": slot,
        "window_minutes": minutes,
    }


def test_codex_quota_mixed_windows_sort_and_expose_worst_responsible_identity():
    """A fresh root cannot mask a stale root in the doctor aggregate."""
    now = dt.datetime(2026, 5, 13, 14, 22, 31, tzinfo=dt.timezone.utc)
    stale_identity = _codex_identity("root-a", "secondary", "secondary", 60)
    fresh_identity = _codex_identity("root-b", "primary", "primary", 300)
    state = _state(
        now_utc=now,
        codex_quota_windows=[
            {
                "identity": fresh_identity,
                "latest_capture_at": now - dt.timedelta(seconds=10),
                "freshness_state": "fresh",
                "age_seconds": 10,
                "stale_after_seconds": 1800,
            },
            {
                "identity": stale_identity,
                "latest_capture_at": now - dt.timedelta(seconds=901),
                "freshness_state": "stale",
                "age_seconds": 901,
                "stale_after_seconds": 900,
            },
        ],
    )

    result = L._check_data_codex_quota(state)

    assert result.severity == "warn"
    assert result.details == {
        "window_count": 2,
        "latest_capture_at": "2026-05-13T14:22:21Z",
        "freshness_state": "stale",
        "age_seconds": 901,
        "stale_after_seconds": 900,
        "responsible_identity": stale_identity,
        "windows": [
            {
                "identity": stale_identity,
                "latest_capture_at": "2026-05-13T14:07:30Z",
                "freshness_state": "stale",
                "age_seconds": 901,
                "stale_after_seconds": 900,
            },
            {
                "identity": fresh_identity,
                "latest_capture_at": "2026-05-13T14:22:21Z",
                "freshness_state": "fresh",
                "age_seconds": 10,
                "stale_after_seconds": 1800,
            },
        ],
    }


def test_codex_quota_all_fresh_windows_are_ok_and_keep_first_sorted_summary():
    """All applicable fresh windows stay healthy without losing per-root order."""
    now = dt.datetime(2026, 5, 13, 14, 22, 31, tzinfo=dt.timezone.utc)
    first_identity = _codex_identity("root-a", "secondary", "secondary", 60)
    latest_identity = _codex_identity("root-b", "primary", "primary", 300)
    result = L._check_data_codex_quota(_state(
        now_utc=now,
        codex_quota_windows=[
            {
                "identity": latest_identity,
                "latest_capture_at": now - dt.timedelta(seconds=5),
                "freshness_state": "fresh",
                "age_seconds": 5,
                "stale_after_seconds": 1800,
            },
            {
                "identity": first_identity,
                "latest_capture_at": now - dt.timedelta(seconds=10),
                "freshness_state": "fresh",
                "age_seconds": 10,
                "stale_after_seconds": 900,
            },
        ],
    ))

    assert result.id == "data.codex_quota"
    assert result.severity == "ok"
    assert result.summary == "2 window(s); fresh"
    assert result.details == {
        "window_count": 2,
        "latest_capture_at": "2026-05-13T14:22:26Z",
        "freshness_state": "fresh",
        "age_seconds": 10,
        "stale_after_seconds": 900,
        "responsible_identity": first_identity,
        "windows": [
            {
                "identity": first_identity,
                "latest_capture_at": "2026-05-13T14:22:21Z",
                "freshness_state": "fresh",
                "age_seconds": 10,
                "stale_after_seconds": 900,
            },
            {
                "identity": latest_identity,
                "latest_capture_at": "2026-05-13T14:22:26Z",
                "freshness_state": "fresh",
                "age_seconds": 5,
                "stale_after_seconds": 1800,
            },
        ],
    }


def test_codex_quota_future_precedes_stale_and_no_corpus_is_not_applicable():
    now = dt.datetime(2026, 5, 13, 14, 22, 31, tzinfo=dt.timezone.utc)
    future_identity = _codex_identity("root-a", "primary", "primary", 300)
    stale_identity = _codex_identity("root-b", "secondary", "secondary", 60)
    result = L._check_data_codex_quota(_state(
        now_utc=now,
        codex_quota_windows=[
            {
                "identity": stale_identity,
                "latest_capture_at": now - dt.timedelta(seconds=901),
                "freshness_state": "stale",
                "age_seconds": 901,
                "stale_after_seconds": 900,
            },
            {
                "identity": future_identity,
                "latest_capture_at": now + dt.timedelta(seconds=301),
                "freshness_state": "future",
                "age_seconds": -301,
                "stale_after_seconds": 1800,
            },
        ],
    ))
    assert result.severity == "warn"
    assert result.details["freshness_state"] == "future"
    assert result.details["responsible_identity"] == future_identity

    absent = L._check_data_codex_quota(_state(codex_quota_windows=[]))
    assert absent.severity == "ok"
    assert absent.details == {
        "window_count": 0,
        "latest_capture_at": None,
        "freshness_state": "unavailable",
        "age_seconds": None,
        "stale_after_seconds": None,
        "responsible_identity": None,
        "windows": [],
    }

    unsafe_corpus = L._check_data_codex_quota(_state(
        codex_jsonl_present=True, codex_quota_windows=[],
    ))
    assert unsafe_corpus.severity == "warn"


def test_codex_hook_state_and_activity_are_root_qualified_and_never_masked():
    now = dt.datetime(2026, 5, 13, 14, 22, 31, tzinfo=dt.timezone.utc)
    state = _state(
        now_utc=now,
        codex_hook_roots=[
            {"source_root_key": "root-b", "state": "installed_enabled",
             "requires_review": False, "remediation": None},
            {"source_root_key": "root-a", "state": "absent",
             "requires_review": False,
             "remediation": "Run `cctally setup` to install the native Codex handler."},
        ],
        codex_lifecycle_activity_24h={
            "root-a": {"last_tick_at": None, "success_count_24h": 0,
                       "error_count_24h": 2},
            "root-b": {"last_tick_at": now - dt.timedelta(seconds=30),
                       "success_count_24h": 4, "error_count_24h": 0},
        },
    )

    hooks = L._check_hooks_codex_installed(state)
    assert hooks.severity == "warn"
    assert hooks.details == {
        "root_count": 2,
        "installed_root_count": 1,
        "enabled_root_count": 1,
        "states": [
            {"source_root_key": "root-a", "state": "absent"},
            {"source_root_key": "root-b", "state": "installed_enabled"},
        ],
        "requires_review": False,
        "trust_state": "partial",
        "worst_state": "absent",
        "responsible_root_key": "root-a",
    }

    activity = L._check_hooks_codex_recent_activity(state)
    assert activity.severity == "ok"
    assert activity.details == {
        "activity_state": "recent",
        "last_tick_at": "2026-05-13T14:22:01Z",
        "age_seconds": 30,
        "success_count_24h": 4,
        "error_count_24h": 0,
        "responsible_root_key": "root-b",
        "roots": [
            {
                "source_root_key": "root-b",
                "activity_state": "recent",
                "last_tick_at": "2026-05-13T14:22:01Z",
                "age_seconds": 30,
                "success_count_24h": 4,
                "error_count_24h": 0,
            },
        ],
    }


def test_codex_activity_error_only_and_stale_success_warn_without_failing_doctor():
    now = dt.datetime(2026, 5, 13, 14, 22, 31, tzinfo=dt.timezone.utc)
    stale_success = now - dt.timedelta(days=1, seconds=1)
    state = _state(
        now_utc=now,
        codex_hook_roots=[
            {"source_root_key": "root-a", "state": "installed_enabled",
             "requires_review": False, "remediation": None},
            {"source_root_key": "root-b", "state": "installed_enabled",
             "requires_review": False, "remediation": None},
        ],
        codex_lifecycle_activity_24h={
            "root-a": {"last_tick_at": None, "success_count_24h": 0,
                       "error_count_24h": 1},
            "root-b": {"last_tick_at": stale_success, "success_count_24h": 0,
                       "error_count_24h": 1},
        },
        # The 7-day liveness leg reads different evidence and FAILs on its
        # own; keep it healthy so this test still isolates the 24-hour check.
        codex_hook_liveness={
            key: {"last_success_at": now - dt.timedelta(hours=2),
                  "marker_count": 1, "unavailable": False}
            for key in ("root-a", "root-b")
        },
    )

    activity = L._check_hooks_codex_recent_activity(state)
    assert activity.severity == "warn"
    assert activity.details["activity_state"] == "never"
    assert activity.details["responsible_root_key"] == "root-a"
    assert activity.details["roots"] == [
        {
            "source_root_key": "root-a", "activity_state": "never",
            "last_tick_at": None, "age_seconds": None,
            "success_count_24h": 0, "error_count_24h": 1,
        },
        {
            "source_root_key": "root-b", "activity_state": "stale",
            "last_tick_at": "2026-05-12T14:22:30Z", "age_seconds": 86401,
            "success_count_24h": 0, "error_count_24h": 1,
        },
    ]
    report = L.run_checks(state)
    assert report.overall_severity == "warn"
    assert report.counts["fail"] == 0


def test_install_symlinks_all_ok():
    s = _state(symlink_state=[("cctally", "ok"), ("cctally-forecast", "ok")])
    r = L._check_install_symlinks(s)
    assert r.severity == "ok"
    assert r.id == "install.symlinks"
    assert "2/2" in r.summary


def test_install_symlinks_missing_warns():
    s = _state(symlink_state=[("cctally", "ok"), ("cctally-forecast", "missing")])
    r = L._check_install_symlinks(s)
    assert r.severity == "warn"
    assert r.remediation == "Run `cctally setup`"
    assert r.details["missing"] == ["cctally-forecast"]


def test_install_path_ok_and_warn():
    assert L._check_install_path(_state()).severity == "ok"
    assert L._check_install_path(_state(path_includes_local_bin=False)).severity == "warn"


# ── Issue #119: availability-aware install.path ──────────────────────


def test_install_path_ok_when_reachable_even_if_local_bin_off_path():
    s = _state(path_includes_local_bin=False, cctally_reachable_on_path=True)
    r = L._check_install_path(s)
    assert r.severity == "ok"


def test_install_path_warns_when_unreachable():
    s = _state(path_includes_local_bin=False, cctally_reachable_on_path=False)
    r = L._check_install_path(s)
    assert r.severity == "warn"


def test_install_path_warns_when_local_bin_on_path_but_unreachable():
    # The false-OK: `~/.local/bin` is on $PATH (e.g. doctor launched by
    # absolute path / from another UI, or a brew-only install per #119)
    # but no `cctally` executable is actually reachable there. Membership
    # in $PATH must NOT alone satisfy the OK predicate — reachability must.
    s = _state(path_includes_local_bin=True, cctally_reachable_on_path=False)
    r = L._check_install_path(s)
    assert r.severity == "warn"
    assert r.summary == "cctally not reachable on $PATH"


def test_install_path_fail_soft_to_local_bin_when_probe_unavailable():
    # When the reachability probe could not run (`shutil.which` raised →
    # None), fall back to the ~/.local/bin membership proxy so a gather
    # failure never hard-WARNs an otherwise-working install.
    assert L._check_install_path(
        _state(path_includes_local_bin=True, cctally_reachable_on_path=None)
    ).severity == "ok"
    assert L._check_install_path(
        _state(path_includes_local_bin=False, cctally_reachable_on_path=None)
    ).severity == "warn"


def test_install_path_warn_remediation_is_channel_aware():
    # Brew kegs own no ~/.local/bin symlinks (#119), so the WARN
    # remediation must point them at `brew shellenv`, not ~/.local/bin.
    brew = L._check_install_path(
        _state(path_includes_local_bin=False, cctally_reachable_on_path=False,
               install_is_brew=True)
    )
    assert brew.severity == "warn"
    assert "brew shellenv" in brew.remediation
    assert ".local/bin" not in brew.remediation
    # Source / npm installs keep the ~/.local/bin + `cctally setup` hint.
    src = L._check_install_path(
        _state(path_includes_local_bin=False, cctally_reachable_on_path=False,
               install_is_brew=False)
    )
    assert src.severity == "warn"
    assert ".local/bin" in src.remediation
    assert "cctally setup" in src.remediation


# ── Beta-channel (spec 2026-07-21 §3): install.update_channel ────────


def test_update_channel_default_stable_is_ok():
    r = L._check_install_update_channel(_state())
    assert r.id == "install.update_channel"
    assert r.severity == "ok"
    assert r.summary == "stable"
    assert r.details["channel"] == "stable"


def test_update_channel_beta_on_npm_is_ok():
    r = L._check_install_update_channel(
        _state(update_channel="beta", install_is_brew=False)
    )
    assert r.severity == "ok"
    assert r.summary == "beta"


def test_update_channel_beta_on_brew_warns():
    r = L._check_install_update_channel(
        _state(update_channel="beta", install_is_brew=True)
    )
    assert r.severity == "warn"
    assert r.details == {"channel": "beta", "method": "brew"}
    assert "stable" in r.remediation.lower()


def test_update_channel_stable_on_brew_is_ok():
    r = L._check_install_update_channel(
        _state(update_channel="stable", install_is_brew=True)
    )
    assert r.severity == "ok"


# ── Issue #119: install.symlinks consumes the `stale` state ──────────


def test_symlinks_stale_only_is_warn_counts_available():
    s = _state(symlink_state=[("cctally", "stale"), ("cctally-tui", "ok")],
               symlinks_path_pinned=False)
    r = L._check_install_symlinks(s)
    assert r.severity == "warn"
    assert "2/2 available" in r.summary
    assert "stale" in r.summary
    assert r.details.get("stale") == ["cctally"]


def test_symlinks_pinned_gives_path_remediation():
    s = _state(symlink_state=[("cctally", "wrong")], symlinks_path_pinned=True)
    r = L._check_install_symlinks(s)
    assert "PATH" in r.remediation and "setup" in r.remediation
    assert r.remediation != "Run `cctally setup`"


def test_install_legacy_snippet_warn():
    s = _state(legacy_snippet=(pathlib.Path("/some/rc"), [42]))
    r = L._check_install_legacy_snippet(s)
    assert r.severity == "warn"
    assert "/some/rc:42" in r.summary


def test_install_legacy_bespoke_warn():
    s = _state(legacy_bespoke={"detected": True,
                               "settings_entries": [{}, {}, {}],
                               "files": ["/p1", "/p2"]})
    r = L._check_install_legacy_bespoke(s)
    assert r.severity == "warn"
    assert r.remediation == "Run `cctally setup --migrate-legacy-hooks`"


def _find_check(report, check_id):
    for cat in report.categories:
        for c in cat.checks:
            if c.id == check_id:
                return c
    raise AssertionError(f"check {check_id!r} not in report")


def test_install_mode_check_dev():
    s = _state(dev_mode=True, app_dir="/h/.local/share/cctally-dev")
    r = L._check_install_dev_mode(s)
    assert r.id == "install.mode"
    assert r.severity == "ok"
    assert "DEV" in r.summary
    assert r.details["dev_mode"] is True
    assert r.details["app_dir"] == "/h/.local/share/cctally-dev"


def test_install_mode_check_prod():
    s = _state(dev_mode=False, app_dir="/h/.local/share/cctally")
    r = L._check_install_dev_mode(s)
    assert r.id == "install.mode"
    assert r.severity == "ok"
    assert "installed" in r.summary
    assert r.details["dev_mode"] is False
    assert r.details["is_dev_checkout"] is False
    assert r.details["app_dir"] == "/h/.local/share/cctally"


def test_install_mode_check_override_on_checkout():
    """P3: CCTALLY_DATA_DIR override on a git checkout — DEV_MODE is False
    (the override won at step 1) but the binary IS a checkout. Must NOT report
    "installed"; surfaces the custom data dir distinctly from auto-detect."""
    s = _state(dev_mode=False, is_dev_checkout=True, app_dir="/tmp/branch-x")
    r = L._check_install_dev_mode(s)
    assert r.id == "install.mode"
    assert r.severity == "ok"
    assert "DEV" in r.summary
    assert "custom data dir" in r.summary
    assert "installed" not in r.summary
    assert r.details["dev_mode"] is False
    assert r.details["is_dev_checkout"] is True
    assert r.details["app_dir"] == "/tmp/branch-x"


def test_install_mode_registered_first_in_install_category():
    rep = L.run_checks(_state())
    check = _find_check(rep, "install.mode")
    assert check.severity == "ok"
    install_cat = next(c for c in rep.categories if c.id == "install")
    assert install_cat.checks[0].id == "install.mode"


def test_hooks_installed_all_present():
    r = L._check_hooks_installed(_state())
    assert r.severity == "ok"
    assert r.id == "hooks.installed"


def test_hooks_installed_missing_warns():
    s = _state(hook_counts={"PostToolBatch": 1, "Stop": 0, "SubagentStop": 0})
    r = L._check_hooks_installed(s)
    assert r.severity == "warn"
    assert "Stop" in r.summary and "SubagentStop" in r.summary
    assert r.remediation == "Run `cctally setup`"
    assert set(r.details["missing"]) == {"Stop", "SubagentStop"}


def test_hooks_recent_activity_ok():
    r = L._check_hooks_recent_activity_24h(_state())
    assert r.severity == "ok"


def test_hooks_recent_activity_zero_fires_warn():
    s = _state(log_activity_24h={"fires": 0, "errors": 0,
                                 "by_event": {"PostToolBatch": 0, "Stop": 0, "SubagentStop": 0},
                                 "last_fire_ago_s": None, "oauth_ok": 0, "throttled": 0})
    r = L._check_hooks_recent_activity_24h(s)
    assert r.severity == "warn"
    assert "0 fires" in r.summary


def test_hooks_recent_activity_high_error_ratio_warn():
    s = _state(log_activity_24h={"fires": 10, "errors": 6,
                                 "by_event": {"PostToolBatch": 10, "Stop": 0, "SubagentStop": 0},
                                 "last_fire_ago_s": 30, "oauth_ok": 4, "throttled": 0})
    r = L._check_hooks_recent_activity_24h(s)
    assert r.severity == "warn"
    assert "error" in r.summary.lower()


def test_hooks_last_fire_age_ok():
    r = L._check_hooks_last_fire_age(_state())
    assert r.severity == "ok"


def test_hooks_last_fire_age_stale_warn():
    s = _state(log_activity_24h={"fires": 1, "errors": 0,
                                 "by_event": {"PostToolBatch": 1, "Stop": 0, "SubagentStop": 0},
                                 "last_fire_ago_s": 7200, "oauth_ok": 1, "throttled": 0})
    r = L._check_hooks_last_fire_age(s)
    assert r.severity == "warn"


def test_hooks_last_fire_age_never_warn():
    s = _state(log_activity_24h={"fires": 0, "errors": 0,
                                 "by_event": {"PostToolBatch": 0, "Stop": 0, "SubagentStop": 0},
                                 "last_fire_ago_s": None, "oauth_ok": 0, "throttled": 0})
    r = L._check_hooks_last_fire_age(s)
    assert r.severity == "warn"
    assert r.summary == "never"


def test_oauth_token_present_ok():
    r = L._check_oauth_token_present(_state())
    assert r.severity == "ok"
    assert r.id == "oauth.token_present"


def test_oauth_token_missing_fails():
    r = L._check_oauth_token_present(_state(oauth_token_present=False))
    assert r.severity == "fail"
    assert r.remediation and "Claude" in r.remediation


def _db_status(status_per_migration=None, user_version=7, registry_size=7, path="/tmp/stats.db"):
    return {
        "path": path,
        "user_version": user_version,
        "registry_size": registry_size,
        "migrations": status_per_migration or [
            {"seq": i, "name": f"00{i}_x", "status": "applied"}
            for i in range(1, registry_size + 1)
        ],
    }


def test_db_stats_file_ok():
    r = L._check_db_stats_file(_state(stats_db_status=_db_status()))
    assert r.severity == "ok"
    assert r.id == "db.stats.file"


def test_db_stats_file_absent_warn():
    s = _state(stats_db_status={"path": "/missing/stats.db", "user_version": 0,
                                 "registry_size": 7, "migrations": [],
                                 "_file_exists": False})
    r = L._check_db_stats_file(s)
    assert r.severity == "warn"
    assert "absent" in r.summary.lower() or "missing" in r.summary.lower()


def test_db_stats_file_reports_interrupted_rebuild_before_fresh_install():
    s = _state(
        stats_db_status={
            "path": "/missing/stats.db",
            "user_version": 0,
            "registry_size": 13,
            "migrations": [],
            "_file_exists": False,
            "_interrupted_rebuild": {
                "live": False,
                "artifacts": ["stats.db.rebuilding-20260726T120000_000000"],
                "journalHighWater": ["observations-2026-07.jsonl", 123],
                "destinationExists": False,
            },
        }
    )
    r = L._check_db_stats_file(s)
    assert r.severity == "warn"
    assert "interrupted rebuild" in r.summary
    assert "cctally report" in (r.remediation or "")


def test_db_stats_file_reports_live_rebuild_before_fresh_install():
    s = _state(
        stats_db_status={
            "path": "/missing/stats.db",
            "user_version": 0,
            "registry_size": 13,
            "migrations": [],
            "_file_exists": False,
            "_interrupted_rebuild": {
                "live": True,
                "artifacts": ["stats.db.rebuilding-20260726T120000_000000"],
                "journalHighWater": ["observations-2026-07.jsonl", 123],
            },
        }
    )
    r = L._check_db_stats_file(s)
    assert r.severity == "warn"
    assert "in progress" in r.summary
    assert "Wait" in (r.remediation or "")


def test_db_stats_file_open_failure_fails():
    s = _state(stats_db_status={"path": "/x/stats.db", "user_version": 0,
                                 "registry_size": 7, "migrations": [],
                                 "_open_error": "database is locked"})
    r = L._check_db_stats_file(s)
    assert r.severity == "fail"
    assert r.details["exception"] == "database is locked"


def test_db_cache_file_ok():
    r = L._check_db_cache_file(_state(cache_db_status=_db_status(registry_size=3)))
    assert r.severity == "ok"


def test_db_migrations_applied_ok():
    s = _state(stats_db_status=_db_status(), cache_db_status=_db_status(registry_size=3))
    r = L._check_db_migrations_applied(s)
    assert r.severity == "ok"


def test_db_migrations_applied_skipped_warn():
    s = _state(stats_db_status=_db_status(status_per_migration=[
        {"seq": 1, "name": "001_x", "status": "skipped", "reason": "manual"},
        {"seq": 2, "name": "002_x", "status": "applied"},
    ], registry_size=2))
    r = L._check_db_migrations_applied(s)
    assert r.severity == "warn"


def test_db_migrations_applied_failed_fails():
    s = _state(stats_db_status=_db_status(status_per_migration=[
        {"seq": 1, "name": "001_x", "status": "failed",
         "last_failure_at": "2026-05-13T00:00:00Z", "log_path": "/x"},
    ], registry_size=1))
    r = L._check_db_migrations_applied(s)
    assert r.severity == "fail"


def test_db_migrations_applied_failed_details_shape():
    """Regression guard: the FAIL-branch details dict must carry exactly
    `failed` (list of (db, name) tuples) and `by_db` (per-db status map).
    A prior copy-paste bug mis-keyed the full `by_db` map under `skipped`."""
    s = _state(stats_db_status=_db_status(status_per_migration=[
        {"seq": 1, "name": "001_x", "status": "failed",
         "last_failure_at": "2026-05-13T00:00:00Z", "log_path": "/x"},
    ], registry_size=1))
    r = L._check_db_migrations_applied(s)
    assert r.severity == "fail"
    assert set(r.details.keys()) == {"failed", "by_db"}
    assert r.details["failed"] == [("stats.db", "001_x")]
    # `skipped` MUST NOT appear in the FAIL details payload.
    assert "skipped" not in r.details


def test_db_migrations_pending_ok():
    r = L._check_db_migrations_pending(_state())
    assert r.severity == "ok"


def test_db_migrations_pending_warn():
    s = _state(stats_db_status=_db_status(status_per_migration=[
        {"seq": 1, "name": "001_x", "status": "applied"},
        {"seq": 2, "name": "002_x", "status": "pending"},
    ], registry_size=2))
    r = L._check_db_migrations_pending(s)
    assert r.severity == "warn"


def _ts(seconds_ago: int) -> dt.datetime:
    return dt.datetime(2026, 5, 13, 14, 22, 31, tzinfo=dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)


def test_data_latest_snapshot_ok():
    r = L._check_data_latest_snapshot_age(_state(latest_snapshot_at=_ts(120)))
    assert r.severity == "ok"


def test_data_latest_snapshot_warn():
    r = L._check_data_latest_snapshot_age(_state(latest_snapshot_at=_ts(1800)))
    assert r.severity == "warn"


def test_data_latest_snapshot_fail_stale():
    r = L._check_data_latest_snapshot_age(_state(latest_snapshot_at=_ts(7200)))
    assert r.severity == "fail"


def test_data_latest_snapshot_fail_never():
    r = L._check_data_latest_snapshot_age(_state(latest_snapshot_at=None))
    assert r.severity == "fail"


def test_data_cache_sync_state_ok():
    s = _state(cache_entries_count=100, cache_last_entry_at=_ts(60), claude_jsonl_present=True)
    r = L._check_data_cache_sync_state(s)
    assert r.severity == "ok"


def test_data_cache_sync_state_stale_warn():
    s = _state(cache_entries_count=100,
               cache_last_entry_at=_ts(48 * 3600),
               claude_jsonl_present=True)
    r = L._check_data_cache_sync_state(s)
    assert r.severity == "warn"


def test_data_cache_sync_state_empty_with_jsonl_warn():
    s = _state(cache_entries_count=0, cache_last_entry_at=None, claude_jsonl_present=True)
    r = L._check_data_cache_sync_state(s)
    assert r.severity == "warn"


def test_data_cache_sync_state_empty_no_jsonl_ok():
    s = _state(cache_entries_count=0, cache_last_entry_at=None, claude_jsonl_present=False)
    r = L._check_data_cache_sync_state(s)
    assert r.severity == "ok"


def test_data_codex_cache_none_ok():
    s = _state(codex_entries_count=0, codex_last_entry_at=None, codex_jsonl_present=False)
    r = L._check_data_codex_cache(s)
    assert r.severity == "ok"
    assert "none" in r.summary.lower()


def test_data_codex_cache_present_ok():
    s = _state(codex_entries_count=10, codex_last_entry_at=_ts(60), codex_jsonl_present=True)
    r = L._check_data_codex_cache(s)
    assert r.severity == "ok"


def test_data_forked_buckets_all_zero_ok():
    r = L._check_data_forked_buckets(_state())
    assert r.severity == "ok"
    assert r.summary == "none"
    assert r.id == "data.forked_buckets"


def test_data_forked_buckets_nonzero_fail():
    """One forked usage row + one forked cost row → fail with both
    surfaced in the summary."""
    s = _state(forked_bucket_counts={"usage": 2, "cost": 1, "milestones": 0})
    r = L._check_data_forked_buckets(s)
    assert r.severity == "fail"
    assert "2 usage" in r.summary
    assert "1 cost" in r.summary
    # milestones=0 is omitted from the summary, kept in details.
    assert "milestones" not in r.summary
    assert r.details == {"usage": 2, "cost": 1, "milestones": 0}
    assert "004_heal_forked_week_start_date_buckets" in (r.remediation or "")


def test_data_forked_buckets_none_state_fail():
    """When stats.db couldn't be opened to gather, surface as fail
    rather than silently passing."""
    s = _state(forked_bucket_counts=None)
    r = L._check_data_forked_buckets(s)
    assert r.severity == "fail"
    assert "state unavailable" in r.summary


def test_data_post_credit_milestones_no_credits_ok():
    """No credited weeks → OK silent."""
    r = L._check_data_post_credit_milestones(_state(credited_weeks=[]))
    assert r.severity == "ok"


def test_data_post_credit_milestones_credit_with_no_crossings_warns():
    """A credited week with weekly_percent >= 1.0 and zero post-credit
    milestone rows → WARN with the week_start_date in the summary."""
    s = _state(credited_weeks=[{
        "week_start_date": "2026-05-09",
        "latest_weekly_percent": 5.0,
        "post_credit_milestone_count": 0,
        "event_id": 1,
    }])
    r = L._check_data_post_credit_milestones(s)
    assert r.severity == "warn"
    assert "2026-05-09" in r.summary


def test_data_post_credit_milestones_credit_with_zero_percent_ok():
    """A credited week with weekly_percent < 1.0 doesn't warn — the
    user just got credited and hasn't started using the new segment;
    the absence of post-credit milestones is the EXPECTED state.
    """
    s = _state(credited_weeks=[{
        "week_start_date": "2026-05-09",
        "latest_weekly_percent": 0.5,
        "post_credit_milestone_count": 0,
        "event_id": 1,
    }])
    r = L._check_data_post_credit_milestones(s)
    assert r.severity == "ok"


def test_data_post_credit_milestones_credit_with_crossings_ok():
    """A credited week with at least one post-credit milestone → OK."""
    s = _state(credited_weeks=[{
        "week_start_date": "2026-05-09",
        "latest_weekly_percent": 3.0,
        "post_credit_milestone_count": 3,
        "event_id": 1,
    }])
    r = L._check_data_post_credit_milestones(s)
    assert r.severity == "ok"


def test_data_post_credit_milestones_none_state_ok():
    """When stats.db couldn't be opened to gather credited_weeks,
    degrade to OK (the db.stats.file check covers DB-open issues)."""
    s = _state(credited_weeks=None)
    r = L._check_data_post_credit_milestones(s)
    assert r.severity == "ok"


# --- U9: conversation_sessions rollup consistency (#217 S1) -----------------

def test_rollup_check_ok_when_consistent():
    """Equal rollup count and distinct-session count → OK."""
    s = _state(conv_sessions_rollup_count=42,
               conv_messages_distinct_sessions=42,
               conv_rollup_sync_in_progress=False)
    r = L._check_data_conversation_sessions_rollup(s)
    assert r.id == "data.conversation_sessions_rollup"
    assert r.severity == "ok"


def test_rollup_check_warns_on_mismatch_quiescent():
    """A mismatch observed while NO sync/reingest/backfill is in progress is a
    real rollup drift → WARN (informational; the next full sync re-derives)."""
    s = _state(conv_sessions_rollup_count=40,
               conv_messages_distinct_sessions=42,
               conv_rollup_sync_in_progress=False)
    r = L._check_data_conversation_sessions_rollup(s)
    assert r.severity == "warn"
    assert "40" in r.summary and "42" in r.summary


def test_rollup_check_ok_when_sync_in_progress():
    """The SAME mismatch is OK while a sync/reingest/backfill is mid-flight
    (Codex P2 — conversation_messages commits per file before the
    conversation_sessions recompute, so a mid-sync read transiently mismatches)."""
    s = _state(conv_sessions_rollup_count=40,
               conv_messages_distinct_sessions=42,
               conv_rollup_sync_in_progress=True)
    r = L._check_data_conversation_sessions_rollup(s)
    assert r.severity == "ok"


def test_rollup_check_ok_when_either_count_none():
    """Pre-rollup / unreadable cache.db (either count None) → OK, never a false
    WARN — consistent with the doctor kernel's graceful-degrade posture."""
    assert L._check_data_conversation_sessions_rollup(
        _state(conv_sessions_rollup_count=None,
               conv_messages_distinct_sessions=42)).severity == "ok"
    assert L._check_data_conversation_sessions_rollup(
        _state(conv_sessions_rollup_count=42,
               conv_messages_distinct_sessions=None)).severity == "ok"


def test_safety_dashboard_bind_loopback_ok():
    r = L._check_safety_dashboard_bind(_state())
    assert r.severity == "ok"


def test_safety_dashboard_bind_lan_warn():
    r = L._check_safety_dashboard_bind(_state(dashboard_bind_stored="lan"))
    assert r.severity == "warn"


def test_safety_dashboard_bind_runtime_override_warn():
    s = _state(dashboard_bind_stored="loopback", runtime_bind="0.0.0.0")
    r = L._check_safety_dashboard_bind(s)
    assert r.severity == "warn"
    assert r.details["runtime_bind"] == "0.0.0.0"


def test_safety_dashboard_bind_runtime_loopback_ok():
    s = _state(dashboard_bind_stored="loopback", runtime_bind="127.0.0.1")
    r = L._check_safety_dashboard_bind(s)
    assert r.severity == "ok"


# ── Conversation viewer (Plan 2, spec §5): LAN + expose_transcripts ──


def test_safety_dashboard_bind_lan_with_expose_surfaces_detail():
    """A LAN bind WITH expose_transcripts serves raw transcript prose to the
    LAN — surfaced additively on the existing LAN-bind WARN."""
    s = _state(dashboard_bind_stored="lan", expose_transcripts=True)
    r = L._check_safety_dashboard_bind(s)
    assert r.severity == "warn"
    assert r.details.get("transcripts_exposed_on_lan") is True
    assert "transcripts exposed on LAN" in r.summary
    # The pre-existing LAN-bind details are untouched.
    assert r.details["config"] == "lan"


def test_safety_dashboard_bind_lan_without_expose_no_detail():
    """LAN bind, expose off (the default): the LAN-bind WARN is unchanged —
    NO transcript detail, NO summary mention. This is the case the
    `06-dashboard-bind-lan` golden pins, so the goldens stay byte-identical."""
    s = _state(dashboard_bind_stored="lan")  # expose_transcripts defaults False
    r = L._check_safety_dashboard_bind(s)
    assert r.severity == "warn"
    assert "transcripts_exposed_on_lan" not in r.details
    assert "transcripts exposed on LAN" not in r.summary


def test_safety_dashboard_bind_loopback_with_expose_is_byte_identical():
    """A loopback bind never reaches the LAN branch — even with expose on, the
    OK report is byte-identical to expose off (no detail, no summary churn).
    This is why the loopback doctor goldens need no regen."""
    off = L._check_safety_dashboard_bind(_state(expose_transcripts=False))
    on = L._check_safety_dashboard_bind(_state(expose_transcripts=True))
    assert off == on
    assert on.severity == "ok"
    assert "transcripts_exposed_on_lan" not in on.details


def test_safety_dashboard_bind_runtime_lan_with_expose_surfaces_detail():
    """A loopback config but a runtime --host LAN override, with expose on,
    also surfaces the transcript detail (the bind that's actually serving is
    non-loopback)."""
    s = _state(dashboard_bind_stored="loopback", runtime_bind="0.0.0.0",
               expose_transcripts=True)
    r = L._check_safety_dashboard_bind(s)
    assert r.severity == "warn"
    assert r.details.get("transcripts_exposed_on_lan") is True


def test_safety_config_json_ok_when_absent_or_parsed():
    assert L._check_safety_config_json_valid(_state()).severity == "ok"


def test_safety_config_json_fail_on_decode_error():
    s = _state(config_json_error="Expecting value: line 1 column 1 (char 0)")
    r = L._check_safety_config_json_valid(s)
    assert r.severity == "fail"


def test_safety_update_state_ok():
    r = L._check_safety_update_state(_state())
    assert r.severity == "ok"


def test_safety_update_state_warn_when_absent():
    r = L._check_safety_update_state(_state(update_state=None))
    assert r.severity == "warn"


def test_safety_update_state_fail_on_error():
    r = L._check_safety_update_state(_state(update_state=None,
                                            update_state_error="malformed JSON"))
    assert r.severity == "fail"


def test_safety_update_state_warn_on_missing_fields():
    """Spec §3.6 — WARN when known fields are missing. An empty dict
    (or one missing current_version / latest_version) is a file that
    parses but is semantically unusable."""
    r = L._check_safety_update_state(_state(update_state={}))
    assert r.severity == "warn"
    assert set(r.details["missing_keys"]) == {"current_version", "latest_version"}
    r2 = L._check_safety_update_state(_state(
        update_state={"current_version": "1.6.3"}))
    assert r2.severity == "warn"
    assert r2.details["missing_keys"] == ["latest_version"]


def test_safety_update_suppress_ok_when_absent():
    r = L._check_safety_update_suppress(_state(update_suppress=None))
    assert r.severity == "ok"


def test_safety_update_suppress_fail_on_error():
    r = L._check_safety_update_suppress(_state(update_suppress=None,
                                               update_suppress_error="malformed JSON"))
    assert r.severity == "fail"


def test_safety_update_suppress_warn_on_missing_fields():
    """Spec §3.6 — WARN when known fields are missing. Default record per
    bin/cctally:9731 is {skipped_versions: [], remind_after: None}; a dict
    missing either key is hand-edit corruption."""
    r = L._check_safety_update_suppress(_state(update_suppress={}))
    assert r.severity == "warn"
    assert set(r.details["missing_keys"]) == {"skipped_versions", "remind_after"}


def test_safety_update_suppress_warn_on_bad_types():
    """Spec §3.6 — WARN on unexpected types. skipped_versions must be a
    list; remind_after may be None / str / numeric / dict."""
    r = L._check_safety_update_suppress(_state(
        update_suppress={"skipped_versions": "not-a-list", "remind_after": None}))
    assert r.severity == "warn"
    assert r.details["bad_types"] == ["skipped_versions"]
    # A bool / list value for `remind_after` is still WARN — it doesn't
    # match any producer shape past or present.
    r2 = L._check_safety_update_suppress(_state(
        update_suppress={"skipped_versions": [], "remind_after": [1, 2, 3]}))
    assert r2.severity == "warn"
    assert r2.details["bad_types"] == ["remind_after"]


def test_safety_update_suppress_ok_when_remind_after_null():
    """Default record has remind_after=None; that must be OK, not WARN."""
    r = L._check_safety_update_suppress(_state(
        update_suppress={"_schema": 1, "skipped_versions": [], "remind_after": None}))
    assert r.severity == "ok"


def test_safety_update_suppress_ok_when_remind_after_is_producer_dict():
    """`cctally update --remind-later` writes `remind_after` as a dict
    `{"version", "until_utc"}`. Doctor must accept that shape, not flag
    the user's legitimate deferral as a corrupt file."""
    r = L._check_safety_update_suppress(_state(
        update_suppress={
            "_schema": 1,
            "skipped_versions": [],
            "remind_after": {"version": "1.6.4", "until_utc": "2026-05-20T12:00:00+00:00"},
        }))
    assert r.severity == "ok"


def test_safety_update_available_ok_when_uptodate():
    r = L._check_safety_update_available(_state())
    assert r.severity == "ok"
    assert "suppressed" not in r.details


def test_safety_update_available_warn_when_newer():
    s = _state(
        update_state={"current_version": "1.6.0", "latest_version": "1.6.3"},
        effective_update_available=True,
        effective_update_reason=None,
    )
    r = L._check_safety_update_available(s)
    assert r.severity == "warn"
    assert "1.6.3" in r.summary


def test_safety_update_available_ok_when_skipped():
    """Newer version exists but the user skipped it via `cctally update --skip`.
    The banner stays silent; doctor must match — no `Run cctally update`
    remediation. Suppression reason is surfaced in details for verbose readers.
    """
    s = _state(
        update_state={"current_version": "1.6.0", "latest_version": "1.6.3"},
        update_suppress={
            "_schema": 1,
            "skipped_versions": ["1.6.3"],
            "remind_after": None,
        },
        effective_update_available=False,
        effective_update_reason="skipped",
    )
    r = L._check_safety_update_available(s)
    assert r.severity == "ok"
    assert r.summary == "no"
    assert r.remediation is None
    assert r.details["suppressed"] is True
    assert r.details["suppression_reason"] == "skipped"


def test_safety_update_available_ok_when_reminded():
    """Newer version exists but the user deferred via `cctally update --remind`.
    Same contract as skipped: banner silent → doctor silent."""
    s = _state(
        update_state={"current_version": "1.6.0", "latest_version": "1.6.3"},
        effective_update_available=False,
        effective_update_reason="reminded",
    )
    r = L._check_safety_update_available(s)
    assert r.severity == "ok"
    assert r.details["suppressed"] is True
    assert r.details["suppression_reason"] == "reminded"


def test_safety_update_available_details_omit_suppressed_when_irrelevant():
    """No probe yet (missing_state / no_newer): details preserve the
    pre-existing shape (no `suppressed` key) so 13 existing fixture
    goldens stay byte-stable."""
    s = _state(update_state=None, effective_update_available=False,
               effective_update_reason="missing_state")
    r = L._check_safety_update_available(s)
    assert "suppressed" not in r.details
    assert "suppression_reason" not in r.details


def test_run_checks_returns_all_categories():
    rep = L.run_checks(_state())
    assert {c.id for c in rep.categories} == {
        "install", "hooks", "auth", "db", "journal", "data", "accounts",
        "pricing", "quota", "safety", "telemetry",
    }


def test_accounts_codex_reset_anchors_warns_on_null_rows():
    state = _state(accounts_state={
        "claude_identity_status": "stably_absent",
        "claude_email": None,
        "real_account_count": 0,
        "by_provider": {},
        "missing_provider": 0,
        "freshest_last_seen_age_s": None,
        "recent_attributed": 0,
        "recent_unattributed": 0,
        "codex_null_reset_anchors": 2,
    })
    checks = {
        check.id: check
        for category in L.run_checks(state).categories
        for check in category.checks
    }

    result = checks["accounts.codex_reset_anchors"]
    assert result.severity == "warn"
    assert result.details == {"null_anchor_rows": 2}
    assert result.remediation == "Run `cctally cache-sync --source codex --rebuild`"


def test_run_checks_all_ok_overall_ok():
    rep = L.run_checks(_state())
    assert rep.overall_severity == "ok"
    assert rep.counts["fail"] == 0


def test_run_checks_one_fail_makes_overall_fail():
    rep = L.run_checks(_state(oauth_token_present=False))
    assert rep.overall_severity == "fail"
    assert rep.counts["fail"] >= 1


def test_run_checks_includes_meta():
    rep = L.run_checks(_state())
    assert rep.schema_version == L.SCHEMA_VERSION
    assert rep.cctally_version == "1.6.3"
    assert rep.generated_at.tzinfo is not None


def test_run_checks_isolates_exceptions():
    """When a check evaluator raises, that check is FAIL with details.exception;
    sibling checks still run. The synthesized FAIL CheckResult MUST carry the
    canonical dotted id (spec §5.2) — NOT the evaluator function name — so the
    stable JSON contract and the fingerprint identity slice (spec §5.5) don't
    flip across success-vs-raise transitions."""
    import _lib_doctor as Lmod
    original = Lmod._check_install_path
    try:
        def boom(_s):
            raise RuntimeError("synthetic boom")
        Lmod._check_install_path = boom
        rep = Lmod.run_checks(_state())
    finally:
        Lmod._check_install_path = original
    install = next(c for c in rep.categories if c.id == "install")
    # The synthesized FAIL CheckResult MUST use the dotted contract id.
    path_check = next(c for c in install.checks if c.id == "install.path")
    assert path_check.severity == "fail"
    assert "synthetic boom" in path_check.details["exception"]
    # No check should ever surface with the function-name id.
    fn_named = [c for c in install.checks if c.id.startswith("_check_")]
    assert fn_named == [], f"function-name ids leaked into report: {fn_named}"
    # Other categories still computed:
    auth = next(c for c in rep.categories if c.id == "auth")
    assert auth.checks[0].severity == "ok"


def test_run_checks_dotted_ids_for_all_checks():
    """Spec §5.2 + §5.5 — every CheckResult.id must be the canonical dotted
    form. Regression guard for the prior bug where exception-synthesized
    CheckResults carried `_check_*` function names."""
    rep = L.run_checks(_state())
    ids = [c.id for cat in rep.categories for c in cat.checks]
    for cid in ids:
        assert "." in cid, f"non-dotted id: {cid!r}"
        assert not cid.startswith("_"), f"function-name id leaked: {cid!r}"


import json as _json


def test_serialize_json_top_level_shape():
    rep = L.run_checks(_state())
    payload = L.serialize_json(rep)
    assert payload["schema_version"] == 1
    assert payload["overall"]["severity"] == "ok"
    assert set(payload["overall"]["counts"].keys()) == {"ok", "warn", "fail"}
    assert payload["generated_at"].endswith("Z")
    assert payload["cctally_version"] == "1.6.3"
    cat_ids = [c["id"] for c in payload["categories"]]
    assert cat_ids == [
        "install", "hooks", "auth", "db", "journal", "data", "accounts",
        "pricing", "quota", "safety", "telemetry",
    ]


def test_serialize_json_remediation_only_when_not_ok():
    rep = L.run_checks(_state(oauth_token_present=False))
    payload = L.serialize_json(rep)
    for cat in payload["categories"]:
        for chk in cat["checks"]:
            if chk["severity"] == "ok":
                assert "remediation" not in chk
            else:
                assert "remediation" in chk and chk["remediation"]


def test_serialize_json_roundtrips_through_json_module():
    rep = L.run_checks(_state())
    payload = L.serialize_json(rep)
    s = _json.dumps(payload, sort_keys=True, indent=2)
    parsed = _json.loads(s)
    assert parsed == payload


def test_fingerprint_stable_across_generated_at_drift():
    rep1 = L.run_checks(_state(now_utc=dt.datetime(2026, 5, 13, 14, 22, 31, tzinfo=dt.timezone.utc)))
    rep2 = L.run_checks(_state(now_utc=dt.datetime(2026, 5, 13, 14, 22, 47, tzinfo=dt.timezone.utc)))
    assert L.fingerprint(rep1) == L.fingerprint(rep2)


def test_fingerprint_stable_across_summary_text_drift():
    """If a future evaluator change tweaks summary text but leaves severity
    + check ID unchanged, fingerprint should not invalidate."""
    rep = L.run_checks(_state())
    fp1 = L.fingerprint(rep)
    # Mutate a check's summary in place by reconstructing the report tree
    cats = []
    for cat in rep.categories:
        new_checks = []
        for c in cat.checks:
            new_checks.append(L.CheckResult(
                id=c.id, title=c.title, severity=c.severity,
                summary=c.summary + " (extra text)",
                remediation=c.remediation, details=c.details,
            ))
        cats.append(L.CategoryResult(
            id=cat.id, title=cat.title, severity=cat.severity, checks=tuple(new_checks),
        ))
    mutated = L.DoctorReport(
        schema_version=rep.schema_version, generated_at=rep.generated_at,
        cctally_version=rep.cctally_version,
        overall_severity=rep.overall_severity, counts=rep.counts,
        categories=tuple(cats),
    )
    assert L.fingerprint(mutated) == fp1


def test_fingerprint_changes_on_severity_flip():
    rep_ok = L.run_checks(_state())
    rep_fail = L.run_checks(_state(oauth_token_present=False))
    assert L.fingerprint(rep_ok) != L.fingerprint(rep_fail)


def test_fingerprint_shape():
    fp = L.fingerprint(L.run_checks(_state()))
    assert fp.startswith("sha1:")
    assert len(fp) == len("sha1:") + 40


def test_render_text_default_has_summary_and_categories():
    rep = L.run_checks(_state())
    out = L.render_text(rep)
    assert "cctally doctor" in out
    assert "Install" in out and "Hooks" in out
    assert "Summary:" in out


def test_render_text_quiet_hides_ok_rows():
    rep = L.run_checks(_state(oauth_token_present=False))
    out = L.render_text(rep, quiet=True)
    # OAuth FAIL row must appear:
    assert "OAuth token" in out
    # An OK row (Symlinks) must NOT appear:
    assert "Symlinks" not in out
    # Summary line still present:
    assert "Summary:" in out


def test_render_text_verbose_includes_details_block():
    rep = L.run_checks(_state())
    out = L.render_text(rep, verbose=True)
    assert "details:" in out


def test_render_text_quiet_and_verbose_mutually_exclusive():
    import pytest
    with pytest.raises(ValueError):
        L.render_text(L.run_checks(_state()), quiet=True, verbose=True)


def test_render_text_glyphs_match_severity():
    rep = L.run_checks(_state(oauth_token_present=False))
    out = L.render_text(rep)
    # Severity glyphs per spec §4.4 example: ✓ ⚠ ✗
    assert "✗" in out  # the FAIL on OAuth


def test_render_text_summary_includes_counts():
    rep = L.run_checks(_state(oauth_token_present=False))
    out = L.render_text(rep)
    counts = rep.counts
    assert f"{counts['ok']} OK" in out
    assert f"{counts['fail']} FAIL" in out


# ── #279 S2 F5: parse-health / db.integrity / db.lock_state ──────────

def _iso(dt_obj):
    return dt_obj.replace(tzinfo=dt.timezone.utc).isoformat()


_NOW = dt.datetime(2026, 5, 13, 14, 22, 31, tzinfo=dt.timezone.utc)


def test_parse_health_absent_is_ok():
    r = L._check_data_parse_health(_state(parse_health_claude=None,
                                          parse_health_codex=None))
    assert r.severity == "ok"
    assert "pre-first-sync" in r.summary


def test_parse_health_recent_anomaly_warns_with_top_reason():
    ph = {"schema": 1, "lines_seen": 100, "lines_malformed": 3,
          "lines_skipped": 2, "reasons": {"no-usage": 2, "no-model": 1},
          "last_anomaly_at": _iso(_NOW - dt.timedelta(days=1))}
    r = L._check_data_parse_health(_state(now_utc=_NOW, parse_health_claude=ph))
    assert r.severity == "warn"
    assert "3 malformed" in r.summary
    assert "no-usage" in r.summary  # dominant reason
    assert "cache-sync --rebuild" in r.remediation


def test_parse_health_boundary_exactly_7d_is_warn():
    ph = {"schema": 1, "lines_seen": 10, "lines_malformed": 1,
          "lines_skipped": 0, "reasons": {"bad-timestamp": 1},
          "last_anomaly_at": _iso(_NOW - dt.timedelta(days=7))}
    r = L._check_data_parse_health(_state(now_utc=_NOW, parse_health_codex=ph))
    assert r.severity == "warn"  # boundary inclusive (<= 7d)


def test_parse_health_stale_anomaly_is_ok_historical():
    ph = {"schema": 1, "lines_seen": 10, "lines_malformed": 5,
          "lines_skipped": 0, "reasons": {"bad-timestamp": 5},
          "last_anomaly_at": _iso(_NOW - dt.timedelta(days=30))}
    r = L._check_data_parse_health(_state(now_utc=_NOW, parse_health_claude=ph))
    assert r.severity == "ok"
    assert "historical" in r.summary


def test_parse_health_zero_counts_is_ok():
    ph = {"schema": 1, "lines_seen": 500, "lines_malformed": 0,
          "lines_skipped": 0, "reasons": {},
          "last_anomaly_at": None}
    r = L._check_data_parse_health(_state(parse_health_claude=ph))
    assert r.severity == "ok"
    assert "no parse anomalies" in r.summary


def test_db_integrity_not_checked_is_ok():
    r = L._check_db_integrity(_state(stats_db_quick_check=None,
                                     cache_db_quick_check=None,
                                     conversations_db_quick_check=None))
    assert r.severity == "ok"
    assert "not checked" in r.summary


def test_db_integrity_stats_corrupt_is_fail_no_delete():
    r = L._check_db_integrity(_state(
        stats_db_quick_check="malformed database schema",
        cache_db_quick_check="database disk image is malformed",
        conversations_db_quick_check="file is not a database"))
    assert r.severity == "fail"
    remediation = r.remediation.lower()
    # #496 S6 F21: BOTH cases, stated positively. This used to pin the word
    # "backup", which is vocabulary rather than behaviour — it turned the suite
    # red on a rewording that changed nothing, while never checking the leg
    # F21 actually requires, the journal-backed branch.
    assert "cctally db rebuild --db stats" in remediation, (
        "the journal-backed branch is not stated: with retained journal data "
        "stats.db is a disposable index and rebuild is the answer"
    )
    assert "journal" in remediation
    assert "only copy" in remediation, (
        "the pre-cutover branch is not stated"
    )
    assert "cctally db repair --db stats --yes" in r.remediation
    assert "do not" in remediation and "delete" in remediation


def test_db_integrity_cache_corrupt_stats_ok_is_warn():
    r = L._check_db_integrity(_state(
        stats_db_quick_check="ok",
        cache_db_quick_check="database disk image is malformed",
        conversations_db_quick_check="ok"))
    assert r.severity == "warn"
    assert "cache-sync --rebuild" in r.remediation


def test_db_integrity_conversations_corrupt_is_store_specific_warn():
    r = L._check_db_integrity(_state(
        stats_db_quick_check="ok",
        cache_db_quick_check="ok",
        conversations_db_quick_check="database disk image is malformed"))
    assert r.severity == "warn"
    assert "conversations.db" in r.summary
    assert "cctally cache-sync --rebuild" in r.remediation
    assert r.details["conversations_quick_check"] == (
        "database disk image is malformed"
    )


def test_db_integrity_both_ok():
    r = L._check_db_integrity(_state(stats_db_quick_check="ok",
                                     cache_db_quick_check="ok",
                                     conversations_db_quick_check="ok"))
    assert r.severity == "ok"
    assert "ok" in r.summary


def test_db_lock_state_held_is_ok_and_named():
    r = L._check_db_lock_state(_state(locks_held={"cache.db.lock": True,
                                                  "cache.db.codex.lock": False}))
    assert r.severity == "ok"  # NEVER warn
    assert "cache.db.lock" in r.summary


def test_db_lock_state_free_is_ok():
    r = L._check_db_lock_state(_state(locks_held={"cache.db.lock": False}))
    assert r.severity == "ok"
    assert "free" in r.summary


def test_db_lock_state_none_is_ok():
    r = L._check_db_lock_state(_state(locks_held=None))
    assert r.severity == "ok"


def test_doctorstate_all_new_fields_default():
    # A DoctorState built with only the pre-existing required fields still
    # constructs — every new S2 field is defaulted (Codex P1-3).
    s = _state()
    assert s.parse_health_claude is None
    assert s.parse_health_codex is None
    assert s.stats_db_quick_check is None
    assert s.cache_db_quick_check is None
    assert s.locks_held is None


def test_new_checks_registered_and_run():
    rep = L.run_checks(_state())
    ids = {c.id for cat in rep.categories for c in cat.checks}
    assert {"data.parse_health", "db.integrity", "db.lock_state"} <= ids


def test_rollup_writer_check_is_ok_with_no_refusal():
    s = _state()
    s = dc.replace(s, conversation_rollup_pricing_refusal=None)
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.id == "pricing.conversation_rollup_writer"
    assert r.severity == "ok"


def test_rollup_writer_check_warns_and_names_both_versions():
    s = dc.replace(_state(), conversation_rollup_pricing_refusal={
        "process_snapshot_date": "2026-08-25",
        "store_snapshot_date": "2026-09-02",
        "first_refused_at_utc": "2026-09-03T08:00:00Z",
    })
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.severity == "warn"
    assert r.details["process_snapshot_date"] == "2026-08-25"
    assert r.details["store_snapshot_date"] == "2026-09-02"


def test_a_malformed_refusal_record_warns_rather_than_degrading_to_ok():
    # Nearby doctor JSON parsing discards malformed values silently. A record
    # that exists but cannot be read is still evidence the guard fired, so it
    # must not read as "no refusal".
    # Mutation: returning OK on the unparseable branch.
    s = dc.replace(_state(),
                   conversation_rollup_pricing_refusal={"__malformed__": True})
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.severity == "warn"
    assert "process_snapshot_date" not in (r.details or {})


def test_the_rollup_writer_check_can_never_fail():
    # Mutation: severity="fail" on any branch, which would move doctor's exit
    # code from 0 to 2 for a condition that means the safety net WORKED.
    for record in (None,
                   {"process_snapshot_date": "a", "store_snapshot_date": "b",
                    "first_refused_at_utc": "c"},
                   {"__malformed__": True}):
        s = dc.replace(_state(), conversation_rollup_pricing_refusal=record)
        assert L._check_pricing_conversation_rollup_writer(s).severity != "fail"


def test_the_rollup_writer_check_is_registered_under_pricing():
    report = L.run_checks(_state())
    pricing = next(c for c in report.categories if c.id == "pricing")
    assert "pricing.conversation_rollup_writer" in {
        check.id for check in pricing.checks}


# --- #705 FIX-E: an unparseable stored fingerprint has no ordinary exit ------
# _pricing_write_authorized fails closed on a value that is not an ISO date, on
# EITHER side, and that decision is deliberate. The consequence is that no
# version can write the rollup, the refusal arms the backfill flag, and the
# documented remedy ("run the same or a newer cctally") does not apply — a
# `cache-sync --rebuild` is refused too. The state is reachable only through
# corruption or a future fingerprint format, but it is unbounded, so doctor
# must name it and name the step that actually gets out of it.


def _unparseable_fp_state(**extra):
    return dc.replace(_state(),
                      conversation_rollup_pricing_fp="v2/2026-09-02", **extra)


def test_an_unparseable_stored_fingerprint_gets_its_own_summary_and_remedy():
    # Mutation: reporting the ordinary refusal wording, whose remedy (restart or
    # upgrade the writing process) does nothing at all in this state.
    s = _unparseable_fp_state(conversation_rollup_pricing_refusal={
        "process_snapshot_date": "2026-09-02",
        "store_snapshot_date": "v2/2026-09-02",
        "first_refused_at_utc": "2026-09-03T08:00:00Z",
    })
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.severity == "warn"
    assert r.remediation == L.ROLLUP_WRITER_UNPARSEABLE_FP_REMEDIATION
    assert r.remediation != L.ROLLUP_WRITER_REFUSAL_REMEDIATION
    assert r.details["stored_fingerprint"] == "v2/2026-09-02"


def test_an_unparseable_stored_fingerprint_warns_before_any_refusal_is_recorded():
    # The store is already unwritable by every version at this point, so waiting
    # for a refusal record to appear would delay the only actionable diagnosis.
    # Mutation: gating the unparseable branch behind `if record`.
    r = L._check_pricing_conversation_rollup_writer(
        _unparseable_fp_state(conversation_rollup_pricing_refusal=None))
    assert r.severity == "warn"
    assert r.remediation == L.ROLLUP_WRITER_UNPARSEABLE_FP_REMEDIATION


def test_a_parseable_stored_fingerprint_keeps_the_ordinary_refusal_remedy():
    # The ordinary case must not be dragged into the new branch: an old process
    # against a newer store IS fixed by restarting or upgrading it.
    # Mutation: treating any recorded fingerprint as unparseable.
    s = dc.replace(_state(),
                   conversation_rollup_pricing_fp="2026-09-30",
                   conversation_rollup_pricing_refusal={
                       "process_snapshot_date": "2026-08-25",
                       "store_snapshot_date": "2026-09-30",
                       "first_refused_at_utc": "2026-09-03T08:00:00Z"})
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.remediation == L.ROLLUP_WRITER_REFUSAL_REMEDIATION
    assert r.details["store_snapshot_date"] == "2026-09-30"


def test_an_absent_or_empty_fingerprint_is_not_unparseable():
    # Absent and empty both mean "a fresh store with nothing to protect", which
    # _pricing_write_authorized already treats as older. Reporting them as
    # corruption would WARN on every install that has never synced.
    # Mutation: `if not _fp_is_comparable(fp)` without the emptiness guard.
    for value in (None, ""):
        r = L._check_pricing_conversation_rollup_writer(
            dc.replace(_state(), conversation_rollup_pricing_fp=value,
                       conversation_rollup_pricing_refusal=None))
        assert r.severity == "ok", value


def test_the_unparseable_fingerprint_branch_still_cannot_fail():
    # The check's exit-code contract is unchanged: this is still a degraded but
    # usable store, so it must never move doctor's exit code from 0 to 2.
    # Mutation: severity="fail" on the new branch.
    for record in (None, {"__malformed__": True},
                   {"process_snapshot_date": "a", "store_snapshot_date": "b",
                    "first_refused_at_utc": "c"}):
        r = L._check_pricing_conversation_rollup_writer(
            _unparseable_fp_state(conversation_rollup_pricing_refusal=record))
        assert r.severity == "warn"


def test_the_rollup_writer_remediations_match_the_documented_ones():
    """`docs/commands/doctor.md` is where an operator reads the recovery step,
    and `_lib_doctor` is where the running command prints it. Nothing else
    compares the two, so a reworded remediation could leave the documented
    recovery describing a step the command no longer names — and for the
    unparseable-fingerprint state the documented step is the ONLY way out.

    Mutation: rewording either remediation without updating the other.
    """
    doc = (pathlib.Path(__file__).resolve().parent.parent
           / "docs" / "commands" / "doctor.md").read_text()
    assert L.ROLLUP_WRITER_REFUSAL_REMEDIATION in doc
    assert L.ROLLUP_WRITER_UNPARSEABLE_FP_REMEDIATION in doc
    # #769 S3: the check grew two further states after Tranche 1, each with its
    # own remedy, and the document described only the two above. Both are
    # store-parameterised, so the document quotes the `conversations.db` form
    # and says the `--db` value follows the check's `store` detail.
    assert L.rollup_writer_degraded_read_remediation("conversations.db") in doc
    assert L.rollup_writer_refused_degraded_read_remediation(
        "conversations.db") in doc


# --- #705 FIX-2: the guard protects two stores, so the check reads two -------
# `sync_cache` runs `_arm_rollup_backfill_on_pricing_change` and the recompute
# on the cache.db connection; `sync_claude_conversations` runs them on the
# conversations.db connection. Both files therefore carry their own
# `conversation_sessions_pricing_fp` and their own refusal record. Reading only
# conversations.db reported OK for every refusal latched by a process that runs
# `sync_cache` and never opens the conversation store — `hook-tick`,
# `statusline`, `daily`, `report`.


def _refusal(process="2026-08-25", store="2026-09-30"):
    return {"process_snapshot_date": process, "store_snapshot_date": store,
            "first_refused_at_utc": "2026-09-03T08:00:00Z"}


def test_a_refusal_latched_only_in_cache_db_is_still_reported():
    # Mutation: dropping the cache.db leg, which is how every hook-tick-only
    # refusal read as "no refused pricing write recorded".
    s = dc.replace(_state(),
                   conversation_rollup_pricing_refusal=None,
                   conversation_rollup_pricing_fp=None,
                   cache_rollup_pricing_refusal=_refusal(),
                   cache_rollup_pricing_fp="2026-09-30")
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.severity == "warn"
    assert r.details["store"] == "cache.db"
    assert r.details["process_snapshot_date"] == "2026-08-25"


def test_the_reported_refusal_names_the_store_it_came_from():
    # An operator clearing an unwritable fingerprint has to know which file's
    # cache_meta row to delete, and the two stores diverge routinely.
    # Mutation: reporting a refusal without naming its store.
    s = dc.replace(_state(),
                   conversation_rollup_pricing_refusal=_refusal(),
                   conversation_rollup_pricing_fp="2026-09-30",
                   cache_rollup_pricing_refusal=None,
                   cache_rollup_pricing_fp=None)
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.details["store"] == "conversations.db"


def test_an_unparseable_fingerprint_in_cache_db_gets_the_same_remedy():
    # The state with no ordinary exit is reachable in either store.
    # Mutation: classifying only the conversations fingerprint.
    s = dc.replace(_state(),
                   conversation_rollup_pricing_fp="2026-09-02",
                   conversation_rollup_pricing_refusal=None,
                   cache_rollup_pricing_fp="v2/2026-09-02",
                   cache_rollup_pricing_refusal=None)
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.severity == "warn"
    assert r.remediation == L.ROLLUP_WRITER_UNPARSEABLE_FP_REMEDIATION
    assert r.details["store"] == "cache.db"
    assert r.details["stored_fingerprint"] == "v2/2026-09-02"


def test_both_stores_clean_is_still_ok_with_empty_details():
    # The OK branch is what every doctor golden records, so it must keep its
    # exact shape while the check grows a second store.
    # Mutation: emitting a store name on the OK branch.
    s = dc.replace(_state(),
                   conversation_rollup_pricing_refusal=None,
                   conversation_rollup_pricing_fp="2026-09-02",
                   cache_rollup_pricing_refusal=None,
                   cache_rollup_pricing_fp="2026-09-02")
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.severity == "ok"
    assert r.details == {}


def test_the_two_store_check_can_never_fail():
    # Mutation: severity="fail" on either store's branch.
    for extra in (
        dict(cache_rollup_pricing_refusal=_refusal()),
        dict(cache_rollup_pricing_fp="v2/2026-09-02"),
        dict(cache_rollup_pricing_refusal={"__malformed__": True}),
    ):
        r = L._check_pricing_conversation_rollup_writer(
            dc.replace(_state(), **extra))
        assert r.severity == "warn", extra


# --- #705 FIX-8: the unparseable branch must keep the dates it has -----------


def test_an_unparseable_fingerprint_keeps_the_refusal_dates():
    """The unparseable branch precedes the record branch, so returning only
    `stored_fingerprint` discarded `process_snapshot_date` and
    `first_refused_at_utc` for a store that carries both. Those two are what
    say WHICH process is refusing and how long it has been refusing, and a
    `--json` consumer has no other source for them.

    Mutation: returning `{"store": …, "stored_fingerprint": …}` alone.
    """
    s = dc.replace(_state(),
                   conversation_rollup_pricing_fp="v2/2026-09-02",
                   conversation_rollup_pricing_refusal=_refusal(
                       process="2026-09-02", store="v2/2026-09-02"))
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.details["stored_fingerprint"] == "v2/2026-09-02"
    assert r.details["process_snapshot_date"] == "2026-09-02"
    assert r.details["first_refused_at_utc"] == "2026-09-03T08:00:00Z"


def test_an_unparseable_fingerprint_with_an_unreadable_record_adds_no_dates():
    # A record that exists but cannot be parsed has no dates to merge, and
    # inventing keys with None values would read as "gathered and absent".
    # Mutation: merging the record dict unconditionally.
    s = dc.replace(_state(),
                   conversation_rollup_pricing_fp="v2/2026-09-02",
                   conversation_rollup_pricing_refusal={"__malformed__": True})
    r = L._check_pricing_conversation_rollup_writer(s)
    assert "process_snapshot_date" not in r.details


# --- #705 FIX-3: the check classifies exactly what the guard acts on ---------


def test_the_check_classifies_fingerprints_the_way_the_shared_predicate_does():
    """`_lib_doctor` had its own `date.fromisoformat` of the fingerprint
    contract, and it coerced with `str()` where the guard did not. Both now
    call `_lib_pricing.pricing_fingerprint_is_comparable`, and this pins the
    check to that one answer.

    Mutation: reintroducing a private parse in `_lib_doctor`.
    """
    import _lib_pricing  # noqa: PLC0415

    for value in (None, "", "2026-09-02", "not-a-date", 20260902):
        r = L._check_pricing_conversation_rollup_writer(
            dc.replace(_state(), conversation_rollup_pricing_fp=value,
                       conversation_rollup_pricing_refusal=None,
                       cache_rollup_pricing_fp=None,
                       cache_rollup_pricing_refusal=None))
        reported_unparseable = (
            r.remediation == L.ROLLUP_WRITER_UNPARSEABLE_FP_REMEDIATION)
        assert reported_unparseable is not \
            _lib_pricing.pricing_fingerprint_is_comparable(value), value


# ── #719: Codex hook state severity, multi-root worst-case, 7d liveness ──


def _codex_row(key, state, *, requires_review=False, remediation=None):
    return {
        "source_root_key": key, "state": state,
        "requires_review": requires_review, "remediation": remediation,
    }


@pytest.mark.parametrize("state,severity", [
    ("installed_enabled", "ok"),
    ("installed_unverified", "warn"),
    ("installed_disabled", "fail"),
    ("installed_untrusted", "fail"),
    ("installed_trust_unobservable", "warn"),
    ("absent", "warn"),
    ("malformed", "warn"),
    ("feature_disabled", "warn"),
])
def test_codex_hook_state_severity_table_is_total(state, severity):
    result = L._check_hooks_codex_installed(_state(
        codex_hook_roots=[_codex_row("root-a", state, remediation="fix it")],
    ))
    assert result.severity == severity
    assert L.CODEX_HOOK_STATE_SEVERITY[state] == severity


def test_a_healthy_sibling_never_masks_a_disabled_codex_root():
    result = L._check_hooks_codex_installed(_state(codex_hook_roots=[
        _codex_row("root-a", "installed_enabled"),
        _codex_row("root-b", "installed_disabled",
                   remediation="Re-enable the cctally handler in Codex /hooks."),
    ]))
    assert result.severity == "fail"
    assert result.details["worst_state"] == "installed_disabled"
    assert result.details["responsible_root_key"] == "root-b"
    assert result.details["enabled_root_count"] == 1
    assert result.remediation == "Re-enable the cctally handler in Codex /hooks."


def test_the_installed_summary_distinguishes_installed_from_enabled():
    """`0/2 root(s) enabled` alone reads as "nothing is installed".

    An `installed_untrusted` or `installed_unverified` handler IS installed
    and may still be firing, and the modal row is all a reader who does not
    expand the details block ever sees.
    """
    result = L._check_hooks_codex_installed(_state(codex_hook_roots=[
        _codex_row("root-a", "installed_untrusted", remediation="review"),
        _codex_row("root-b", "installed_unverified", remediation="review"),
    ]))
    assert result.summary == "0/2 root(s) enabled, 2 installed"
    assert result.details["installed_root_count"] == 2
    assert result.details["enabled_root_count"] == 0


def test_the_installed_summary_stays_byte_stable_when_nothing_extra_is_installed():
    """Every root that is installed is also enabled, so the ratio says it all."""
    for rows, expected in [
        ([_codex_row("root-a", "installed_enabled")], "1/1 root(s) enabled"),
        ([_codex_row("root-a", "absent"),
          _codex_row("root-b", "installed_enabled")], "1/2 root(s) enabled"),
        ([_codex_row("root-a", "absent")], "0/1 root(s) enabled"),
        ([], "not applicable"),
    ]:
        assert L._check_hooks_codex_installed(
            _state(codex_hook_roots=rows)).summary == expected


def test_each_untrusted_state_carries_its_own_remediation():
    for state, remediation in [
        ("installed_untrusted", "Review and trust the cctally handler in Codex /hooks."),
        ("installed_unverified", "review after the change"),
        ("installed_disabled", "Re-enable the cctally handler in Codex /hooks."),
    ]:
        result = L._check_hooks_codex_installed(_state(
            codex_hook_roots=[_codex_row("root-a", state, remediation=remediation)],
        ))
        assert result.remediation == remediation, state


def test_codex_liveness_fails_when_an_enabled_hook_is_silent_for_seven_days():
    now = dt.datetime(2026, 9, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
    result = L._check_hooks_codex_liveness_7d(_state(
        now_utc=now,
        codex_hook_roots=[_codex_row("root-a", "installed_enabled")],
        codex_hook_liveness={"root-a": {
            "last_success_at": now - dt.timedelta(days=7, seconds=1),
            "marker_count": 2, "unavailable": False,
        }},
    ))
    assert result.severity == "fail"
    assert result.details["liveness_state"] == "stale"
    assert result.details["window_seconds"] == 604800
    assert result.details["age_seconds"] == 604801
    assert result.details["marker_count"] == 2
    assert result.details["responsible_root_key"] == "root-a"


def test_codex_liveness_reduces_to_the_newest_account_marker_per_root():
    """A dormant historical account must not fail a root firing under a
    current one; the gather already reduced to the newest readable mtime."""
    now = dt.datetime(2026, 9, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
    result = L._check_hooks_codex_liveness_7d(_state(
        now_utc=now,
        codex_hook_roots=[_codex_row("root-a", "installed_enabled")],
        codex_hook_liveness={"root-a": {
            "last_success_at": now - dt.timedelta(hours=3),
            "marker_count": 3, "unavailable": False,
        }},
    ))
    assert result.severity == "ok"
    assert result.details["liveness_state"] == "recent"
    assert result.details["marker_count"] == 3


def test_codex_liveness_states_never_unavailable_and_not_applicable():
    now = dt.datetime(2026, 9, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
    never = L._check_hooks_codex_liveness_7d(_state(
        now_utc=now,
        codex_hook_roots=[_codex_row("root-a", "installed_enabled")],
        codex_hook_liveness={},
    ))
    assert never.severity == "fail"
    assert never.details["liveness_state"] == "never"

    unavailable = L._check_hooks_codex_liveness_7d(_state(
        now_utc=now,
        codex_hook_roots=[_codex_row("root-a", "installed_enabled")],
        codex_hook_liveness={"root-a": {
            "last_success_at": None, "marker_count": 0, "unavailable": True}},
    ))
    assert unavailable.severity == "warn"
    assert unavailable.details["liveness_state"] == "unavailable"

    # A disabled root is not evaluated for liveness; the installed check owns it.
    disabled = L._check_hooks_codex_liveness_7d(_state(
        now_utc=now,
        codex_hook_roots=[_codex_row("root-a", "installed_disabled")],
        codex_hook_liveness={},
    ))
    assert disabled.severity == "ok"
    assert disabled.details["liveness_state"] == "not-applicable"
    assert disabled.details["roots"] == []


def test_codex_liveness_and_activity_evaluate_only_enabled_roots():
    now = dt.datetime(2026, 9, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
    state = _state(
        now_utc=now,
        codex_hook_roots=[
            _codex_row("root-a", "installed_untrusted"),
            _codex_row("root-b", "installed_enabled"),
        ],
        codex_lifecycle_activity_24h={
            "root-a": {"last_tick_at": None, "success_count_24h": 0,
                       "error_count_24h": 0},
            "root-b": {"last_tick_at": now - dt.timedelta(seconds=30),
                       "success_count_24h": 1, "error_count_24h": 0},
        },
        codex_hook_liveness={"root-b": {
            "last_success_at": now - dt.timedelta(hours=1),
            "marker_count": 1, "unavailable": False}},
    )
    activity = L._check_hooks_codex_recent_activity(state)
    assert [row["source_root_key"] for row in activity.details["roots"]] == ["root-b"]
    liveness = L._check_hooks_codex_liveness_7d(state)
    assert [row["source_root_key"] for row in liveness.details["roots"]] == ["root-b"]


# --- #728: a read that failed is reported as DEGRADED, never as OK ----------
# The fingerprint value alone cannot distinguish "this store recorded nothing"
# from "doctor could not read this store", so a locked cache.db reported "no
# refused pricing write recorded" and doctor exited OK over a store it had no
# evidence about.


def _observation(state, *, raw=None, error_kind=None):
    import _lib_pricing
    return _lib_pricing.PricingFingerprintObservation(
        state=state, parsed_date=None, raw=raw, error_kind=error_kind)


def test_a_degraded_fingerprint_read_warns_rather_than_reporting_ok():
    for field in ("conversation_rollup_pricing_observation",
                  "cache_rollup_pricing_observation"):
        s = dc.replace(_state(), **{
            field: _observation("degraded", error_kind="operational_error")})
        r = L._check_pricing_conversation_rollup_writer(s)
        assert r.severity == "warn", field
        assert r.remediation == L.rollup_writer_degraded_read_remediation(
            r.details["store"]), field
        assert r.details["store"] == (
            "conversations.db" if field.startswith("conversation")
            else "cache.db")


def test_a_degraded_read_is_reported_distinctly_from_an_unparseable_value():
    # Two different diagnoses with two different remedies: an unparseable value
    # needs the row deleted, and a failed read needs whatever is holding the
    # store released. Reporting either under the other's wording sends the
    # operator to a step that cannot work.
    degraded = L._check_pricing_conversation_rollup_writer(
        dc.replace(_state(),
                   conversation_rollup_pricing_observation=_observation(
                       "degraded", error_kind="operational_error")))
    malformed = L._check_pricing_conversation_rollup_writer(
        _unparseable_fp_state(
            conversation_rollup_pricing_observation=_observation(
                "malformed", raw="v2/2026-09-02")))
    assert degraded.summary != malformed.summary
    assert degraded.remediation != malformed.remediation
    assert malformed.remediation == L.ROLLUP_WRITER_UNPARSEABLE_FP_REMEDIATION


def test_an_unparseable_value_outranks_a_degraded_read_in_the_other_store():
    # An unparseable value is a diagnosed permanent state with a known step out
    # of it; a failed read is undiagnosed and often transient. Report the one
    # the operator can act on.
    s = dc.replace(
        _state(),
        conversation_rollup_pricing_observation=_observation(
            "degraded", error_kind="operational_error"),
        cache_rollup_pricing_fp="v2/2026-09-02",
        cache_rollup_pricing_observation=_observation(
            "malformed", raw="v2/2026-09-02"),
    )
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.remediation == L.ROLLUP_WRITER_UNPARSEABLE_FP_REMEDIATION
    assert r.details["store"] == "cache.db"


def test_a_degraded_read_outranks_a_recorded_refusal_in_the_other_store():
    # A recorded refusal has a documented remedy; a store doctor could not read
    # has no verdict at all, so it must not be hidden behind the other store's.
    s = dc.replace(
        _state(),
        conversation_rollup_pricing_observation=_observation(
            "degraded", error_kind="operational_error"),
        cache_rollup_pricing_refusal={
            "process_snapshot_date": "2026-08-25",
            "store_snapshot_date": "2026-09-30",
            "first_refused_at_utc": "2026-09-03T08:00:00Z",
        },
    )
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.remediation == L.rollup_writer_degraded_read_remediation(
        "conversations.db")
    assert r.details["store"] == "conversations.db"


def test_a_degraded_read_can_never_fail_doctor():
    # Same posture as every other leg of this check: the guard working is not
    # a FAIL, and a transient lock must not move doctor's exit code.
    s = dc.replace(_state(),
                   conversation_rollup_pricing_observation=_observation(
                       "degraded", error_kind="operational_error"))
    assert L._check_pricing_conversation_rollup_writer(s).severity != "fail"


def test_a_present_or_absent_observation_leaves_the_check_ok():
    # Guard against over-reporting: the two ordinary states must stay silent,
    # or every healthy install warns.
    for state in ("absent", "present"):
        s = dc.replace(_state(),
                       conversation_rollup_pricing_observation=_observation(state),
                       cache_rollup_pricing_observation=_observation(state))
        assert L._check_pricing_conversation_rollup_writer(s).severity == "ok"


# --- #729: doctor warns on ACTIVE episodes only -----------------------------


def test_an_inactive_refusal_record_does_not_warn():
    # A store that diverged and then converged is healthy. Warning on the
    # surviving tombstone would make every recovered install warn forever,
    # which is why the record used to be deleted rather than settled.
    s = dc.replace(_state(), conversation_rollup_pricing_refusal={
        "process_snapshot_date": "2026-08-25",
        "store_snapshot_date": "2026-09-30",
        "first_refused_at_utc": "2026-09-03T08:00:00Z",
        "active": False,
    })
    assert L._check_pricing_conversation_rollup_writer(s).severity == "ok"


def test_an_active_refusal_record_still_warns():
    s = dc.replace(_state(), conversation_rollup_pricing_refusal={
        "process_snapshot_date": "2026-08-25",
        "store_snapshot_date": "2026-09-30",
        "first_refused_at_utc": "2026-09-03T08:00:00Z",
        "active": True,
    })
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.severity == "warn"
    assert r.details["first_refused_at_utc"] == "2026-09-03T08:00:00Z"


def test_a_record_written_before_the_active_field_existed_still_warns():
    # Back-compat: the older record was written only on refusal and DELETED on
    # convergence, so its presence alone means an active episode. Reading a
    # missing `active` as False would silently stop reporting every refusal
    # latched by a version that shipped before this field.
    s = dc.replace(_state(), conversation_rollup_pricing_refusal={
        "process_snapshot_date": "2026-08-25",
        "store_snapshot_date": "2026-09-30",
        "first_refused_at_utc": "2026-09-03T08:00:00Z",
    })
    assert L._check_pricing_conversation_rollup_writer(s).severity == "warn"


def test_an_inactive_record_in_one_store_does_not_mask_an_active_one():
    s = dc.replace(
        _state(),
        conversation_rollup_pricing_refusal={
            "process_snapshot_date": "2026-08-25",
            "store_snapshot_date": "2026-09-30",
            "first_refused_at_utc": "2026-09-03T08:00:00Z",
            "active": False,
        },
        cache_rollup_pricing_refusal={
            "process_snapshot_date": "2026-08-25",
            "store_snapshot_date": "2026-10-15",
            "first_refused_at_utc": "2026-09-05T08:00:00Z",
            "active": True,
        },
    )
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.severity == "warn"
    assert r.details["store"] == "cache.db"


# --- #769 S3 A1: a refusal latched during a DEGRADED read ------------------
# The store date a refusal records is the value the failed read produced, which
# is None. Doctor's DEGRADED branch fires only while the read is STILL failing,
# so once the store is readable again the record branch renders that None under
# the version-skew wording and attaches the version-skew remedy — advice that
# does nothing for a read that failed.


def _degraded_read_record(**extra):
    record = {
        "process_snapshot_date": "2026-09-05",
        "store_snapshot_date": None,
        "first_refused_at_utc": "2026-09-06T08:00:00Z",
    }
    record.update(extra)
    return record


def test_a_refusal_with_no_store_date_is_not_reported_as_version_skew():
    s = dc.replace(_state(),
                   conversation_rollup_pricing_refusal=_degraded_read_record())
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.severity == "warn"
    assert "None" not in r.summary, (
        "the summary renders the failed read's absent store date verbatim"
    )
    assert r.remediation != L.ROLLUP_WRITER_REFUSAL_REMEDIATION, (
        "restarting or upgrading the writer does not fix a read that failed"
    )


def test_a_refusal_with_no_store_date_carries_the_degraded_read_remedy():
    s = dc.replace(_state(),
                   conversation_rollup_pricing_refusal=_degraded_read_record())
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.remediation == L.rollup_writer_refused_degraded_read_remediation(
        "conversations.db")
    assert r.details["store"] == "conversations.db"
    assert r.details["store_snapshot_date"] is None
    assert r.details["first_refused_at_utc"] == "2026-09-06T08:00:00Z"


def test_a_refusal_naming_a_store_date_keeps_the_version_skew_remedy():
    # The negative direction: the ordinary refusal must not be re-routed.
    s = dc.replace(_state(), conversation_rollup_pricing_refusal={
        "process_snapshot_date": "2026-08-25",
        "store_snapshot_date": "2026-09-30",
        "first_refused_at_utc": "2026-09-03T08:00:00Z",
    })
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.remediation == L.ROLLUP_WRITER_REFUSAL_REMEDIATION
    assert "2026-09-30" in r.summary


def test_an_inactive_degraded_read_record_still_does_not_warn():
    s = dc.replace(_state(),
                   conversation_rollup_pricing_refusal=_degraded_read_record(
                       active=False))
    assert L._check_pricing_conversation_rollup_writer(s).severity == "ok"


# --- #769 S3 A7: the remedy names the database it is talking about ---------


@pytest.mark.parametrize("field,store,db_flag", [
    ("conversation_rollup_pricing_observation", "conversations.db",
     "--db conversations"),
    ("cache_rollup_pricing_observation", "cache.db", "--db cache"),
])
def test_the_degraded_read_remedy_names_the_affected_database(
        field, store, db_flag):
    """`db checkpoint` takes a `--db` value, and the check already knows which
    store it is reporting. Naming the other one sends the operator to drain a
    WAL that is not the one holding the read."""
    s = dc.replace(_state(), **{
        field: _observation("degraded", error_kind="operational_error")})
    r = L._check_pricing_conversation_rollup_writer(s)
    assert r.details["store"] == store
    assert db_flag in r.remediation
    other = "--db cache" if db_flag == "--db conversations" else (
        "--db conversations")
    assert other not in r.remediation
