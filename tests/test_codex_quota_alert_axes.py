"""The five alert-invalidation axes (public #5 §3, Task 10).

Alert state is NOT a function of window dirtiness. A projector bounded by the
change ledger sees exactly one of the five things that can make an alert
eligible, so the other four have to be detected some other way or a bounded pass
silently stops honouring them. Each axis below is a way the answer changes with
no row in ``quota_window_snapshots`` moving at all.

The kernel decides a SUPERSET scope on purpose: widening a bounded pass costs a
load, missing an identity costs an alert.
"""
from __future__ import annotations

import datetime as dt
import importlib

import pytest

import _lib_quota_alert_axes as axes
from conftest import load_script, redirect_paths
from helpers.quota_projection_dump import (
    ROOT_KEY,
    build_codex_quota_store,
    count_expanded_groups,
    enable_quota_alerts,
    store_as_of,
)


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

IDENTITY_A = ("codex", "root-a", "unattributed", "limit", "primary", 300)
IDENTITY_B = ("codex", "root-b", "unattributed", "limit", "primary", 300)


def _scope(**overrides):
    kwargs = {
        "ledger_groups": (),
        "stored_fingerprints": {},
        "resolved_fingerprints": {},
        "gate_before": True,
        "gate_after": True,
        "now": NOW,
        "next_evaluation_at": None,
        "scheduled_roots": None,
    }
    kwargs.update(overrides)
    return axes.alert_dirty_scope(**kwargs)


# ── axis 3: the delivery gate ──────────────────────────────────────────────

def test_disabling_needs_no_observations_at_all():
    """Disabling is pure state removal.

    It must enumerate every arming row, delete it and journal a disarm even with
    zero dirty windows and zero lifecycle-eligible roots — an arming boundary
    that survives a disable would turn disabled-period evidence into a later
    alert.
    """
    scope = _scope(gate_after=False, ledger_groups=(1, 2))
    assert scope.disarm_all is True
    assert scope.scope == axes.SCOPE_NONE
    assert scope.reasons == ("gate_disabled",)


def test_enabling_requires_a_semantic_pass():
    """Activation has to write ``suppressed_backfill`` terminal rows for
    already-satisfied thresholds instead of dispatching history, which it can
    only do if it actually sees the blocks."""
    scope = _scope(gate_before=False, gate_after=True)
    assert scope.disarm_all is False
    assert scope.scope == axes.SCOPE_ALL
    assert "gate_enabled" in scope.reasons


def test_an_unknown_prior_gate_is_treated_as_an_enable():
    """A first pass has no recorded gate state and cannot be distinguished from
    one that just flipped on, so it must not be assumed to be a no-op."""
    assert _scope(gate_before=None).scope == axes.SCOPE_ALL


def test_a_steady_enabled_gate_adds_nothing():
    assert _scope(gate_before=True, gate_after=True).scope == axes.SCOPE_NONE


# ── axis 1: physical-window dirtiness ──────────────────────────────────────

def test_a_dirty_window_scopes_to_its_groups():
    scope = _scope(ledger_groups=(1,))
    assert scope.scope == axes.SCOPE_GROUPS
    assert scope.widens(axes.SCOPE_GROUPS) is False


# ── axis 2: policy scope ───────────────────────────────────────────────────

def test_a_changed_rule_scopes_to_its_roots():
    scope = _scope(
        stored_fingerprints={IDENTITY_A: "old", IDENTITY_B: "same"},
        resolved_fingerprints={IDENTITY_A: "new", IDENTITY_B: "same"},
    )
    assert scope.scope == axes.SCOPE_ROOTS
    assert scope.roots == frozenset({"root-a"})
    assert "rule_changed" in scope.reasons


def test_an_unchanged_rule_adds_nothing():
    scope = _scope(
        stored_fingerprints={IDENTITY_A: "same"},
        resolved_fingerprints={IDENTITY_A: "same"},
    )
    assert scope.scope == axes.SCOPE_NONE


def test_a_newly_resolvable_identity_counts_as_changed():
    """No stored fingerprint means the identity was never armed under this rule,
    so it has an activation owing."""
    scope = _scope(
        stored_fingerprints={},
        resolved_fingerprints={IDENTITY_A: "new"},
    )
    assert scope.scope == axes.SCOPE_ROOTS
    assert scope.roots == frozenset({"root-a"})


# ── axis 4: scheduled time ─────────────────────────────────────────────────

