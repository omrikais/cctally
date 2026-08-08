"""#496 S6 §5.1, §5.2, §5.6 — the detached retention worker and its admission.

The worker runs on the shipped `_stats-corruption-heal` shape: a non-blocking
admission flock, a durable request marker written BEFORE the spawn, a
worker-active probe, and `_spawn_detached`.
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import sys
import time

import pytest

from conftest import load_script, redirect_paths
from test_retention_walk import (
    build_backup, build_bundle, build_incident, build_rebuild_record,
)


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    import _cctally_retention

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _cctally_core.LOG_DIR.mkdir(parents=True, exist_ok=True)
    return ns, _cctally_core, _cctally_retention


def _policy(ret, **kw):
    fields = {
        "max_age_seconds": None, "max_count_per_family": None,
        "max_total_bytes": None, "min_free_bytes": None,
        "max_shape_examples": 8,
    }
    fields.update(kw)
    return ret._kernel.RetentionPolicy(**fields)


def _old_incident(core, name_stamp="20200101T000000", **kw):
    return build_incident(core.APP_DIR, "stats.db", name_stamp, **kw)


# --------------------------------------------------------------------------
# Admission, end to end
# --------------------------------------------------------------------------


def test_a_reservation_makes_the_marker_durable_before_the_spawn(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    seen = {}

    def fake_spawn(command):
        seen["marker"] = ret._retention_request_path().exists()
        seen["command"] = command
        return True

    monkeypatch.setattr(
        sys.modules["_cctally_update"], "_spawn_detached", fake_spawn,
    )
    assert ret.defer_artifact_retention() == "spawned"
    assert seen["command"] == "_artifact-retention"
    assert seen["marker"] is True, (
        "the spawn must not run before the request marker is durable"
    )


def test_a_failed_spawn_drops_the_marker_so_the_next_command_is_admitted(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sys.modules["_cctally_update"], "_spawn_detached", lambda command: False,
    )
    assert ret.defer_artifact_retention() == "failed"
    assert not ret._retention_request_path().exists()


def test_a_fresh_marker_coalesces_rather_than_admitting_a_second_worker(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sys.modules["_cctally_update"], "_spawn_detached", lambda command: True,
    )
    assert ret.defer_artifact_retention() == "spawned"
    assert ret.defer_artifact_retention() == "pending"


def test_an_active_worker_coalesces_and_refreshes_the_stamp(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(ret, "_retention_worker_active", lambda: True)
    assert ret.reserve_artifact_retention("new-plan") == "pending"
    assert not ret._retention_request_path().exists()


def test_the_daily_stamp_is_written_on_the_admission_not_on_success(
    tmp_path, monkeypatch,
):
    """A throttle stamped only on success re-spawns a failing worker forever."""
    _ns, core, ret = _load(tmp_path, monkeypatch)
    assert ret.retention_rate_limited() is False
    assert ret.reserve_artifact_retention("new-plan") == "reserved"
    assert ret.retention_rate_limited() is True


def test_a_recovery_reservation_does_not_consume_the_daily_window(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    assert ret.reserve_artifact_retention("recovery") == "reserved"
    assert ret.retention_rate_limited() is False


def test_a_pending_reclaim_record_is_what_recovery_keys_on(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    assert ret.pending_reclaim_plan_present() is False
    (core.APP_DIR / ".reclaim-pending-1.json").write_text("{}", encoding="utf-8")
    assert ret.pending_reclaim_plan_present() is True


# --------------------------------------------------------------------------
# Where the worker may and may not be scheduled
# --------------------------------------------------------------------------


def test_the_worker_never_runs_on_the_statusline_path(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    spawned: list = []
    monkeypatch.setattr(
        sys.modules["_cctally_update"], "_spawn_detached",
        lambda command: spawned.append(command) or True,
    )
    for command in ("statusline", "doctor", "report", "_artifact-retention"):
        ret.maybe_defer_artifact_retention(command=command, exit_code=0)
    assert spawned == []


def test_a_command_that_cannot_admit_touches_the_filesystem_not_at_all(
    tmp_path, monkeypatch,
):
    """`cctally statusline` renders on every prompt, and it can never admit.

    The rate-limit probe is a `stat` and the pending-plan probe is a `glob` of
    the whole data directory, and both were evaluated as ARGUMENTS to the pure
    predicate — so every render paid a readdir to learn what the command name
    alone decides. Counting the two probes is the observation; counting bytes
    or wall-clock would not fail deterministically.
    """
    _ns, _core, ret = _load(tmp_path, monkeypatch)
    probes = {"rate_limit": 0, "pending": 0}
    monkeypatch.setattr(
        ret, "retention_rate_limited",
        lambda **kw: probes.__setitem__(
            "rate_limit", probes["rate_limit"] + 1) or False,
    )
    monkeypatch.setattr(
        ret, "pending_reclaim_plan_present",
        lambda **kw: probes.__setitem__(
            "pending", probes["pending"] + 1) or False,
    )
    monkeypatch.setattr(
        sys.modules["_cctally_update"], "_spawn_detached", lambda command: True,
    )
    for command in ("statusline", "doctor", "report", "_artifact-retention"):
        ret.maybe_defer_artifact_retention(command=command, exit_code=0)
    assert probes == {"rate_limit": 0, "pending": 0}
    # Non-vacuity: a command that CAN admit still measures both.
    ret.maybe_defer_artifact_retention(command="sync-week", exit_code=0)
    assert probes["rate_limit"] == 1 and probes["pending"] == 1


def test_a_record_credit_preview_schedules_nothing(tmp_path, monkeypatch):
    _ns, _core, ret = _load(tmp_path, monkeypatch)
    spawned: list = []
    monkeypatch.setattr(
        sys.modules["_cctally_update"], "_spawn_detached",
        lambda command: spawned.append(command) or True,
    )
    ret.maybe_defer_artifact_retention(
        command="record-credit", exit_code=0, applied=False,
    )
    assert spawned == []
    assert ret.maybe_defer_artifact_retention(
        command="record-credit", exit_code=0, applied=True,
    ) == "spawned"
    assert spawned == ["_artifact-retention"]


def test_a_successful_mutating_command_schedules_one_sweep(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    spawned: list = []
    monkeypatch.setattr(
        sys.modules["_cctally_update"], "_spawn_detached",
        lambda command: spawned.append(command) or True,
    )
    assert ret.maybe_defer_artifact_retention(
        command="sync-week", exit_code=0,
    ) == "spawned"
    assert spawned == ["_artifact-retention"]


def test_db_prune_never_schedules_a_sweep_in_either_mode(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    spawned: list = []
    monkeypatch.setattr(
        sys.modules["_cctally_update"], "_spawn_detached",
        lambda command: spawned.append(command) or True,
    )
    ret.maybe_defer_artifact_retention(command="db", action="prune", exit_code=0)
    assert spawned == []


def test_the_env_kill_switch_disables_scheduling(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    monkeypatch.setenv("CCTALLY_DISABLE_RETENTION_SWEEP", "1")
    monkeypatch.setattr(
        sys.modules["_cctally_update"], "_spawn_detached",
        lambda command: pytest.fail("spawned under the kill switch"),
    )
    assert ret.maybe_defer_artifact_retention(
        command="sync-week", exit_code=0,
    ) == ""


# --------------------------------------------------------------------------
# The sweep the worker runs
# --------------------------------------------------------------------------


def test_a_malformed_policy_skips_deletion_entirely(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    incident = _old_incident(core)
    core.CONFIG_PATH.write_text(
        json.dumps({"storage.artifact_retention": {"max_age_days": True}}),
        encoding="utf-8",
    )
    result = ret.run_retention_sweep()
    assert result.status == "policy-malformed"
    assert result.applied is False
    assert "max_age_days" in result.reason
    assert incident.exists()


def test_a_config_file_that_is_not_valid_json_is_malformed_not_default(
    tmp_path, monkeypatch,
):
    """§6.5 C14: `load_config()` turns corrupt JSON into defaults."""
    _ns, core, ret = _load(tmp_path, monkeypatch)
    core.CONFIG_PATH.write_text("{not json", encoding="utf-8")
    resolution = ret.read_retention_policy()
    assert resolution.status == "malformed"
    assert resolution.policy is None


def test_a_dev_binary_declines_to_prune_the_production_directory(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    incident = _old_incident(core)
    monkeypatch.setattr(ret, "would_block_prod_retention", lambda root=None: True)
    result = ret.run_retention_sweep(policy=_policy(ret, max_age_seconds=1))
    assert result.status == "prod-refused"
    assert result.applied is False
    assert incident.exists()


def test_the_prod_guard_reads_the_real_data_dir_not_a_faked_home(
    tmp_path, monkeypatch,
):
    """Non-vacuity: the guard fires on the real prod dir and not on tmp_path."""
    _ns, core, ret = _load(tmp_path, monkeypatch)
    assert ret.would_block_prod_retention() is False
    monkeypatch.setattr(
        sys.modules["_cctally_core"], "_real_prod_data_dir",
        lambda: pathlib.Path(core.APP_DIR),
    )
    if (sys.modules["_cctally_core"]._repo_root() / ".git").exists():
        assert ret.would_block_prod_retention() is True


def test_a_sweep_with_nothing_to_delete_succeeds(tmp_path, monkeypatch):
    """An empty plan must not raise: it is the ordinary steady state."""
    _ns, core, ret = _load(tmp_path, monkeypatch)
    result = ret.run_retention_sweep(policy=_policy(ret, max_age_seconds=86400))
    assert result.status == "ok"
    assert result.outcome.deleted_ids == ()


def test_the_sweep_deletes_an_old_classified_incident(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    incident = _old_incident(core)
    result = ret.run_retention_sweep(policy=_policy(ret, max_age_seconds=1))
    assert result.status == "ok"
    assert f"quarantine/{incident.name}" in result.outcome.deleted_ids
    assert not incident.exists()


def test_the_sweep_never_deletes_an_unclassified_incident(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    incident = _old_incident(core, classified=False)
    result = ret.run_retention_sweep(policy=_policy(ret, max_age_seconds=1))
    assert result.outcome.deleted_ids == ()
    assert incident.exists()
    assert "max_age_seconds" in result.plan.unsatisfied_rules


def test_the_sweep_classifies_before_it_plans(tmp_path, monkeypatch):
    """§4.5: the backfill runs inside the worker's EXISTING exclusive hold.

    An incident with a v1 manifest and a correlating bundle is unclassified on
    disk, so a sweep that planned first would protect it forever.
    """
    _ns, core, ret = _load(tmp_path, monkeypatch)
    build_bundle(core.APP_DIR, "stats.db", "20200101T000000", origin="stats.open")
    incident = _old_incident(core, name_stamp="20200101T000001", classified=False)
    result = ret.run_retention_sweep(policy=_policy(ret, max_age_seconds=1))
    assert f"quarantine/{incident.name}" in result.outcome.deleted_ids
    assert not incident.exists()


def test_the_worker_holds_no_cache_flock(tmp_path, monkeypatch):
    """The binding lock-order constraint, observed rather than only scanned.

    A holder of `artifact-retention.lock` that waits on `cache.db.lock` closes
    a real cycle against `db rederive --yes`. The static scan in
    `tests/test_artifact_retention_lock_order.py` is the primary guard; this
    watches an actual sweep open descriptors.
    """
    _ns, core, ret = _load(tmp_path, monkeypatch)
    _old_incident(core)
    forbidden = {
        str(core.CACHE_LOCK_PATH), str(core.CACHE_LOCK_CODEX_PATH),
        str(core.CACHE_LOCK_MAINTENANCE_PATH),
        str(core.CONVERSATIONS_LOCK_PATH),
        str(core.STATS_LOCK_MAINTENANCE_PATH),
        str(core.JOURNAL_INGEST_LOCK_PATH),
    }
    # Captured BEFORE the sweep: `monkeypatch.undo()` would revert
    # `redirect_paths` too, and the constant would then read the real home.
    expected_lock = str(core.ARTIFACT_RETENTION_LOCK_PATH)
    opened: list = []
    real_open = os.open

    def watching_open(path, *args, **kw):
        opened.append(str(path))
        return real_open(path, *args, **kw)

    monkeypatch.setattr(os, "open", watching_open)
    ret.run_retention_sweep(policy=_policy(ret, max_age_seconds=1))
    assert forbidden.isdisjoint(opened), sorted(forbidden.intersection(opened))
    # Non-vacuity: the sweep really did open its own lock.
    assert expected_lock in opened


def test_a_worker_that_cannot_take_the_lock_marks_nothing(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    incident = _old_incident(core)
    monkeypatch.setattr(
        ret, "_acquire_retention_flock", lambda mode, timeout: False,
    )
    result = ret.run_retention_sweep(policy=_policy(ret, max_age_seconds=1))
    assert result.status == "blocked"
    assert result.applied is False
    assert incident.exists()


# --------------------------------------------------------------------------
# The worker entry point
# --------------------------------------------------------------------------


def test_the_worker_runs_the_sweep_the_marker_asked_for(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    incident = _old_incident(core)
    core.CONFIG_PATH.write_text(
        json.dumps({"storage.artifact_retention": {"max_age_days": 1}}),
        encoding="utf-8",
    )
    assert ret.reserve_artifact_retention("new-plan") == "reserved"
    assert ret.cmd_artifact_retention_internal(None) == 0
    assert not incident.exists()
    assert not ret._retention_request_path().exists()
    assert "ok" in ret._retention_log_path().read_text(encoding="utf-8")


def test_a_recovery_worker_only_resumes(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    incident = _old_incident(core)
    core.CONFIG_PATH.write_text(
        json.dumps({"storage.artifact_retention": {"max_age_days": 1}}),
        encoding="utf-8",
    )
    assert ret.reserve_artifact_retention("recovery") == "reserved"
    assert ret.cmd_artifact_retention_internal(None) == 0
    assert incident.exists(), "recovery must not start a new plan"


def test_the_worker_returns_zero_when_the_sweep_raises(tmp_path, monkeypatch):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    ret.reserve_artifact_retention("new-plan")

    def boom(**kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(ret, "run_retention_sweep", boom)
    assert ret.cmd_artifact_retention_internal(None) == 0
    assert "error=RuntimeError" in ret._retention_log_path().read_text(
        encoding="utf-8"
    )


def test_a_second_worker_that_loses_the_worker_flock_does_nothing(
    tmp_path, monkeypatch,
):
    _ns, core, ret = _load(tmp_path, monkeypatch)
    incident = _old_incident(core)
    core.CONFIG_PATH.write_text(
        json.dumps({"storage.artifact_retention": {"max_age_days": 1}}),
        encoding="utf-8",
    )
    ret.reserve_artifact_retention("new-plan")
    holder = os.open(ret._retention_worker_path(), os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert ret.cmd_artifact_retention_internal(None) == 0
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    assert incident.exists()
