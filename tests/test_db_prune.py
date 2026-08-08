"""#496 S6 §6 — `cctally db prune`.

Preview by default; `--yes` applies. The exit-code taxonomy is §6.2's and its
asymmetry is deliberate: a preview that reports a blocked bound still exits 0,
because it deleted nothing and nothing failed, while the same condition on an
apply exits 3 because the apply was the operation meant to resolve it.
"""
from __future__ import annotations

import json
import pathlib
import sys

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
    # `main()` runs the update/telemetry post-command hooks, which detach real
    # subprocesses. This is the established seam that keeps them out.
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    return ns, _cctally_core, _cctally_retention


def _set_policy(core, **block):
    core.CONFIG_PATH.write_text(
        json.dumps({"storage.artifact_retention": block}), encoding="utf-8",
    )


def _run(ns, capsys, argv):
    code = ns["main"](argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _old(core, stamp="20200101T000000", **kw):
    return build_incident(core.APP_DIR, "stats.db", stamp, **kw)


# --------------------------------------------------------------------------
# Preview and apply
# --------------------------------------------------------------------------


def test_preview_deletes_nothing_and_exits_zero(tmp_path, monkeypatch, capsys):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    incident = _old(core)
    _set_policy(core, max_age_days=1)
    code, out, _err = _run(ns, capsys, ["db", "prune"])
    assert code == 0
    assert "Nothing was deleted" in out
    assert incident.exists()


def test_yes_applies_and_reports_what_it_freed(tmp_path, monkeypatch, capsys):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    incident = _old(core)
    _set_policy(core, max_age_days=1)
    code, out, _err = _run(ns, capsys, ["db", "prune", "--yes"])
    assert code == 0
    assert "freed" in out.lower()
    assert not incident.exists()


def test_a_preview_and_the_apply_that_follows_plan_the_same_deletion(
    tmp_path, monkeypatch, capsys,
):
    """§5.7: one deletion implementation, so the preview must be honest.

    The apply classifies before it plans. A preview that did not fold the same
    verdict in would under-report on exactly the incidents `--yes` reclaims.
    """
    ns, core, _ret = _load(tmp_path, monkeypatch)
    build_bundle(core.APP_DIR, "stats.db", "20200101T000000", origin="stats.open")
    _old(core, stamp="20200101T000001", classified=False)
    _set_policy(core, max_age_days=1)
    _code, out, _err = _run(ns, capsys, ["db", "prune", "--json"])
    preview = json.loads(out)["plan"]["deleteIds"]
    _code, out, _err = _run(ns, capsys, ["db", "prune", "--yes", "--json"])
    applied = json.loads(out)["result"]["deletedIds"]
    assert preview and sorted(preview) == sorted(applied)


def test_a_no_op_apply_exits_zero(tmp_path, monkeypatch, capsys):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    _set_policy(core, max_age_days=3650)
    code, out, _err = _run(ns, capsys, ["db", "prune", "--yes"])
    assert code == 0
    assert "Freed" in out


# --------------------------------------------------------------------------
# What the report has to state
# --------------------------------------------------------------------------


def test_protected_rows_state_why(tmp_path, monkeypatch, capsys):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    incident = _old(core, classified=False)
    (incident / "classification.json").write_text(
        json.dumps({
            "schemaVersion": 1, "incident": incident.name,
            "method": "header-only", "confidence": "unknown", "evidence": {},
        }),
        encoding="utf-8",
    )
    _set_policy(core, max_age_days=1)
    _code, out, _err = _run(ns, capsys, ["db", "prune"])
    assert "Protected and never deleted" in out
    assert "classification is unknown" in out


def test_an_incident_with_no_verdict_at_all_reads_differently(
    tmp_path, monkeypatch, capsys,
):
    """"Considered and undecided" is not the same as "never looked at"."""
    ns, core, _ret = _load(tmp_path, monkeypatch)
    _old(core, classified=False)
    _set_policy(core, max_age_days=1)
    _code, out, _err = _run(ns, capsys, ["db", "prune"])
    assert "no classification recorded" in out


def test_the_shape_floor_line_appears_when_the_floor_kept_something(
    tmp_path, monkeypatch, capsys,
):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    _old(core, stamp="20200101T000000", shape="rare")
    _set_policy(core, max_age_days=1)
    _code, out, _err = _run(ns, capsys, ["db", "prune"])
    assert "damage-shape example" in out


def test_the_shape_floor_line_is_absent_when_it_kept_nothing(
    tmp_path, monkeypatch, capsys,
):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    _old(core, stamp="20200101T000000")
    _set_policy(core, max_age_days=1)
    _code, out, _err = _run(ns, capsys, ["db", "prune"])
    # The policy line always states `keep 8 damage-shape examples`, so the
    # floor's own line is identified by its distinctive clause instead.
    assert "would otherwise have removed" not in out


def test_the_policy_line_states_the_effective_policy(tmp_path, monkeypatch, capsys):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    _set_policy(core, max_age_days=14, max_count_per_family=None)
    _code, out, _err = _run(ns, capsys, ["db", "prune"])
    assert "keep 14 days" in out
    assert "per family" not in out


def test_a_stuck_reclaim_record_is_named_in_the_report(
    tmp_path, monkeypatch, capsys,
):
    ns, core, ret = _load(tmp_path, monkeypatch)
    (core.APP_DIR / ".reclaim-pending-stuck.json").write_text(json.dumps({
        "schemaVersion": ret.RECLAIM_RECORD_SCHEMA_VERSION,
        "planId": "stuck",
        "entries": [{
            "id": "quarantine/gone", "rootId": "quarantine/gone",
            "tombstone": "quarantine/.reclaiming-stuck-gone",
            "phase": "marking", "isDir": True, "device": 1, "inode": 2,
            "size": 0, "mtimeNs": 0,
            "error": "fail-closed: source and tombstone are both present",
            "firstFailedAtUtc": "2020-01-01T00:00:00Z", "failureCount": 9,
        }],
    }), encoding="utf-8")
    _set_policy(core, max_age_days=1)
    _code, out, _err = _run(ns, capsys, ["db", "prune"])
    assert "stuck for over a day" in out
    assert "quarantine/gone" in out


# --------------------------------------------------------------------------
# Exit codes (§6.2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["db", "prune"], ["db", "prune", "--yes"],
])
def test_a_malformed_policy_exits_two_in_both_modes(
    tmp_path, monkeypatch, capsys, argv,
):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    incident = _old(core)
    _set_policy(core, max_age_days=0)
    code, _out, err = _run(ns, capsys, argv)
    assert code == 2
    assert "malformed" in err
    assert incident.exists()