def test_a_legacy_boundary_that_has_come_due_widens_to_everything():
    """An observation captured in the future is skipped as a qualifier today and
    becomes eligible when wall time passes it, with no mutation to observe.
    Which identity it belongs to is not recorded — only the instant — so the
    honest scope is everything."""
    scope = _scope(next_evaluation_at=NOW - dt.timedelta(seconds=1))
    assert scope.scope == axes.SCOPE_ALL
    assert "scheduled" in scope.reasons


def test_a_due_boundary_with_owners_scopes_to_only_those_roots():
    """Persisted ownership makes axis 4 bounded enough for the hook path."""
    scope = _scope(
        next_evaluation_at=NOW - dt.timedelta(seconds=1),
        scheduled_roots=("root-a",),
        defer_scheduled=True,
    )
    assert scope.scope == axes.SCOPE_ROOTS
    assert scope.roots == frozenset({"root-a"})
    assert "scheduled" in scope.reasons
    assert axes.REASON_SCHEDULED_DEFERRED not in scope.reasons


def test_a_boundary_still_in_the_future_adds_nothing():
    assert _scope(
        next_evaluation_at=NOW + dt.timedelta(minutes=1)
    ).scope == axes.SCOPE_NONE


def test_a_boundary_exactly_at_now_is_due():
    assert _scope(next_evaluation_at=NOW).scope == axes.SCOPE_ALL


# ── axis 4 on the hook path: deferred, never silently dropped ──────────────

def test_the_hook_path_defers_the_scheduled_axis_instead_of_widening():
    """Axis 4 is the ONE widening route driven by wall clock rather than by a
    config change, so on a hook-only install it lands unannounced as the very
    whole-history pass `full_pass="defer"` exists to keep off the blocking path.
    It is deferred rather than honoured — and recorded, so the caller knows it
    still owes the boundary."""
    scope = _scope(next_evaluation_at=NOW, defer_scheduled=True)
    assert scope.scope == axes.SCOPE_NONE
    assert axes.REASON_SCHEDULED_DEFERRED in scope.reasons
    assert "scheduled" not in scope.reasons


def test_deferring_the_schedule_leaves_the_config_driven_axes_inline():
    """The two carve-outs that stay inline are unaffected: a delivery-gate
    ENABLE and a rule change must SEE the blocks to write `suppressed_backfill`
    terminal rows rather than dispatch history."""
    scope = _scope(
        gate_before=False, next_evaluation_at=NOW, defer_scheduled=True)
    assert scope.scope == axes.SCOPE_ALL
    assert "gate_enabled" in scope.reasons
    assert axes.REASON_SCHEDULED_DEFERRED in scope.reasons


def test_a_deferred_boundary_is_retained_not_recomputed():
    """`retain_due` is what makes deferral honest.

    Without it the very pass that declined to widen would still recompute the
    boundary from its own partial evidence, drop the matured instant, and the
    axis would never fire again — a silent loss dressed as a deferral.
    """
    stored = NOW - dt.timedelta(hours=1)
    assert axes.next_evaluation_boundary(
        capture_times=[NOW + dt.timedelta(hours=5)], now=NOW, stored=stored,
        retain_due=True,
    ) == stored
    assert axes.next_evaluation_boundary(
        capture_times=[NOW + dt.timedelta(hours=5)], now=NOW, stored=stored,
        retain_due=False,
    ) == NOW + dt.timedelta(hours=5)


# ── precedence ─────────────────────────────────────────────────────────────

def test_the_strongest_axis_wins():
    scope = _scope(
        ledger_groups=(1,),
        stored_fingerprints={IDENTITY_A: "old"},
        resolved_fingerprints={IDENTITY_A: "new"},
        next_evaluation_at=NOW,
    )
    assert scope.scope == axes.SCOPE_ALL
    assert set(scope.reasons) == {"window_dirty", "rule_changed", "scheduled"}


@pytest.mark.parametrize("scope_value,expected", [
    (axes.SCOPE_NONE, False),
    (axes.SCOPE_GROUPS, False),
    (axes.SCOPE_ROOTS, True),
    (axes.SCOPE_ALL, True),
])
def test_widens_reports_whether_the_ledger_is_enough(scope_value, expected):
    scope = axes.AlertDirtyScope(
        disarm_all=False, scope=scope_value, roots=frozenset(), reasons=())
    assert scope.widens(axes.SCOPE_GROUPS) is expected


