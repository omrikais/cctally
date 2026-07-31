"""Incremental Codex quota reconciliation — reproduction + equivalence oracle.

Public issue omrikais/cctally#5 ("Codex hook takes about 30 seconds after nearly
every prompt and often times out"). Spec:
``docs/superpowers/specs/2026-07-31-codex-hook-incremental-quota-reconcile-design.md``.

``test_appending_one_observation_expands_only_its_group`` is the ROOT-CAUSE
PROOF. It asserts work done rather than wall-clock: against a store of many
closed windows plus one live one, appending a single observation must expand
exactly one physical group. Today's reconcile loads every observation in the
store, so it expands all of them — that failure IS the defect, stated as a
measurement instead of a stopwatch.

``test_incremental_matches_whole_history`` is the SAFETY NET. Its two arms now
genuinely differ — one appends an observation and lets the ledger bound the pass,
the other re-materializes everything through ``force_full`` — so the equality is
the real claim that a bounded pass and a whole-history pass agree. To keep it
from being a vacuous "empty == empty", the non-vacuity assertions below pin that
the dump actually observes the projection (blocks, milestones, projection state,
arming and terminal threshold evidence), and
``test_canonical_dump_detects_a_projection_difference`` pins that it can tell two
different projections apart.
"""
from __future__ import annotations

import datetime as dt
import importlib

import pytest

from conftest import load_script, redirect_paths
from helpers.quota_projection_dump import (
    ROOT_KEY,
    append_one_observation_to_live_window,
    build_codex_quota_store,
    canonical_projection_dump,
    count_expanded_groups,
    enable_quota_alerts,
    store_as_of,
    wipe_projection,
)


# A store big enough that "loads everything" and "loads one group" cannot be
# confused, and small enough to reconcile twice inside a unit test.
CLOSED_WINDOWS = 200
OBSERVATIONS_PER_WINDOW = 10


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    quota = importlib.import_module("_cctally_quota")
    return ns, quota


def _force_full_reconcile(quota, **kwargs):
    """Run a provably whole-history pass.

    ``force_full=True`` is the incremental projector's own bypass: it skips the
    ledger, the watermark and the periodic-verification deadline alike, so the
    comparison arm is whole-history by construction rather than by coincidence.
    """
    return quota.reconcile_codex_quota_projection(force_full=True, **kwargs)


def test_appending_one_observation_expands_only_its_group(tmp_path, monkeypatch):
    """A single new observation must not re-materialize all history."""
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns,
        closed_windows=CLOSED_WINDOWS,
        observations_per_window=OBSERVATIONS_PER_WINDOW,
    )
    now = store_as_of(ns)
    quota.reconcile_codex_quota_projection(now=now)  # prime

    dirty_group = append_one_observation_to_live_window(ns)

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now)

    assert counter.groups == 1, (
        f"expected exactly one physical group expanded, got {counter.groups} "
        f"({counter.observations} observations loaded across {counter.calls} "
        f"call(s)) — the pass is proportional to all history, not to the change"
    )
    assert dirty_group in counter.group_keys, (
        "the expanded group is not the one the new observation landed in"
    )


def test_incremental_matches_whole_history(tmp_path, monkeypatch):
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns,
        closed_windows=120,
        observations_per_window=OBSERVATIONS_PER_WINDOW,
    )
    # Alert-bearing on purpose. Without an eligible root and an enabled gate the
    # oracle reads `quota_threshold_events` and `quota_alert_arming` as empty in
    # BOTH arms, so the equivalence assertion covers them vacuously — and they
    # are precisely what the scoped sweep (Task 8) and the alert axes (Task 10)
    # put at risk. `before_reset` is small so `now` lands AFTER the newest
    # capture; a future-clocked history is classified `future` and skips every
    # threshold decision.
    enable_quota_alerts(ns, actual=(2,))
    now = store_as_of(ns, before_reset=dt.timedelta(minutes=10))
    eligible = {ROOT_KEY}

    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys=eligible)
    append_one_observation_to_live_window(ns)
    with count_expanded_groups(quota) as bounded:
        quota.reconcile_codex_quota_projection(
            now=now, alert_eligible_root_keys=eligible)
    # SELF-GUARDING. Equality between the two arms holds trivially if the
    # "incremental" arm silently loaded everything, so this test could not
    # catch the regression it is named for without pinning that the arm was
    # actually bounded.
    assert bounded.groups == 1, (
        f"the incremental arm expanded {bounded.groups} groups, so the "
        f"equality below compares two whole-history passes")
    stats = ns["open_db"]()
    try:
        incremental = canonical_projection_dump(stats)
    finally:
        stats.close()

    stats = ns["open_db"]()
    try:
        wipe_projection(stats)
    finally:
        stats.close()
    _force_full_reconcile(quota, now=now, alert_eligible_root_keys=eligible)
    stats = ns["open_db"]()
    try:
        whole_history = canonical_projection_dump(stats)
    finally:
        stats.close()

    # Non-vacuity: an empty projection would make any two dumps equal. One
    # assertion per table the dump covers — the alert tables included, because
    # "both empty" is the failure mode that silently voids this test.
    assert len(whole_history["blocks"]) == 121, (
        f"expected one block per seeded window, got "
        f"{len(whole_history['blocks'])}"
    )
    assert whole_history["milestones"], "no milestone ladder was materialized"
    assert whole_history["projection_state"], "no projection state was stamped"
    assert whole_history["alert_arming"], (
        "no arming boundary was written — the oracle would cover "
        "quota_alert_arming vacuously")
    assert whole_history["threshold_events"], (
        "no terminal threshold evidence was written — the oracle would cover "
        "quota_threshold_events vacuously")

    assert incremental == whole_history