def test_a_config_file_that_is_not_json_also_exits_two(
    tmp_path, monkeypatch, capsys,
):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    core.CONFIG_PATH.write_text("{not json", encoding="utf-8")
    code, _out, _err = _run(ns, capsys, ["db", "prune", "--yes"])
    assert code == 2


def test_a_partial_apply_exits_three(tmp_path, monkeypatch, capsys):
    ns, core, ret = _load(tmp_path, monkeypatch)
    _old(core)
    _set_policy(core, max_age_days=1)
    real_rename = ret._rename_within_parent
    monkeypatch.setattr(
        ret, "_rename_within_parent",
        lambda src, dst: (_ for _ in ()).throw(OSError("busy")),
    )
    code, _out, _err = _run(ns, capsys, ["db", "prune", "--yes"])
    assert code == 3
    assert real_rename is not None


def test_protected_evidence_leaving_a_bound_unsatisfied_exits_three(
    tmp_path, monkeypatch, capsys,
):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    _old(core, classified=False)
    _set_policy(core, max_age_days=1)
    code, out, _err = _run(ns, capsys, ["db", "prune", "--yes"])
    assert code == 3
    assert "max_age_seconds" in out


def test_a_blocked_preview_exits_zero_with_status_blocked(
    tmp_path, monkeypatch, capsys,
):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    _old(core, classified=False)
    _set_policy(core, max_age_days=1)
    code, out, _err = _run(ns, capsys, ["db", "prune", "--json"])
    assert code == 0
    assert json.loads(out)["status"] == "blocked"