# ── the boundary helper ────────────────────────────────────────────────────

def test_the_boundary_is_the_earliest_still_future_capture():
    assert axes.next_evaluation_boundary(
        capture_times=[
            NOW - dt.timedelta(hours=1),
            NOW + dt.timedelta(hours=2),
            NOW + dt.timedelta(hours=1),
        ],
        now=NOW, stored=None,
    ) == NOW + dt.timedelta(hours=1)


def test_a_stored_future_boundary_survives_a_bounded_pass():
    """A bounded pass only sees the dirty windows.

    Dropping the stored value would forget a future-clocked observation sitting
    in a window this pass never loaded. Retaining it can only cost one extra
    pass, never a missed one — once it comes due the axis widens to everything
    and it is recomputed from complete evidence.
    """
    stored = NOW + dt.timedelta(hours=3)
    assert axes.next_evaluation_boundary(
        capture_times=[], now=NOW, stored=stored) == stored


def test_a_past_boundary_is_dropped():
    assert axes.next_evaluation_boundary(
        capture_times=[NOW - dt.timedelta(minutes=5)], now=NOW,
        stored=NOW - dt.timedelta(days=1),
    ) is None


# ── integration ────────────────────────────────────────────────────────────

def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    quota = importlib.import_module("_cctally_quota")
    return ns, quota


def _arming_rows(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM quota_alert_arming WHERE source='codex'"
        ).fetchone()[0]
    finally:
        conn.close()


def _stored_boundary(ns):
    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT next_evaluation_at_utc FROM quota_projection_ledger_state "
            " WHERE source='codex'").fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _stored_schedule(ns):
    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT next_evaluation_by_root_json "
            "FROM quota_projection_ledger_state WHERE source='codex'"
        ).fetchone()
        return {} if row is None else __import__("json").loads(row[0])
    finally:
        conn.close()


def _stored_gate(ns):
    """The persisted delivery-gate state (axis 3's `gate_before`)."""
    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT alerts_enabled FROM quota_projection_ledger_state "
            " WHERE source='codex'").fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _dispositions(ns):
    """Terminal alert evidence by disposition, for the no-burst assertion."""
    conn = ns["open_db"]()
    try:
        return dict(conn.execute(
            "SELECT disposition, COUNT(*) FROM quota_threshold_events "
            " WHERE source='codex' GROUP BY disposition").fetchall())
    finally:
        conn.close()


def _stats_write(ns, sql, params=()):
    conn = ns["open_db"]()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_mismatched_scalar_and_owned_schedule_fail_safe(tmp_path, monkeypatch):
    ns, quota = _load(tmp_path, monkeypatch)
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO quota_projection_ledger_state "
            "(source, next_evaluation_at_utc, next_evaluation_by_root_json) "
            "VALUES ('codex', ?, ?)",
            (
                quota._utc_iso(NOW),
                '{"root-a":"2026-07-31T13:00:00+00:00"}',
            ),
        )
        assert quota._ledger_state(conn) is None
    finally:
        conn.close()


def test_disabling_disarms_a_store_with_nothing_dirty(tmp_path, monkeypatch):
    """The ordering the current implementation relies on, end to end.

    Arm the store, change nothing physical, turn delivery off, and reconcile
    with NO alert-eligible roots — a read-only report reconcile. The arming rows
    must still be gone.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(ns, closed_windows=3, observations_per_window=10)
    enable_quota_alerts(ns, actual=(2,))
    now = store_as_of(ns, before_reset=dt.timedelta(minutes=10))
    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys={ROOT_KEY})
    assert _arming_rows(ns) > 0, "the fixture armed nothing to disarm"

    import _cctally_core
    import json as _json
    _cctally_core.CONFIG_PATH.write_text(_json.dumps(
        {"alerts": {"enabled": False, "notifier": "none",
                    "quota": {"enabled": True, "actual_thresholds": [2],
                              "projected_thresholds": [], "rules": []}}}) + "\n")

    quota.reconcile_codex_quota_projection(now=now)

    assert _arming_rows(ns) == 0, (
        "a disabled gate left an arming boundary behind, so disabled-period "
        "evidence could still become an alert")


def test_a_future_capture_is_remembered_and_re_evaluated(tmp_path, monkeypatch):
    """Axis 4, end to end.

    The newest observations in this fixture sit AHEAD of ``now``, so they are
    skipped as threshold qualifiers. The instant is persisted, and a later tick
    whose clock has passed it must widen the pass instead of finding a clean
    ledger and doing nothing.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(ns, closed_windows=3, observations_per_window=10)
    enable_quota_alerts(ns, actual=(2,))
    early = store_as_of(ns, before_reset=dt.timedelta(hours=2))
    quota.reconcile_codex_quota_projection(
        now=early, alert_eligible_root_keys={ROOT_KEY})

    boundary = _stored_boundary(ns)
    assert boundary is not None, (
        "a future-clocked capture left no next-evaluation boundary")
    parsed = dt.datetime.fromisoformat(boundary.replace("Z", "+00:00"))
    assert parsed > early

    later = parsed + dt.timedelta(seconds=1)
    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(
            now=later, alert_eligible_root_keys={ROOT_KEY})

    assert counter.groups == 4, (
        f"the due boundary did not widen the pass (expanded {counter.groups} "
        f"of 4 groups) — a future-clocked observation would never be evaluated")