def test_canonical_dump_detects_a_projection_difference(tmp_path, monkeypatch):
    """The oracle must fail when the projection genuinely differs.

    Without this, ``test_incremental_matches_whole_history`` could pass because
    the dump throws away everything that could disagree.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns, closed_windows=3, observations_per_window=OBSERVATIONS_PER_WINDOW)
    now = store_as_of(ns)
    quota.reconcile_codex_quota_projection(now=now)

    stats = ns["open_db"]()
    try:
        before = canonical_projection_dump(stats)
        stats.execute(
            "UPDATE quota_window_blocks SET current_percent = current_percent + 1 "
            "WHERE id = (SELECT MIN(id) FROM quota_window_blocks)")
        stats.commit()
        after_value = canonical_projection_dump(stats)
        stats.execute(
            "UPDATE quota_window_blocks SET orphaned_at = '2026-07-02T00:00:00Z' "
            "WHERE id = (SELECT MIN(id) FROM quota_window_blocks)")
        stats.commit()
        after_orphan = canonical_projection_dump(stats)
        stats.execute("DELETE FROM quota_percent_milestones")
        stats.commit()
        after_delete = canonical_projection_dump(stats)
    finally:
        stats.close()

    assert before != after_value, "a changed block percent was not observed"
    assert after_value != after_orphan, "an orphaned block was not observed"
    assert after_orphan != after_delete, "a deleted milestone was not observed"


def test_canonical_dump_ignores_per_pass_provenance(tmp_path, monkeypatch):
    """Re-running the SAME whole-history pass must produce the SAME dump.

    Each pass mints a fresh random ``generation`` and re-stamps
    ``completed_at_utc``, so a dump that kept them could never compare equal —
    the equivalence test would fail for a reason that is not about correctness.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns, closed_windows=3, observations_per_window=OBSERVATIONS_PER_WINDOW)
    now = store_as_of(ns)

    quota.reconcile_codex_quota_projection(now=now)
    stats = ns["open_db"]()
    try:
        first = canonical_projection_dump(stats)
        generations = {
            row[0] for row in stats.execute(
                "SELECT generation FROM quota_window_blocks")
        }
        wipe_projection(stats)
    finally:
        stats.close()

    _force_full_reconcile(quota, now=now)
    stats = ns["open_db"]()
    try:
        second = canonical_projection_dump(stats)
        regenerations = {
            row[0] for row in stats.execute(
                "SELECT generation FROM quota_window_blocks")
        }
    finally:
        stats.close()

    assert generations and regenerations
    assert generations != regenerations, (
        "the two passes reused one generation — the dump's provenance "
        "exclusions would then be untested")
    assert first == second


def test_store_seeds_one_physical_group_per_window(tmp_path, monkeypatch):
    """The fixture's premise: every seeded window is its OWN physical group.

    If the windows collapsed into one group, ``groups == 1`` would hold for the
    wrong reason and the reproduction test would be vacuous.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns, closed_windows=5, observations_per_window=OBSERVATIONS_PER_WINDOW)

    with count_expanded_groups(quota) as counter:
        observations = quota.load_codex_quota_observations(
            source_root_keys={ROOT_KEY})

    assert len(observations) == 6 * OBSERVATIONS_PER_WINDOW
    assert counter.groups == 6


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
