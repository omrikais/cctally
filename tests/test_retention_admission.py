"""#496 S6 §5.2 — the one central admission predicate, pure.

Revision 3 stated `hook-tick` as a mandatory condition and then said ordinary
commands enter through the same predicate, which read literally means no
ordinary command can ever qualify. Revision 4 replaced that with a disjunction:

    new_plan = eligible_hook_tick_branch OR eligible_mutating_command_branch
    recovery = eligible_invocation AND a pending plan exists

The predicate takes no clock and no filesystem: the daily rate limit and the
presence of a pending plan are handed in as booleans.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from _lib_artifact_retention import (  # noqa: E402
    RETENTION_MUTATING_COMMANDS,
    RETENTION_PREVIEW_BY_DEFAULT,
    retention_admission,
    retention_admission_possible,
)


def admits(**kw):
    return retention_admission(**kw) == "new-plan"


# --------------------------------------------------------------------------
# The disjunction
# --------------------------------------------------------------------------


def test_new_plan_admission_is_a_disjunction_of_two_branches():
    assert admits(command="hook-tick", hook_forked=True, exit_code=0) is True
    assert admits(command="sync-week", exit_code=0) is True
    # Inline fallback: `os.fork()` failed, so the hook body is running on
    # Claude Code's blocking path and must not gain a spawn.
    assert admits(command="hook-tick", hook_forked=False, exit_code=0) is False
    assert admits(
        command="hook-tick", hook_forked=True, hook_explain=True, exit_code=0,
    ) is False
    assert admits(
        command="hook-tick", hook_forked=True, hook_foreground=True, exit_code=0,
    ) is False


@pytest.mark.parametrize("cmd", [
    "statusline", "doctor", "report", "daily", "forecast", "dashboard",
    "_artifact-retention", "_stats-corruption-heal", "_update-check",
])
def test_never_admitting_commands(cmd):
    assert admits(command=cmd, exit_code=0) is False
    assert retention_admission(
        command=cmd, exit_code=0, pending_plan_present=True,
    ) == ""


@pytest.mark.parametrize("mode", ["preview", "yes"])
def test_db_prune_never_admits_in_any_mode(mode):
    """§5.2: not just preview.

    A successful `--yes` has already applied the plan, so an automatic one
    immediately afterwards is pure waste; a preview that triggered a real
    deletion would contradict its own output.
    """
    assert admits(command="db", action="prune", prune_mode=mode, exit_code=0) is False
    assert retention_admission(
        command="db", action="prune", prune_mode=mode, exit_code=0,
        pending_plan_present=True,
    ) == ""


def test_a_failed_command_never_admits():
    assert admits(command="sync-week", exit_code=3) is False
    assert admits(command="hook-tick", hook_forked=True, exit_code=1) is False


def test_the_daily_rate_limit_gates_a_new_plan():
    assert admits(command="sync-week", exit_code=0, rate_limited=True) is False
    assert admits(
        command="hook-tick", hook_forked=True, exit_code=0, rate_limited=True,
    ) is False


def test_pending_recovery_is_unconditional_and_not_rate_limited():
    """A crashed deletion must not wait 24 hours."""
    assert retention_admission(
        command="sync-week", exit_code=0, rate_limited=True,
        pending_plan_present=True,
    ) == "recovery"


def test_a_new_plan_outranks_recovery_because_the_sweep_resumes_first():
    """One pass does both: the worker resumes before it marks (§5.4)."""
    assert retention_admission(
        command="sync-week", exit_code=0, pending_plan_present=True,
    ) == "new-plan"


def test_a_failed_command_does_not_even_recover():
    assert retention_admission(
        command="statusline", exit_code=0, pending_plan_present=True,
    ) == ""
    assert retention_admission(
        command="sync-week", exit_code=3, pending_plan_present=True,
    ) == ""


# --------------------------------------------------------------------------
# The allowlist
# --------------------------------------------------------------------------


def test_the_mutating_allowlist_is_the_ten_commands_the_spec_names():
    assert RETENTION_MUTATING_COMMANDS == frozenset({
        "sync-week", "record-usage", "record-credit", "cache-sync",
        "db rebuild", "db rederive", "db journal-repair", "db vacuum",
        "db checkpoint", "db backup",
    })


@pytest.mark.parametrize("action", [
    "rebuild", "rederive", "journal-repair", "vacuum", "checkpoint", "backup",
])
def test_the_db_children_on_the_allowlist_admit_by_qualified_name(action):
    # `applied=True` because two of these six are preview-by-default and the
    # preview of one of them must not admit; see the section below.
    assert admits(command="db", action=action, exit_code=0, applied=True) is True


@pytest.mark.parametrize("action", ["status", "skip", "unskip", "recover", "repair"])
def test_a_db_child_that_is_not_on_the_allowlist_does_not_admit(action):
    assert admits(command="db", action=action, exit_code=0) is False


def test_read_only_status_is_an_annotation_not_a_computed_property():
    """§5.2: reports legitimately write to the cache and are still reads.

    `daily` and `report` sync the entry cache on every run, so "does it mutate
    the filesystem" would misclassify them. They are absent from the allowlist
    and therefore never admit.
    """
    for command in ("daily", "monthly", "weekly", "report", "session", "blocks"):
        assert admits(command=command, exit_code=0) is False


# --------------------------------------------------------------------------
# Preview-by-default commands
# --------------------------------------------------------------------------
#
# `db rederive` and `db journal-repair` are on the mutating allowlist because
# their APPLY mutates. Their PREVIEW does not, and `db journal-repair`'s
# preview carries a literal no-mutation contract that `bin/cctally` already
# honours by skipping the update hooks for it — a preview that wrote an
# admission marker broke
# `test_preview_lists_exact_violation_without_persistent_writes`.


@pytest.mark.parametrize("action", ["rederive", "journal-repair"])
def test_a_preview_of_a_preview_by_default_db_child_does_not_admit(action):
    assert admits(command="db", action=action, exit_code=0, applied=False) is False
    assert retention_admission(
        command="db", action=action, exit_code=0, applied=False,
        pending_plan_present=True,
    ) == ""


@pytest.mark.parametrize("action", ["rederive", "journal-repair"])
def test_the_apply_of_the_same_command_still_admits(action):
    assert admits(command="db", action=action, exit_code=0, applied=True) is True


@pytest.mark.parametrize("action", ["rebuild", "vacuum", "checkpoint", "backup"])
def test_a_command_that_has_no_preview_mode_is_unaffected(action):
    """`applied` is absent on a command with no `--yes`, and must not gate it."""
    assert admits(command="db", action=action, exit_code=0, applied=None) is True


def test_a_bare_mutating_command_is_unaffected_by_the_preview_gate():
    assert admits(command="sync-week", exit_code=0, applied=False) is True


def test_record_credit_is_gated_on_yes_like_every_preview_by_default_command():
    """`record-credit` is preview-and-confirm by default and sits on the
    mutating allowlist, so without this gate its PREVIEW admitted a sweep —
    the same class as the `db journal-repair` gap, on a command outside the
    `db` subgroup.
    """
    assert "record-credit" in RETENTION_PREVIEW_BY_DEFAULT
    assert admits(command="record-credit", exit_code=0, applied=False) is False
    assert admits(command="record-credit", exit_code=0, applied=True) is True


def test_every_preview_by_default_command_is_on_a_list_that_can_gate_it():
    """Non-vacuity: a name in the set that no branch reaches gates nothing."""
    for name in RETENTION_PREVIEW_BY_DEFAULT:
        assert (
            name in RETENTION_MUTATING_COMMANDS
            or name in ("db prune",)
        ), name


# --------------------------------------------------------------------------
# The cheap rejection comes first
# --------------------------------------------------------------------------


def test_the_pure_rejection_needs_no_clock_and_no_filesystem():
    """`cctally statusline` can never admit, and must not pay to learn it.

    The glue measured the daily rate limit (a `stat`) and the presence of a
    pending plan (a `glob` of the whole data directory) as ARGUMENTS to the
    predicate, so every statusline render paid a readdir before the pure
    predicate could reject the command.
    """
    assert retention_admission_possible(command="statusline") is False
    assert retention_admission_possible(command="doctor") is False
    assert retention_admission_possible(command="db", action="prune") is False
    assert retention_admission_possible(command="sync-week", exit_code=1) is False
    assert retention_admission_possible(
        command="record-credit", applied=False) is False
    assert retention_admission_possible(command="sync-week") is True
    assert retention_admission_possible(
        command="hook-tick", hook_forked=True) is True


def test_the_pure_rejection_agrees_with_the_full_predicate():
    """A second gate that disagreed with the one it fronts would be a bug."""
    cases = [
        dict(command="statusline"),
        dict(command="doctor"),
        dict(command="sync-week"),
        dict(command="sync-week", exit_code=2),
        dict(command="db", action="prune"),
        dict(command="db", action="rederive", applied=False),
        dict(command="db", action="rederive", applied=True),
        dict(command="record-credit", applied=False),
        dict(command="record-credit", applied=True),
        dict(command="hook-tick", hook_forked=True),
        dict(command="hook-tick", hook_forked=False),
        dict(command="hook-tick", hook_forked=True, hook_explain=True),
    ]
    for case in cases:
        possible = retention_admission_possible(**case)
        decided = retention_admission(
            **case, rate_limited=False, pending_plan_present=True,
        )
        assert possible == bool(decided), case