def test_a_quiet_due_hook_tick_reconciles_only_the_owning_root(
    tmp_path, monkeypatch,
):
    """Issue #460: a scheduled hook pass is bounded, consumed, and stable."""
    ns, quota = _load(tmp_path, monkeypatch)
    other_root = "root-later"
    build_codex_quota_store(ns, closed_windows=3, observations_per_window=2)
    build_codex_quota_store(
        ns, closed_windows=3, observations_per_window=2,
        root_key=other_root)
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "UPDATE quota_window_snapshots "
            "SET captured_at_utc=strftime('%Y-%m-%dT%H:%M:%SZ', "
            "    captured_at_utc, '+30 minutes') "
            "WHERE source_root_key=?", (other_root,))
        cache.commit()
    finally:
        cache.close()
    enable_quota_alerts(ns, actual=(2,))
    early = store_as_of(ns, before_reset=dt.timedelta(hours=2))
    eligible = {ROOT_KEY, other_root}
    observations = quota.load_codex_quota_observations()
    future_roots = {
        observation.identity.source_root_key
        for observation in observations
        if observation.captured_at > early
    }
    assert future_roots == eligible, (
        f"the acceptance fixture did not retain future captures for {eligible}; "
        f"got {future_roots}")
    quota.reconcile_codex_quota_projection(
        now=early, alert_eligible_root_keys=eligible)

    schedule = _stored_schedule(ns)
    assert set(schedule) == eligible
    first = dt.datetime.fromisoformat(
        schedule[ROOT_KEY].replace("Z", "+00:00"))
    later = dt.datetime.fromisoformat(
        schedule[other_root].replace("Z", "+00:00"))
    assert early < first < later

    with count_expanded_groups(quota) as due:
        quota.reconcile_codex_quota_projection(
            now=first + dt.timedelta(seconds=1),
            alert_eligible_root_keys=eligible,
            full_pass="defer",
        )
    assert due.groups == 4, (
        f"the first due hook tick expanded {due.groups} groups instead of "
        "the four belonging to its recorded root")
    assert _stored_boundary(ns) == schedule[other_root]

    with count_expanded_groups(quota) as quiet:
        quota.reconcile_codex_quota_projection(
            now=first + dt.timedelta(seconds=2),
            alert_eligible_root_keys=eligible,
            full_pass="defer",
        )
    assert quiet.groups == 0, (
        f"the quiet follow-up expanded {quiet.groups} groups after the due "
        "root had already been evaluated")