def test_the_shape_floor_alone_never_produces_a_blocked_verdict(
    tmp_path, monkeypatch, capsys,
):
    """§3.6: a permanent FAIL no action clears is worse than no check at all.

    Two of the maintainer's four damage shapes have exactly one example, so
    this is the state a healthy install reaches and stays in.
    """
    ns, core, _ret = _load(tmp_path, monkeypatch)
    _old(core, stamp="20200101T000000", shape="rare")
    _set_policy(core, max_age_days=1)
    code, out, _err = _run(ns, capsys, ["db", "prune", "--yes", "--json"])
    payload = json.loads(out)
    assert code == 0
    assert payload["unsatisfiedRules"] == []
    assert payload["plan"]["floorRetainedIds"]


# --------------------------------------------------------------------------
# The JSON envelope (§6.3)
# --------------------------------------------------------------------------


def test_json_is_stamped_first_and_camel_case(tmp_path, monkeypatch, capsys):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    _old(core)
    _set_policy(core, max_age_days=1)
    _code, out, _err = _run(ns, capsys, ["db", "prune", "--json"])
    payload = json.loads(out)
    assert list(payload)[:9] == [
        "schemaVersion", "status", "policy", "before", "plan", "protected",
        "result", "unsatisfiedRules", "errors",
    ]
    assert payload["schemaVersion"] == 1


def test_json_names_no_absolute_paths(tmp_path, monkeypatch, capsys):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    _old(core)
    build_backup(core.APP_DIR, "cache.db", "20200101T000000")
    _set_policy(core, max_age_days=1)
    _code, out, _err = _run(ns, capsys, ["db", "prune", "--json"])
    assert str(core.APP_DIR) not in out
    assert json.loads(out)["plan"]["deleteIds"]


def test_the_json_policy_block_reports_the_effective_policy(
    tmp_path, monkeypatch, capsys,
):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    _set_policy(core, max_age_days=14, max_total_mib=None)
    _code, out, _err = _run(ns, capsys, ["db", "prune", "--json"])
    policy = json.loads(out)["policy"]
    assert policy["maxAgeDays"] == 14
    assert policy["maxTotalMib"] is None
    assert policy["maxShapeExamples"] == 8


# --------------------------------------------------------------------------
# Backups (§3.7 / §6.1)
# --------------------------------------------------------------------------


def test_include_backups_reaches_user_backups_and_the_default_does_not(
    tmp_path, monkeypatch, capsys,
):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    user = build_backup(
        core.APP_DIR, "stats.db", "20200101T000000", machine=False,
    )
    _set_policy(core, max_age_days=1)
    _run(ns, capsys, ["db", "prune", "--yes"])
    assert user.exists()
    _run(ns, capsys, ["db", "prune", "--yes", "--include-backups"])
    assert not user.exists()


def test_an_unrecognized_bak_name_is_never_auto_deleted(
    tmp_path, monkeypatch, capsys,
):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    hand = core.APP_DIR / "stats.db.bak-pre-011-reversal"
    hand.write_bytes(b"hand-made")
    _set_policy(core, max_age_days=1)
    _run(ns, capsys, ["db", "prune", "--yes", "--include-backups"])
    assert hand.exists()


def test_a_machine_backup_family_is_reclaimed_whole(tmp_path, monkeypatch, capsys):
    ns, core, _ret = _load(tmp_path, monkeypatch)
    stem = build_backup(core.APP_DIR, "stats.db", "20200101T000000")
    _set_policy(core, max_age_days=1)
    _run(ns, capsys, ["db", "prune", "--yes"])
    assert not stem.exists()
    assert not (core.APP_DIR / f"{stem.name}-wal").exists()
    assert not (core.APP_DIR / f"{stem.name}.classification.json").exists()
