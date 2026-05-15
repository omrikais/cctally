"""Unit tests for bin/_lib_doctor.py. See plan Tasks 1-11."""
import sys
import pathlib

# Add bin/ to path so `import _lib_doctor` resolves.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import dataclasses as dc
import datetime as dt
import pathlib
import _lib_doctor as L


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
        "dashboard_bind_stored", "runtime_bind",
        "config_json_error",
        "update_state", "update_state_error",
        "update_suppress", "update_suppress_error",
        "effective_update_available", "effective_update_reason",
        "now_utc", "cctally_version",
        "forked_bucket_counts",
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
    )
    base.update(overrides)
    return L.DoctorState(**base)


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


def test_run_checks_returns_six_categories():
    rep = L.run_checks(_state())
    assert {c.id for c in rep.categories} == {
        "install", "hooks", "auth", "db", "data", "safety"
    }


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
    assert cat_ids == ["install", "hooks", "auth", "db", "data", "safety"]


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