def test_due_root_and_dirty_root_converge_before_watermark_and_certificate(
    tmp_path, monkeypatch,
):
    """A due owner must not make simultaneous ledger work disappear.

    Root A owns the matured schedule, root B has one physically dirty group,
    and root C is clean. The combined pass must promote A and B to complete
    root histories (four groups), leave C unloaded, consume/prune the ledger
    only after B converges, and still publish a certificate for all three roots.
    """
    import _fixture_builders

    ns, quota = _load(tmp_path, monkeypatch)
    dirty_root = "root-dirty"
    clean_root = "root-clean"
    for root_key in (ROOT_KEY, dirty_root, clean_root):
        build_codex_quota_store(
            ns, closed_windows=1, observations_per_window=2,
            root_key=root_key)
    cache = ns["open_cache_db"]()
    try:
        for root_key, minutes in ((dirty_root, 30), (clean_root, 60)):
            cache.execute(
                "UPDATE quota_window_snapshots "
                "SET captured_at_utc=strftime('%Y-%m-%dT%H:%M:%SZ', "
                "    captured_at_utc, ?) WHERE source_root_key=?",
                (f"+{minutes} minutes", root_key),
            )
        cache.commit()
    finally:
        cache.close()

    roots = {ROOT_KEY, dirty_root, clean_root}
    enable_quota_alerts(ns, actual=(2,))
    early = store_as_of(ns, before_reset=dt.timedelta(hours=2))
    quota.reconcile_codex_quota_projection(
        now=early, alert_eligible_root_keys=roots)
    schedule = _stored_schedule(ns)
    due_at = dt.datetime.fromisoformat(
        schedule[ROOT_KEY].replace("Z", "+00:00"))
    assert due_at < dt.datetime.fromisoformat(
        schedule[dirty_root].replace("Z", "+00:00"))

    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "UPDATE quota_window_snapshots SET used_percent=used_percent+0.25 "
            "WHERE id=(SELECT MAX(id) FROM quota_window_snapshots "
            "WHERE source_root_key=?)", (dirty_root,))
        _fixture_builders.bump_codex_physical_mutation_seq(cache)
        cache.commit()
        ledger_high = cache.execute(
            "SELECT seq FROM sqlite_sequence "
            "WHERE name='quota_window_change_log'"
        ).fetchone()[0]
    finally:
        cache.close()

    with count_expanded_groups(quota) as combined:
        quota.reconcile_codex_quota_projection(
            now=due_at + dt.timedelta(seconds=1),
            alert_eligible_root_keys=roots,
            full_pass="defer",
        )
    assert combined.groups == 4, (
        f"the due+dirty pass expanded {combined.groups} groups instead of "
        "the two complete affected roots")
    assert {key[1] for key in combined.group_keys} == {ROOT_KEY, dirty_root}

    stats = ns["open_db"]()
    try:
        watermark = stats.execute(
            "SELECT watermark_seq FROM quota_projection_ledger_state "
            "WHERE source='codex'"
        ).fetchone()[0]
        dirty_current = stats.execute(
            "SELECT MAX(current_percent) FROM quota_window_blocks "
            "WHERE source_root_key=? AND orphaned_at IS NULL",
            (dirty_root,),
        ).fetchone()[0]
    finally:
        stats.close()
    assert watermark == ledger_high
    assert dirty_current == pytest.approx(6.25)

    cache = ns["open_cache_db"]()
    try:
        assert cache.execute(
            "SELECT COUNT(*) FROM quota_window_change_log WHERE seq<=?",
            (watermark,),
        ).fetchone()[0] == 0
        certificate = __import__("json").loads(cache.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            (quota._DASHBOARD_PROJECTION_CERTIFICATE_KEY,),
        ).fetchone()[0])
    finally:
        cache.close()
    assert set(certificate["signatures"]) == roots

    with count_expanded_groups(quota) as reporting:
        quota.reconcile_codex_quota_projection(
            now=due_at + dt.timedelta(seconds=2))
    assert reporting.calls == 0, (
        "the all-root certificate was incoherent after the scoped pass, so a "
        "reporting reconcile missed its O(1) short-circuit")


# ── the axes against `full_pass="defer"` (the hook path) ───────────────────

def _armed_store(tmp_path, monkeypatch):
    """A store with delivery on and one alert-eligible full pass already done.

    Four physical groups — three closed windows plus the live one — so "did the
    pass widen" is the difference between 4 expanded groups and 0.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(ns, closed_windows=3, observations_per_window=10)
    enable_quota_alerts(ns, actual=(2,))
    now = store_as_of(ns, before_reset=dt.timedelta(minutes=10))
    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys={ROOT_KEY})
    return ns, quota, now


@pytest.mark.parametrize("axis,expected_groups", [
    ("gate_enabled", 4),
    ("rule_changed", 4),
    ("scheduled", 0),
])
def test_which_alert_axes_widen_inline_on_the_hook_path(
    tmp_path, monkeypatch, axis, expected_groups,
):
    """The second carve-out, pinned — including what it does NOT cover.

    `full_pass="defer"` was documented as having two carve-outs "neither
    reachable from the hook", but every hook tick that clears the 15s lifecycle
    throttle passes a non-empty `alert_eligible_root_keys`, so the §3 widening
    is live on every one of them. Two of its three routes are config-driven and
    the carve-out argument holds: a rule change or a delivery-gate ENABLE has to
    SEE the blocks to write `suppressed_backfill` terminal rows rather than
    dispatch history, so deferring would break them rather than delay them.

    The third is wall-clock driven. A future-clocked capture (clock skew across
    a sleep/resume, an NTP correction) sets the boundary, and the first tick
    after wall time passes it ran a whole-history load and apply on the blocking
    path — the ~14-30s reconcile against Codex's 30-second kill that this whole
    change removes, arriving unannounced once. It is deferred instead.
    """
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    if axis == "gate_enabled":
        _stats_write(
            ns, "UPDATE quota_projection_ledger_state SET alerts_enabled=0 "
                " WHERE source='codex'")
    elif axis == "rule_changed":
        _stats_write(
            ns, "UPDATE quota_alert_arming SET rule_fingerprint='stale' "
                " WHERE source='codex'")
    else:
        _stats_write(
            ns, "UPDATE quota_projection_ledger_state "
                "   SET next_evaluation_at_utc=? WHERE source='codex'",
            (quota._utc_iso(now - dt.timedelta(hours=1)),))

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(
            now=now, alert_eligible_root_keys={ROOT_KEY}, full_pass="defer")

    assert counter.groups == expected_groups, (
        f"the {axis} axis expanded {counter.groups} groups under "
        f"full_pass='defer', expected {expected_groups}")


def test_a_widened_hook_pass_retires_the_axes_it_actually_evaluated(
    tmp_path, monkeypatch,
):
    """Axes 3 and 4 together, which is where "did not act on it" stops being
    the same question as "deferred it".

    `alert_dirty_scope` widens to `SCOPE_ALL` whenever `gate_before is not True`
    — the NULL a stats.db epoch rebuild leaves behind and the False an alerts
    disable leaves behind both qualify — so a hook tick in that state runs the
    whole-history load and apply INLINE. That is the accepted axis-3 carve-out,
    and it is only acceptable because it happens ONCE: the pass arms the rules,
    writes `suppressed_backfill`, and stamps the gate, so the next tick resolves
    a quiet scope.

    A matured boundary sitting on the same tick must not take that away. The
    pass genuinely looked at every observation of every active root, so it
    evaluated the matured instant as surely as a widening for axis 4 itself
    would have — refusing to retire either value leaves the identical scope
    standing for the next tick, and the whole-history pass repeats on every
    turn: the ~14-30 s reconcile against Codex's 30 s kill, forever, which is
    the defect this branch exists to remove.
    """
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    _stats_write(
        ns, "UPDATE quota_projection_ledger_state SET alerts_enabled=0, "
            "    next_evaluation_at_utc=? WHERE source='codex'",
        (quota._utc_iso(now - dt.timedelta(hours=1)),))

    with count_expanded_groups(quota) as first:
        quota.reconcile_codex_quota_projection(
            now=now, alert_eligible_root_keys={ROOT_KEY}, full_pass="defer")
    assert first.groups == 4, (
        f"the gate_enabled axis expanded {first.groups} of 4 groups, so this "
        f"tick never ran the whole-history pass the assertions below are about")

    gate_after_first = _stored_gate(ns)

    with count_expanded_groups(quota) as second:
        quota.reconcile_codex_quota_projection(
            now=now, alert_eligible_root_keys={ROOT_KEY}, full_pass="defer")

    assert second.groups == 0, (
        f"the following hook tick expanded {second.groups} groups instead of "
        f"staying bounded: the widened pass retired neither the gate nor the "
        f"boundary, so every subsequent tick resolves the same SCOPE_ALL and "
        f"runs the whole-history reconcile inline forever")
    assert gate_after_first == 1, (
        "the pass loaded and applied every group, armed the rules and wrote "
        "its terminal rows, then declined to stamp the gate it had just acted "
        "on — so the enable stayed owing to a pass that had already performed "
        "it")
    assert _stored_boundary(ns) is None, (
        "the widened pass kept a matured boundary whose every candidate "
        "observation it had just loaded and evaluated, which is the other half "
        "of the same standing scope")


def test_the_hook_keeps_a_boundary_it_declined_to_act_on(tmp_path, monkeypatch):
    """Deferring axis 4 is only honest if the instant survives the tick.

    A tick that declines the widening but still recomputes the boundary from
    its own bounded evidence drops the matured instant, and the axis never
    fires again — a silent loss wearing a deferral's clothes.
    """
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    stale = quota._utc_iso(now - dt.timedelta(hours=1))
    _stats_write(
        ns, "UPDATE quota_projection_ledger_state SET next_evaluation_at_utc=? "
            " WHERE source='codex'", (stale,))

    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys={ROOT_KEY}, full_pass="defer")

    assert _stored_boundary(ns) == stale, (
        "the deferring tick consumed the boundary it refused to evaluate, so "
        "the matured observation is never re-examined by anybody")


def test_a_reporting_only_pass_does_not_consume_a_matured_boundary(
    tmp_path, monkeypatch,
):
    """Axis 4 belongs to whoever does the alert work, and a reporting-only pass
    does none: `_evaluate_quota_alerts` returns at `if not alert_eligible_roots`
    long before any threshold is looked at. Advancing the boundary from such a
    pass retires the axis on behalf of an evaluation that never happened.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(ns, closed_windows=3, observations_per_window=10)
    enable_quota_alerts(ns, actual=(2,))
    early = store_as_of(ns, before_reset=dt.timedelta(hours=2))
    quota.reconcile_codex_quota_projection(
        now=early, alert_eligible_root_keys={ROOT_KEY})
    boundary = _stored_boundary(ns)
    assert boundary is not None, "the fixture recorded no boundary to retire"
    parsed = dt.datetime.fromisoformat(boundary.replace("Z", "+00:00"))

    # The `_codex-quota-verify` worker's shape: force_full, no eligible roots.
    quota.reconcile_codex_quota_projection(
        now=parsed + dt.timedelta(seconds=1), force_full=True)

    assert _stored_boundary(ns) == boundary, (
        "a reporting-only pass advanced the next-evaluation boundary past an "
        "instant it never evaluated, so the matured observation is skipped "
        "forever")


def test_a_deferred_route_leaves_the_alert_axes_for_the_next_tick(
    tmp_path, monkeypatch,
):
    """The whole hand-off, end to end, for axis 3.

    Every upgrading install is in exactly this state: the epoch bump rebuilds
    stats.db, so the hook's catch-all defers with no projection work at all,
    and the worker that repairs it runs `force_full` with NO alert-eligible
    roots. If either of them stamps `alerts_enabled`, the ENABLE is consumed by
    a pass that armed nothing — `gate_before` reads True from then on, the axis
    never re-fires, and the `suppressed_backfill` rows activation owes are never
    written.
    """
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    _stats_write(
        ns, "UPDATE quota_projection_ledger_state SET alerts_enabled=0 "
            " WHERE source='codex'")
    _stats_write(ns, "DELETE FROM quota_alert_arming WHERE source='codex'")
    _stats_write(ns, "DELETE FROM quota_threshold_events WHERE source='codex'")
    # ...on the catch-all route, which returns BEFORE the §3 widening.
    _stats_write(
        ns, "UPDATE quota_projection_ledger_state SET interpretation_version=9999"
            " WHERE source='codex'")

    import _cctally_update
    spawned = []
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached",
        lambda command: spawned.append(command) or True)

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(
            now=now, alert_eligible_root_keys={ROOT_KEY}, full_pass="defer")
    assert counter.groups == 0, "the catch-all ran a pass on the hook path"
    assert spawned == [quota.CODEX_QUOTA_VERIFY_COMMAND]
    assert _stored_gate(ns) == 0, "the deferring tick consumed the enable"

    assert quota.cmd_codex_quota_verify_internal(None) == 0
    assert _stored_gate(ns) == 0, (
        "the reporting-only worker consumed the delivery-gate ENABLE it did no "
        "alert work for — no arming row, no suppressed_backfill, and the axis "
        "cannot re-fire because gate_before is now True")

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(
            now=now, alert_eligible_root_keys={ROOT_KEY}, full_pass="defer")

    assert counter.groups == 4, "the recovered enable did not widen the pass"
    assert _arming_rows(ns) > 0
    assert _stored_gate(ns) == 1
    dispositions = _dispositions(ns)
    assert dispositions.get("suppressed_backfill", 0) > 0, (
        "activation wrote no terminal suppression for already-satisfied "
        "thresholds")
    assert dispositions.get("alerted", 0) == 0, (
        "the recovered activation DISPATCHED history instead of suppressing "
        "it — a late activation must never become an alert burst")


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
