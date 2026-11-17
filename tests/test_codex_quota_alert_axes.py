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
import json

import pytest

import _fixture_builders
import _lib_quota_alert_axes as axes
from conftest import load_script, redirect_paths
from helpers.quota_projection_dump import (
    ROOT_KEY,
    SOURCE_PATH,
    append_one_observation_to_live_window,
    build_codex_quota_store,
    count_expanded_groups,
    enable_quota_alerts,
    raw_load_recorder,
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

    # #1070 (spec §5.6, Q15): the owned scheduled evaluation is a
    # complete-root requirement, so the due hook records it for its owning
    # root only and the worker evaluates that root.
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path, keys=eligible)
    with count_expanded_groups(quota) as due:
        result = quota.reconcile_codex_quota_projection(
            now=first + dt.timedelta(seconds=1),
            alert_eligible_root_keys=eligible,
            full_pass="defer",
        )
    assert due.groups == 0, (
        f"the first due hook tick expanded {due.groups} groups; the scheduled "
        "evaluation belongs to the worker")
    assert result.alert_work_pending, result
    [request] = _requests()
    assert (request["reason"], request["roots"]) == ("scheduled", [ROOT_KEY])
    _run_worker(monkeypatch, first + dt.timedelta(seconds=1))
    assert _requests() == []
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

    # #1070 (spec §5.6): the due root's complete-root evaluation is deferred
    # to the worker as an obligation, while the dirty root's bounded physical
    # work still lands on this hook.
    _no_spawn(monkeypatch)
    with count_expanded_groups(quota) as combined:
        result = quota.reconcile_codex_quota_projection(
            now=due_at + dt.timedelta(seconds=1),
            alert_eligible_root_keys=roots,
            full_pass="defer",
        )
    assert {key[1] for key in combined.group_keys} == {dirty_root}, (
        f"the due+dirty hook expanded {combined.group_keys}: only the dirty "
        "root's unit is bounded hook work")
    assert result.alert_work_pending, result
    assert [(r["reason"], r["roots"]) for r in _requests()] == [
        ("scheduled", [ROOT_KEY])]

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


def test_a_completed_activation_retires_the_axes_it_evaluated(
    tmp_path, monkeypatch,
):
    """Axes 3 and 4 together, which is where "did not act on it" stops being
    the same question as "deferred it" (#1070, spec §5.6).

    `alert_dirty_scope` widens to `SCOPE_ALL` whenever `gate_before is not True`
    — the NULL a stats.db epoch rebuild leaves behind and the False an alerts
    disable leaves behind both qualify. On the hook that widening is a whole
    history the hook cannot afford (§4.2 M1: 32 s against Codex's 30 s kill),
    so the hook records an obligation and the `_codex-quota-verify` worker
    completes it. The worker genuinely looks at every observation of every
    owed root, so it must retire BOTH the gate and the matured boundary:
    retiring neither leaves the identical obligation standing, and every
    following hook would record it again forever. Detects an activation that
    completes without consuming the axes it evaluated.
    """
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    _stats_write(
        ns, "UPDATE quota_projection_ledger_state SET alerts_enabled=0, "
            "    next_evaluation_at_utc=? WHERE source='codex'",
        (quota._utc_iso(now - dt.timedelta(hours=1)),))

    with raw_load_recorder(quota) as hook:
        result = _hook(quota, now)
    assert hook.unbounded_calls == 0, "the hook loaded a complete history"
    assert result.alert_work_pending, result
    assert {r["reason"] for r in _requests()} == {
        "gate_enabled", "scheduled_legacy"}, _requests()

    _run_worker(monkeypatch, now + dt.timedelta(seconds=30))
    assert _requests() == [], "the completed activation kept its requests"
    assert _stored_gate(ns) == 1, (
        "the worker armed the rules and wrote its terminal rows, then declined "
        "to stamp the gate it had just acted on")
    assert _stored_boundary(ns) is None, (
        "the worker kept a matured boundary whose every candidate observation "
        "it had just loaded and evaluated")

    with raw_load_recorder(quota) as second:
        result = _hook(quota, now + dt.timedelta(minutes=1))
    assert second.unbounded_calls == 0
    assert not result.alert_work_pending, (
        "the hook after a completed activation recorded it again")
    assert _requests() == []


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

    The "nothing armed" state comes from a REAL disable and re-enable: the
    verify worker's ingest cycle replays the journal, which restores arming and
    terminal rows deleted straight from stats, so a hand-made empty state is
    back before the activation runs and its suppression check would pass on the
    restored rows. The re-enable adds a 5 % rule that every window's retained
    history (1-6 %) already satisfied and that no earlier activation ever
    claimed, so its `suppressed_backfill` rows, and the arming row's boundary at
    the hook's cutoff, can only be the worker's activation's own writes.
    """
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    _write_delivery_off()
    quota.reconcile_codex_quota_projection(now=now + dt.timedelta(minutes=1))
    assert _arming_rows(ns) == 0, "premise: the disable did not disarm"
    assert _stored_gate(ns) in (0, False), _stored_gate(ns)
    enable_quota_alerts(ns, actual=(2, 5))
    assert 5 not in _thresholds_claimed(ns), "premise: 5 % was already claimed"
    cutoff = now + dt.timedelta(minutes=2)
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
        result0 = quota.reconcile_codex_quota_projection(
            now=cutoff, alert_eligible_root_keys={ROOT_KEY}, full_pass="defer")
    assert counter.groups == 0, "the catch-all ran a pass on the hook path"
    # #1070 Q13: the enable it owes is pending, so the reconcile holds the
    # verify request; the hook makes it once its lifecycle locks are released.
    assert result0.alert_work_pending, result0
    assert spawned == []
    assert quota._defer_codex_quota_verification() == "spawned"
    assert spawned == [quota.CODEX_QUOTA_VERIFY_COMMAND]
    assert _stored_gate(ns) == 0, "the deferring tick consumed the enable"

    assert quota.cmd_codex_quota_verify_internal(None) == 0
    assert _stored_gate(ns) == 0, (
        "the reporting-only worker consumed the delivery-gate ENABLE it did no "
        "alert work for — no arming row, no suppressed_backfill, and the axis "
        "cannot re-fire because gate_before is now True")
    assert _arming_rows(ns) == 0, (
        "premise: the reporting worker's journal replay restored the disarmed "
        "identity")

    # #1070 (spec §5.6): the recovered enable is an obligation the hook
    # records and the worker, admitted through the real lifecycle locks,
    # completes — the hook itself never loads the whole history.
    _lifecycle_roots(monkeypatch, tmp_path)
    with raw_load_recorder(quota) as hook:
        result = quota.reconcile_codex_quota_projection(
            now=cutoff, alert_eligible_root_keys={ROOT_KEY}, full_pass="defer")
    assert hook.unbounded_calls == 0, "the hook ran the activation inline"
    assert result.alert_work_pending, result
    assert [r["reason"] for r in _requests()] == ["gate_enabled"]
    assert _stored_gate(ns) == 0, "the hook consumed the enable it deferred"

    _run_worker(monkeypatch, cutoff + dt.timedelta(seconds=30))
    assert _thresholds_claimed(ns).get(5) == {"suppressed_backfill"}, (
        "the worker's activation wrote no terminal suppression for the 5 % "
        f"threshold history had already satisfied: {_thresholds_claimed(ns)}")
    assert {row[6] for row in _arming_state(ns)} == {quota._utc_iso(cutoff)}, (
        "the arming rows are not the activation's own, at the hook's cutoff")
    assert _requests() == []
    assert _stored_gate(ns) == 1
    assert _dispositions(ns).get("alerted", 0) == 0, (
        "the recovered activation DISPATCHED history instead of suppressing "
        "it — a late activation must never become an alert burst")


def _thresholds_claimed(ns):
    """``{threshold: {disposition, ...}}`` over every Codex terminal event."""
    conn = ns["open_db"]()
    try:
        claimed: dict = {}
        for threshold, disposition in conn.execute(
                "SELECT threshold, disposition FROM quota_threshold_events "
                " WHERE source='codex'"):
            claimed.setdefault(int(threshold), set()).add(str(disposition))
        return claimed
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── #1070: a disabled-delivery disarm stores the gate it implies ───────────

_REENABLE_THRESHOLDS = (2, 25, 50)


def _write_delivery_off():
    import _cctally_core

    _cctally_core.CONFIG_PATH.write_text(json.dumps({"alerts": {
        "enabled": False, "notifier": "none",
        "quota": {"enabled": True,
                  "actual_thresholds": list(_REENABLE_THRESHOLDS),
                  "projected_thresholds": [], "rules": []}}}) + "\n")


def _append_live(ns, *, offset, captured_at, used_percent):
    """One observation in the live window, at an exact percent."""
    conn = ns["open_cache_db"]()
    try:
        row = conn.execute(
            "SELECT logical_limit_key, observed_slot, window_minutes, "
            "       resets_at_utc, "
            "       COALESCE(canonical_resets_at_utc, resets_at_utc) "
            "  FROM quota_window_snapshots WHERE source='codex' "
            " ORDER BY 5 DESC LIMIT 1").fetchone()
        _fixture_builders.seed_codex_quota_snapshot(
            conn, source_root_key=ROOT_KEY, source_path=SOURCE_PATH,
            line_offset=offset, captured_at_utc=quota_iso(captured_at),
            observed_slot=str(row[1]), logical_limit_key=str(row[0]),
            window_minutes=int(row[2]), used_percent=used_percent,
            resets_at_utc=str(row[3]), canonical_resets_at_utc=str(row[4]),
            plan_type="pro",
        )
        _fixture_builders.bump_codex_physical_mutation_seq(conn)
        conn.commit()
        return str(row[4])
    finally:
        conn.close()


def quota_iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _live_events(ns, live_reset):
    conn = ns["open_db"]()
    try:
        return {
            int(row[0]): str(row[1]) for row in conn.execute(
                "SELECT threshold, disposition FROM quota_threshold_events "
                " WHERE source='codex' "
                "   AND unixepoch(resets_at_utc)=unixepoch(?)", (live_reset,))
        }
    finally:
        conn.close()


@pytest.mark.parametrize("disabled_pass", ["reporting_only", "alert_eligible"])
def test_re_enabling_delivery_alerts_on_a_later_crossing(
    tmp_path, monkeypatch, disabled_pass,
):
    """Detects #1070: a reporting-only pass that disarmed while delivery was off
    left the stored gate ``True``, so re-enabling delivery never widened, the
    identity was re-armed as a first activation on the next eligible pass, and
    a genuine later crossing was recorded ``suppressed_backfill`` with no
    notification. The ``alert_eligible`` disabled pass is the control.

    History stays undispatched either way: a threshold satisfied during the
    disabled interval is written ``suppressed_backfill`` on the re-enable, and
    only the crossing after it alerts.

    The re-enable's activation needs the complete history, which the hook
    cannot afford (spec §4.2 M1), so it completes in the ``_codex-quota-verify``
    worker (§5.6): the hook records cutoff T, the 50 % crossing lands after T
    and before the worker, and the worker still alerts it — a crossing lost to
    the delayed activation is the second failure this test detects.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    jr = importlib.import_module("_cctally_journal")
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    build_codex_quota_store(ns, closed_windows=3, observations_per_window=10)
    enable_quota_alerts(ns, actual=_REENABLE_THRESHOLDS)
    now0 = store_as_of(ns, before_reset=dt.timedelta(minutes=20))
    eligible = {ROOT_KEY}

    # The live window peaks at about 6 %: the identity's first activation, an
    # ordinary inline (prime) pass.
    quota.reconcile_codex_quota_projection(
        now=now0, alert_eligible_root_keys=eligible)
    live_reset = _append_live(
        ns, offset=10_000, captured_at=now0 + dt.timedelta(minutes=1),
        used_percent=30.0)
    events = _live_events(ns, live_reset)
    assert events == {2: "suppressed_backfill"}, events
    assert dispatched == [], "premise: the first activation dispatches nothing"

    _write_delivery_off()
    if disabled_pass == "reporting_only":
        quota.reconcile_codex_quota_projection(
            now=now0 + dt.timedelta(minutes=2))
    else:
        quota.reconcile_codex_quota_projection(
            now=now0 + dt.timedelta(minutes=2),
            alert_eligible_root_keys=eligible, full_pass="defer")
    assert _arming_rows(ns) == 0, "the disabled pass did not disarm"
    assert 25 not in _live_events(ns, live_reset), "nothing claimed while off"
    assert _stored_gate(ns) in (0, False), (
        f"the {disabled_pass} pass disarmed but stored the delivery gate as "
        f"{_stored_gate(ns)!r}, so a later re-enable cannot widen")

    # #1070 (spec §5.6, §7): the eligible hook under "defer" records the
    # activation obligation with cutoff T, publishes only its bounded scope,
    # claims and dispatches nothing and is not acknowledged.
    enable_quota_alerts(ns, actual=_REENABLE_THRESHOLDS)
    cutoff = now0 + dt.timedelta(minutes=3)
    with raw_load_recorder(quota) as hook:
        result = quota.reconcile_codex_quota_projection(
            now=cutoff, alert_eligible_root_keys=eligible, full_pass="defer")
    assert _live_events(ns, live_reset) == {2: "suppressed_backfill"}, (
        "the re-enable hook evaluated thresholds inline instead of deferring "
        f"the activation: {_live_events(ns, live_reset)}")
    assert hook.unbounded_calls == 0, "the re-enable hook loaded whole history"
    assert result.status == "published" and result.alert_work_pending, result
    assert dispatched == [], "the re-enable dispatched history"
    assert _stored_gate(ns) in (0, False), "the hook consumed the deferred enable"
    [request] = _requests()
    assert request["reason"] == "gate_enabled" and request["roots"] == []
    assert _cutoff(request, ROOT_KEY) == cutoff

    # A real crossing captured after T and before the worker runs.
    _append_live(ns, offset=10_001,
                 captured_at=now0 + dt.timedelta(minutes=4), used_percent=60.0)
    _run_worker(monkeypatch, now0 + dt.timedelta(minutes=5))
    events = _live_events(ns, live_reset)
    assert events == {
        2: "suppressed_backfill", 25: "suppressed_backfill", 50: "alerted"}, (
        f"after the {disabled_pass} disabled pass the worker's activation "
        f"recorded {events}: the disabled-interval 25% crossing must be "
        "suppressed as of the cutoff and the 50% crossing after it alerted")
    assert [alert.get("threshold") for alert in dispatched] == [50], dispatched
    assert _stored_gate(ns) in (1, True)
    assert _requests() == [], "the completed activation kept its request"
    assert "result=success" in _worker_lines(ns)[-1], _worker_lines(ns)

    with raw_load_recorder(quota) as later:
        result = quota.reconcile_codex_quota_projection(
            now=now0 + dt.timedelta(minutes=6),
            alert_eligible_root_keys=eligible, full_pass="defer")
    assert later.unbounded_calls == 0, "a later ordinary hook was not bounded"
    assert not result.alert_work_pending, result
    assert len(dispatched) == 1, dispatched


def test_a_config_flip_between_preparation_and_apply_stays_consistent(
    tmp_path, monkeypatch,
):
    """Detects a split delivery decision: the reconcile read the alert config
    for its scope and `_evaluate_quota_alerts` read it again, so a config
    change landing between the two stored a gate, arming rows and claims from
    two different configurations. Everything must follow the FIRST read."""
    ns, quota = _load(tmp_path, monkeypatch)
    jr = importlib.import_module("_cctally_journal")
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    build_codex_quota_store(ns, closed_windows=3, observations_per_window=10)
    enable_quota_alerts(ns, actual=_REENABLE_THRESHOLDS)
    now0 = store_as_of(ns, before_reset=dt.timedelta(minutes=20))
    quota.reconcile_codex_quota_projection(
        now=now0, alert_eligible_root_keys={ROOT_KEY})
    armed_before = _arming_rows(ns)
    assert armed_before, "premise: the identity is armed"
    live_reset = _append_live(
        ns, offset=10_000, captured_at=now0 + dt.timedelta(minutes=1),
        used_percent=30.0)

    # The first read sees delivery ON; every later read sees it OFF.
    original = quota._quota_alert_config
    calls = {"n": 0}

    def flipping():
        calls["n"] += 1
        if calls["n"] > 1:
            _write_delivery_off()
        return original()

    monkeypatch.setattr(quota, "_quota_alert_config", flipping)
    quota.reconcile_codex_quota_projection(
        now=now0 + dt.timedelta(minutes=2),
        alert_eligible_root_keys={ROOT_KEY}, full_pass="defer")
    monkeypatch.setattr(quota, "_quota_alert_config", original)

    assert _stored_gate(ns) in (1, True), _stored_gate(ns)
    assert _arming_rows(ns) == armed_before, (
        "the apply disarmed under a config the pass never prepared with")
    assert _live_events(ns, live_reset).get(25) == "alerted", (
        _live_events(ns, live_reset))
    assert [alert.get("threshold") for alert in dispatched] == [25], dispatched


def test_a_rebuild_leaves_the_delivery_gate_null(tmp_path, monkeypatch):
    """Preservation: the rebuild re-materialization has no disarm emitter, so
    the stored gate it writes stays NULL (the fail-safe that makes the next
    eligible pass widen) even with delivery disabled."""
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(ns, closed_windows=2, observations_per_window=5)
    _write_delivery_off()
    now = store_as_of(ns, before_reset=dt.timedelta(minutes=20))
    stats = ns["open_db"]()
    try:
        stats.execute("BEGIN IMMEDIATE")
        quota.rematerialize_quota_projection_for_rebuild(stats, now=now)
        stats.commit()
    finally:
        stats.close()
    assert _stored_gate(ns) is None
    assert _arming_rows(ns) == 0



# ── #1070 T11: the deferred alert activation (spec §5.6, Q11–Q16) ──────────

_SECOND_ROOT = "root-second"


def _sidecar_path():
    import _cctally_core

    return _cctally_core.APP_DIR / "codex-quota-alert-obligations.json"


def _requests():
    """The sidecar's pending requests, or ``None`` when it does not exist."""
    path = _sidecar_path()
    if not path.exists():
        return None
    return json.loads(path.read_text())["requests"]


def _cutoff(request, root_key):
    return dt.datetime.fromisoformat(request["cutoffs"][root_key])


def _no_spawn(monkeypatch):
    import _cctally_update

    spawned: list = []
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached",
        lambda command: spawned.append(command) or True)
    return spawned


def _lifecycle_roots(monkeypatch, tmp_path, keys=(ROOT_KEY,)):
    """Resolve the worker's configured Codex roots to the store's synthetic
    root keys, each with a real home directory for its lifecycle lock."""
    import _cctally_record
    from _lib_codex_hooks import CodexHookRoot

    roots = []
    for key in sorted(keys):
        home = tmp_path / f"codex-home-{key}"
        home.mkdir(parents=True, exist_ok=True)
        roots.append(CodexHookRoot(key, home, home / "hooks.json"))
    monkeypatch.setattr(
        _cctally_record, "_codex_lifecycle_roots", lambda: list(roots))
    return roots


def _run_worker(monkeypatch, now):
    import _cctally_quota

    monkeypatch.setenv("CCTALLY_AS_OF", now.isoformat())
    try:
        assert _cctally_quota.cmd_codex_quota_verify_internal(None) == 0
    finally:
        monkeypatch.delenv("CCTALLY_AS_OF", raising=False)


def _worker_lines(ns):
    path = ns["APP_DIR"] / "logs" / "hook-tick.log"
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines()
            if " op=quota-verify " in line]


def _hook(quota, now, roots=(ROOT_KEY,)):
    return quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys=set(roots), full_pass="defer")


def _arming_state(ns):
    conn = ns["open_db"]()
    try:
        return sorted(tuple(row) for row in conn.execute(
            "SELECT source_root_key, account_key, logical_limit_key, "
            "       observed_slot, window_minutes, rule_fingerprint, "
            "       activated_at_utc FROM quota_alert_arming "
            " WHERE source='codex'"))
    finally:
        conn.close()


def _ledger_row(ns):
    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT watermark_seq, alerts_enabled, next_evaluation_at_utc, "
            "       next_evaluation_by_root_json, last_full_pass_at "
            "  FROM quota_projection_ledger_state WHERE source='codex'"
        ).fetchone()
        return None if row is None else tuple(row)
    finally:
        conn.close()


def _generations(ns):
    conn = ns["open_db"]()
    try:
        return sorted(tuple(row) for row in conn.execute(
            "SELECT source_root_key, generation FROM quota_projection_state"))
    finally:
        conn.close()


def _set_axis(ns, quota, now, axis):
    past = quota._utc_iso(now - dt.timedelta(hours=1))
    if axis in ("gate_enabled", "in_lock"):
        _stats_write(
            ns, "UPDATE quota_projection_ledger_state SET alerts_enabled=0 "
                " WHERE source='codex'")
    elif axis == "rebuild_null_gate":
        _stats_write(
            ns, "UPDATE quota_projection_ledger_state SET alerts_enabled=NULL "
                " WHERE source='codex'")
    elif axis == "rule_changed":
        # A real rule replacement: the armed identities' stored fingerprints
        # no longer match the resolved rule. (A direct stats write of the
        # fingerprint is not journalled state: observed, the leg's in-lock
        # re-preparation read the original fingerprint and derived no rule
        # change, so that fixture tested nothing.)
        enable_quota_alerts(ns, actual=(2, 3))
    elif axis == "scheduled_owned":
        _stats_write(
            ns, "UPDATE quota_projection_ledger_state "
                "   SET next_evaluation_at_utc=?, "
                "       next_evaluation_by_root_json=? WHERE source='codex'",
            (past, json.dumps({ROOT_KEY: past})))
    elif axis == "scheduled_legacy":
        _stats_write(
            ns, "UPDATE quota_projection_ledger_state "
                "   SET next_evaluation_at_utc=?, "
                "       next_evaluation_by_root_json='{}' WHERE source='codex'",
            (past,))
    else:  # pragma: no cover - parameter typo
        raise AssertionError(axis)


_EXPECTED_REQUEST = {
    "gate_enabled": ("gate_enabled", []),
    "in_lock": ("gate_enabled", []),
    "rebuild_null_gate": ("gate_enabled", []),
    "rule_changed": ("rule_changed", [ROOT_KEY]),
    "scheduled_owned": ("scheduled", [ROOT_KEY]),
    "scheduled_legacy": ("scheduled_legacy", []),
}


@pytest.mark.parametrize("axis", sorted(_EXPECTED_REQUEST))
def test_a_defer_hook_never_loads_a_complete_history_for_alert_work(
    tmp_path, monkeypatch, axis,
):
    """Detects a non-converging activation on the hook (#1070, spec §4.2 M1):
    a first enable, a rule change, a rebuild's NULL gate and an owned or
    legacy scheduled evaluation each need complete root histories, which took
    32 s on a production-shaped store against Codex's 30 s hook kill, so the
    killed pass committed nothing and the next hook repeated it. Under
    ``"defer"`` neither the optimistic preparation nor the in-lock
    re-preparation (``in_lock``: a publication interleaved before the lock)
    may load a complete history for that work: the hook publishes its bounded
    scope, leaves every alert axis as stored, records a durable request with
    its own ``now`` as the cutoff and dispatches nothing."""
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    jr = importlib.import_module("_cctally_journal")
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _no_spawn(monkeypatch)
    _set_axis(ns, quota, now, axis)
    _append_live(ns, offset=20_000, captured_at=now - dt.timedelta(minutes=1),
                 used_percent=1.5)
    def alert_axes():
        return (_arming_state(ns), _stored_gate(ns), _stored_boundary(ns),
                _stored_schedule(ns), _dispositions(ns))

    before = alert_axes()

    preparations: list = []
    original_prepare = quota._prepare_codex_quota_pass

    def recording_prepare(**kwargs):
        preparations.append(bool(kwargs["in_lock"]))
        return original_prepare(**kwargs)

    monkeypatch.setattr(quota, "_prepare_codex_quota_pass", recording_prepare)
    if axis == "in_lock":
        original_ingest = jr.run_stats_ingest
        fired = {"done": False}

        def interleaving(*args, **kwargs):
            if kwargs.get("codex_apply") is not None and not fired["done"]:
                fired["done"] = True
                _append_live(
                    ns, offset=20_001,
                    captured_at=now - dt.timedelta(seconds=30),
                    used_percent=1.6)
                quota.reconcile_codex_quota_projection(now=now)  # reporting H
            return original_ingest(*args, **kwargs)

        monkeypatch.setattr(jr, "run_stats_ingest", interleaving)

    with raw_load_recorder(quota) as loads:
        result = _hook(quota, now)

    if axis == "in_lock":
        assert True in preparations, (
            f"premise: the leg did not re-prepare in the lock ({preparations})")
    assert loads.unbounded_calls == 0, (
        f"the {axis} hook loaded a complete history "
        f"({loads.unbounded_calls} unbounded loads)")
    assert result.status == "published", result
    assert result.alert_work_pending, result
    assert alert_axes() == before, (
        "the hook evaluated or consumed alert work it deferred")
    assert dispatched == []
    [request] = _requests()
    assert (request["reason"], request["roots"]) == _EXPECTED_REQUEST[axis]
    assert _cutoff(request, ROOT_KEY) == now


def test_repeated_hooks_and_a_throttled_spawn_never_move_the_cutoff(
    tmp_path, monkeypatch,
):
    """Detects a cutoff that drifts with each request (Q12, Q16): a hook that
    observes a pending obligation requests service again, but merging the
    request must neither move its cutoff nor refresh the spawn attempt, or a
    crossing between the first hook and the worker turns into backfill."""
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    spawned = _no_spawn(monkeypatch)
    _set_axis(ns, quota, now, "gate_enabled")
    _hook(quota, now)
    [first] = _requests()
    assert quota._defer_codex_quota_verification() == "spawned"
    for minutes in (1, 2):
        result = _hook(quota, now + dt.timedelta(minutes=minutes))
        assert result.alert_work_pending, result
        assert quota._defer_codex_quota_verification() == "throttled"
    assert _requests() == [first]
    assert spawned == [quota.CODEX_QUOTA_VERIFY_COMMAND]


def test_a_failed_worker_keeps_the_request_and_its_retry_completes(
    tmp_path, monkeypatch,
):
    """Detects an obligation retired by a worker that failed (Q11): only a
    successful eligible evaluation may remove the request, and the retry
    completes the activation at the original cutoff."""
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    _set_axis(ns, quota, now, "gate_enabled")
    _hook(quota, now)
    [first] = _requests()

    original = quota._apply_quota_projection_rows

    def failing(*args, **kwargs):
        raise RuntimeError("activation boom")

    monkeypatch.setattr(quota, "_apply_quota_projection_rows", failing)
    _run_worker(monkeypatch, now + dt.timedelta(minutes=1))
    monkeypatch.setattr(quota, "_apply_quota_projection_rows", original)
    line = _worker_lines(ns)[-1]
    assert "result=error" in line and "projection=alert_activation_failed" in line
    assert _requests() == [first]
    assert _stored_gate(ns) == 0

    _run_worker(monkeypatch, now + dt.timedelta(minutes=2))
    assert "result=success" in _worker_lines(ns)[-1], _worker_lines(ns)
    assert _requests() == []
    assert _stored_gate(ns) == 1


@pytest.mark.parametrize("change", ["disable", "re_enable", "rule_replacement"])
def test_a_pending_request_follows_its_delivery_generation(
    tmp_path, monkeypatch, change,
):
    """Detects a request that outlives its generation (Q12): a live disable
    cancels it (its cutoff belongs to a generation that ended), a later
    re-enable records a new one at its own cutoff, and a rule replacement
    supersedes it with a new cutoff rather than borrowing the old config's."""
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    _no_spawn(monkeypatch)
    _set_axis(ns, quota, now, "gate_enabled")
    _hook(quota, now)
    [first] = _requests()
    later = now + dt.timedelta(minutes=2)
    if change in ("disable", "re_enable"):
        _write_delivery_off()
        quota.reconcile_codex_quota_projection(
            now=now + dt.timedelta(minutes=1))
        assert _requests() == [], "the disarm left the pending request"
        if change == "disable":
            return
        enable_quota_alerts(ns, actual=(2,))
    else:
        enable_quota_alerts(ns, actual=(2, 40))
    _hook(quota, later)
    # A rule replacement over armed identities also owes their rule change;
    # the delivery generation is carried by the gate request.
    [second] = [r for r in _requests() if r["reason"] == "gate_enabled"]
    assert second["id"] != first["id"]
    assert second["reason"] == "gate_enabled"
    assert _cutoff(second, ROOT_KEY) == later
    if change == "rule_replacement":
        assert second["config_fingerprint"] != first["config_fingerprint"]


def test_a_stats_rebuild_keeps_the_pending_request(tmp_path, monkeypatch):
    """Detects an obligation lost to a rebuild (Q12): the request lives
    outside stats.db, so a stats rebuild preserves it byte for byte and the
    worker still completes it."""
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    jr = importlib.import_module("_cctally_journal")
    _set_axis(ns, quota, now, "gate_enabled")
    _hook(quota, now)
    raw = _sidecar_path().read_bytes()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    assert _sidecar_path().read_bytes() == raw
    _run_worker(monkeypatch, now + dt.timedelta(minutes=1))
    assert _requests() == []
    assert _stored_gate(ns) == 1


def _two_root_armed_store(tmp_path, monkeypatch):
    ns, quota = _load(tmp_path, monkeypatch)
    for root_key in (ROOT_KEY, _SECOND_ROOT):
        build_codex_quota_store(
            ns, closed_windows=2, observations_per_window=5, root_key=root_key)
    enable_quota_alerts(ns, actual=(2,))
    now = store_as_of(ns, before_reset=dt.timedelta(minutes=10))
    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys={ROOT_KEY, _SECOND_ROOT})
    return ns, quota, now


def test_a_partial_lifecycle_lock_acquisition_leaves_the_obligation(
    tmp_path, monkeypatch,
):
    """Detects a global activation completed over part of its roots (Q14):
    the worker needs every owed root's real lifecycle lock; missing one, it
    releases everything, leaves the request pending and logs
    ``alert_lifecycle_contended``."""
    import fcntl
    import os

    ns, quota, now = _two_root_armed_store(tmp_path, monkeypatch)
    _no_spawn(monkeypatch)
    roots = _lifecycle_roots(monkeypatch, tmp_path, keys=(ROOT_KEY, _SECOND_ROOT))
    _set_axis(ns, quota, now, "gate_enabled")
    _hook(quota, now, roots=(ROOT_KEY, _SECOND_ROOT))
    [first] = _requests()

    lock_dir = ns["APP_DIR"] / "codex-hook-tick"
    lock_dir.mkdir(parents=True, exist_ok=True)
    held = os.open(lock_dir / f"{_SECOND_ROOT}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        _run_worker(monkeypatch, now + dt.timedelta(minutes=1))
        probe = os.open(lock_dir / f"{ROOT_KEY}.lock", os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released
            fcntl.flock(probe, fcntl.LOCK_UN)
        finally:
            os.close(probe)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    line = _worker_lines(ns)[-1]
    assert "result=error" in line and "projection=alert_lifecycle_contended" in line
    assert _requests() == [first]
    assert _stored_gate(ns) == 0
    assert roots  # both roots configured

    _run_worker(monkeypatch, now + dt.timedelta(minutes=2))
    assert _requests() == []
    assert _stored_gate(ns) == 1


def _delayed_crossing_store(tmp_path, monkeypatch, *, thresholds):
    """Delivery turned off and on again, the live window at 30 %: the next
    eligible hook is a first activation the worker completes.

    The disable is a real one — a live reconcile under delivery off journals
    the disarm and stores the false gate. Deleting the arming rows from
    stats.db directly is not durable state: observed, a later ingest cycle
    restored the journalled boundary, which turned the activation into an
    old one and dispatched history."""
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(ns, closed_windows=3, observations_per_window=10)
    enable_quota_alerts(ns, actual=thresholds)
    now0 = store_as_of(ns, before_reset=dt.timedelta(minutes=20))
    quota.reconcile_codex_quota_projection(
        now=now0, alert_eligible_root_keys={ROOT_KEY})
    live_reset = _append_live(
        ns, offset=10_000, captured_at=now0 + dt.timedelta(minutes=1),
        used_percent=30.0)
    _write_delivery_off()
    quota.reconcile_codex_quota_projection(
        now=now0 + dt.timedelta(seconds=90))
    assert _arming_state(ns) == [] and _stored_gate(ns) == 0, (
        "premise: the disable did not disarm")
    enable_quota_alerts(ns, actual=thresholds)
    return ns, quota, now0, live_reset


def test_a_superseded_request_activates_at_its_first_eligible_observer(
    tmp_path, monkeypatch,
):
    """Detects an activation that borrows a superseded generation's cutoff
    (Q12): the rules changed after the hook recorded T1, so the worker — the
    first eligible observer of the new configuration — supersedes the request
    at its own ``now`` T2, and a crossing between T1 and T2 of the NEW rule is
    history, not an alert."""
    ns, quota, now0, live_reset = _delayed_crossing_store(
        tmp_path, monkeypatch, thresholds=(2,))
    jr = importlib.import_module("_cctally_journal")
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    t1 = now0 + dt.timedelta(minutes=2)
    _hook(quota, t1)
    [first] = _requests()
    _append_live(ns, offset=10_001, captured_at=t1 + dt.timedelta(minutes=1),
                 used_percent=40.0)
    enable_quota_alerts(ns, actual=(2, 35))
    t2 = t1 + dt.timedelta(minutes=2)
    _run_worker(monkeypatch, t2)
    assert _live_events(ns, live_reset) == {
        2: "suppressed_backfill", 35: "suppressed_backfill"}, (
        _live_events(ns, live_reset))
    assert dispatched == [], "the superseded activation dispatched history"
    assert {row[6] for row in _arming_state(ns)} == {quota._utc_iso(t2)}
    assert _requests() == []
    assert first["id"]


#: A request the NEWER configuration recorded, and a leg prepared under the
#: older one half a second earlier. The two spellings are what the writers
#: produce, and their text order is the reverse of their time order
#: (``Z`` sorts after ``.``), so only a parsed comparison sees the leg is older.
_STALE_NOW = "2026-07-01T12:00:00Z"
_NEWER_CREATED = "2026-07-01T12:00:00.500000+00:00"
_NEWER_REQUEST = {
    "id": "a" * 32, "reason": "gate_enabled", "config_fingerprint": "newer",
    "roots": [], "cutoffs": {ROOT_KEY: _NEWER_CREATED},
    "created_at": _NEWER_CREATED,
}


def test_a_stale_leg_never_supersedes_a_newer_generations_request():
    """Detects a cutoff moved past the first validated hand-off (Q5, spec
    §5.6): a leg prepared before a configuration change merges after a hook
    already recorded the new configuration's request. Superseding it there
    minted a new id under the OLD fingerprint with the stale ``now`` as every
    cutoff, and the next hook superseded that again with its own later
    ``now``, so crossings between the first hand-off and that hook became
    history. The newer request must come back exactly as recorded."""
    quota = importlib.import_module("_cctally_quota")
    merged, changed = quota._merge_codex_alert_obligations(
        [dict(_NEWER_REQUEST)], derived=(("gate_enabled", frozenset()),),
        fingerprint="older", observed_roots={ROOT_KEY}, now_iso=_STALE_NOW)
    assert (merged, changed) == ([_NEWER_REQUEST], False), merged

    # The control: a leg prepared AFTER the request still supersedes it.
    later = "2026-07-01T12:00:01+00:00"
    merged, changed = quota._merge_codex_alert_obligations(
        [dict(_NEWER_REQUEST)], derived=(("gate_enabled", frozenset()),),
        fingerprint="newest", observed_roots={ROOT_KEY}, now_iso=later)
    [superseded] = merged
    assert changed and superseded["id"] != _NEWER_REQUEST["id"], merged
    assert superseded["cutoffs"] == {ROOT_KEY: later}, merged


def test_a_stale_activation_leg_completes_no_newer_request(
    tmp_path, monkeypatch,
):
    """Detects a newer generation's request retired by a stale activation: a
    worker leg prepared under the older configuration evaluated the owed root
    under the OLD rules at the newer request's cutoff and removed the request,
    so the next hook derived a rule change and recorded it at a later cutoff.
    The stale leg evaluates nothing it owes and completes nothing."""
    _ns, quota = _load(tmp_path, monkeypatch)
    quota._write_codex_alert_obligations([dict(_NEWER_REQUEST)])
    config = (True, True, (), {"actual_thresholds": [2]})
    final = type("Final", (), dict(
        alert_eligible_roots=frozenset({ROOT_KEY}), consume_alert_axes=True,
        schedule_evaluated_roots=frozenset(), alert_config=config,
        active_roots=frozenset({ROOT_KEY}),
        alert_obligations=(("gate_enabled", frozenset()),)))()
    assert quota._codex_alert_config_fingerprint(config) != "newer"

    plan = quota._plan_codex_alert_leg(
        final, activation_roots=frozenset({ROOT_KEY}), now_iso=_STALE_NOW)

    assert plan.completing_ids == frozenset(), plan
    assert ROOT_KEY not in plan.evaluate_roots, plan
    assert plan.pending and not plan.consume_alert_axes, plan
    assert _requests() == [_NEWER_REQUEST]


def test_a_crash_after_the_arming_emission_completes_its_suppressions_on_retry(
    tmp_path, monkeypatch,
):
    """Detects a retry that treats a replayed boundary as an old activation
    (Q12): the worker journaled the arming row at cutoff T and crashed before
    the historical suppressions; replay restores the row with a matching
    fingerprint, and the retry must still suppress what was satisfied at or
    before T and alert only what came after."""
    ns, quota, now0, live_reset = _delayed_crossing_store(
        tmp_path, monkeypatch, thresholds=_REENABLE_THRESHOLDS)
    jr = importlib.import_module("_cctally_journal")
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    cutoff = now0 + dt.timedelta(minutes=2)
    _hook(quota, cutoff)
    _append_live(ns, offset=10_001, captured_at=cutoff + dt.timedelta(minutes=1),
                 used_percent=60.0)

    original = quota._insert_quota_terminal_event

    def crashing(*args, **kwargs):
        raise RuntimeError("crash after arming")

    monkeypatch.setattr(quota, "_insert_quota_terminal_event", crashing)
    _run_worker(monkeypatch, cutoff + dt.timedelta(minutes=2))
    monkeypatch.setattr(quota, "_insert_quota_terminal_event", original)
    assert _requests(), "the crashed activation removed its request"
    jr.run_stats_ingest(mode="authoritative")  # replay the journaled arming
    assert quota._utc_iso(cutoff) in {row[6] for row in _arming_state(ns)}, (
        "premise: replay did not restore the arming boundary at the cutoff")
    assert 25 not in _live_events(ns, live_reset)

    _run_worker(monkeypatch, cutoff + dt.timedelta(minutes=3))
    assert _live_events(ns, live_reset) == {
        2: "suppressed_backfill", 25: "suppressed_backfill", 50: "alerted"}, (
        _live_events(ns, live_reset))
    assert [alert.get("threshold") for alert in dispatched] == [50], dispatched
    assert _requests() == []


@pytest.mark.parametrize("kind", ["owned", "legacy"])
def test_scheduled_work_completes_without_resetting_activation_time(
    tmp_path, monkeypatch, kind,
):
    """Detects a scheduled evaluation treated as a new activation (Q15): it
    evaluates armed rules, so the worker keeps every existing activation
    boundary while it retires the due boundary it evaluated."""
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    armed = _arming_state(ns)
    _set_axis(ns, quota, now, f"scheduled_{kind}")
    _hook(quota, now)
    assert [r["reason"] for r in _requests()] == [
        "scheduled" if kind == "owned" else "scheduled_legacy"]
    _run_worker(monkeypatch, now + dt.timedelta(minutes=1))
    assert _requests() == []
    assert _arming_state(ns) == armed, "the scheduled work reset activation time"
    assert _stored_boundary(ns) is None
    assert _stored_schedule(ns) == {}


@pytest.mark.parametrize("point", ["write", "replace", "dir_fsync"])
def test_a_failed_durable_recording_withholds_the_pass(
    tmp_path, monkeypatch, point,
):
    """Detects a hand-off lost to a crash (Appendix B round 4, P2): the hook
    must not consume the physical dirtiness unless the request carrying its
    cutoff is durable, so a failed write, replace or directory fsync withholds
    the whole pass — nothing applied, ``unavailable`` with
    ``alert_work_pending``."""
    import os

    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    _no_spawn(monkeypatch)
    jr = importlib.import_module("_cctally_journal")
    _set_axis(ns, quota, now, "gate_enabled")
    _append_live(ns, offset=20_000, captured_at=now - dt.timedelta(minutes=1),
                 used_percent=1.5)
    before = (_ledger_row(ns), _generations(ns))
    name = _sidecar_path().name
    sidecar_fds: set = set()
    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace
    real_fsync_dir = jr._fsync_dir

    def tracking_open(target, flags, *args, **kwargs):
        fd = real_open(target, flags, *args, **kwargs)
        if name in os.fspath(target):
            sidecar_fds.add(fd)
        return fd

    def failing_fsync(fd):
        if point == "write" and fd in sidecar_fds:
            raise OSError("fsync boom")
        return real_fsync(fd)

    def failing_replace(src, dst, *args, **kwargs):
        if point == "replace" and os.fspath(dst).endswith(name):
            raise OSError("replace boom")
        return real_replace(src, dst, *args, **kwargs)

    def failing_fsync_dir(target):
        if point == "dir_fsync" and str(target) == str(_sidecar_path().parent):
            raise OSError("dir fsync boom")
        return real_fsync_dir(target)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    monkeypatch.setattr(os, "replace", failing_replace)
    monkeypatch.setattr(jr, "_fsync_dir", failing_fsync_dir)
    result = _hook(quota, now)
    monkeypatch.setattr(os, "open", real_open)
    monkeypatch.setattr(os, "fsync", real_fsync)
    monkeypatch.setattr(os, "replace", real_replace)
    monkeypatch.setattr(jr, "_fsync_dir", real_fsync_dir)

    assert result.status == "unavailable" and result.alert_work_pending, result
    assert (_ledger_row(ns), _generations(ns)) == before, (
        "the pass applied although its obligation was not durably recorded")
    leftovers = [p.name for p in _sidecar_path().parent.iterdir()
                 if p.name.startswith(f".{name}")]
    assert leftovers == [], leftovers


def _damage(kind):
    raw = (b"{not json" if kind == "unreadable"
           else json.dumps({"version": 2, "requests": []}).encode())
    _sidecar_path().parent.mkdir(parents=True, exist_ok=True)
    _sidecar_path().write_bytes(raw)
    return raw


@pytest.mark.parametrize("kind", ["unreadable", "unsupported"])
def test_a_damaged_sidecar_is_preserved_and_keeps_every_pass_reporting_only(
    tmp_path, monkeypatch, kind,
):
    """Detects a damaged sidecar replaced with a later cutoff or ignored
    (Appendix B round 4, P1): stats cannot prove its contents held no
    obligation, so every eligible hook and worker pass is reporting-only for
    alerts (no threshold evaluated, nothing dispatched), still publishes its
    physical scope, reports pending work, and leaves the bytes untouched; only
    a live disarm moves the file aside."""
    ns, quota = _load(tmp_path, monkeypatch)
    jr = importlib.import_module("_cctally_journal")
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    build_codex_quota_store(ns, closed_windows=2, observations_per_window=10)
    enable_quota_alerts(ns, actual=_REENABLE_THRESHOLDS)
    now0 = store_as_of(ns, before_reset=dt.timedelta(minutes=20))
    quota.reconcile_codex_quota_projection(
        now=now0, alert_eligible_root_keys={ROOT_KEY})
    live_reset = _append_live(
        ns, offset=10_000, captured_at=now0 + dt.timedelta(minutes=1),
        used_percent=30.0)
    raw = _damage(kind)

    result = _hook(quota, now0 + dt.timedelta(minutes=2))
    assert result.status == "published" and result.alert_work_pending, result
    assert 25 not in _live_events(ns, live_reset), (
        "an eligible hook evaluated thresholds over a damaged sidecar")
    _run_worker(monkeypatch, now0 + dt.timedelta(minutes=3))
    line = _worker_lines(ns)[-1]
    assert "result=error" in line and (
        "projection=alert_obligations_unreadable" in line), line
    assert 25 not in _live_events(ns, live_reset)
    assert dispatched == []
    assert _sidecar_path().read_bytes() == raw, "the damaged sidecar was rewritten"

    _write_delivery_off()
    quota.reconcile_codex_quota_projection(now=now0 + dt.timedelta(minutes=4))
    assert _requests() == [], "the disarm did not end the damaged generation"
    aside = [p for p in _sidecar_path().parent.iterdir()
             if p.name.startswith(_sidecar_path().name + ".unreadable-")]
    assert [p.read_bytes() for p in aside] == [raw], aside


def test_a_damaged_sidecar_keeps_a_replayed_rule_change_from_dispatching_history(
    tmp_path, monkeypatch,
):
    """The round-4 verification regression: delivery on, a rule change adds
    50 % while retained usage is 60 %, the worker journals the new fingerprint
    and boundary T and crashes before the historical suppression, replay
    restores the arming row, and the sidecar is damaged. Stats alone now look
    clear (gate True, fingerprints match, nothing due), so a hook that trusted
    them would alert 50 % from history. It must evaluate nothing."""
    ns, quota = _load(tmp_path, monkeypatch)
    jr = importlib.import_module("_cctally_journal")
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    build_codex_quota_store(ns, closed_windows=2, observations_per_window=10)
    enable_quota_alerts(ns, actual=(2, 25))
    now0 = store_as_of(ns, before_reset=dt.timedelta(minutes=20))
    live_reset = _append_live(
        ns, offset=10_000, captured_at=now0 - dt.timedelta(minutes=1),
        used_percent=60.0)
    quota.reconcile_codex_quota_projection(
        now=now0, alert_eligible_root_keys={ROOT_KEY})
    assert _live_events(ns, live_reset) == {
        2: "suppressed_backfill", 25: "suppressed_backfill"}

    enable_quota_alerts(ns, actual=_REENABLE_THRESHOLDS)
    cutoff = now0 + dt.timedelta(minutes=1)
    _hook(quota, cutoff)
    assert [r["reason"] for r in _requests()] == ["rule_changed"]
    original = quota._insert_quota_terminal_event

    def crashing(*args, **kwargs):
        raise RuntimeError("crash after arming")

    monkeypatch.setattr(quota, "_insert_quota_terminal_event", crashing)
    _run_worker(monkeypatch, cutoff + dt.timedelta(seconds=30))
    monkeypatch.setattr(quota, "_insert_quota_terminal_event", original)
    jr.run_stats_ingest(mode="authoritative")
    assert quota._utc_iso(cutoff) in {row[6] for row in _arming_state(ns)}, (
        "premise: replay did not restore the new boundary")
    _damage("unreadable")

    _append_live(ns, offset=10_001, captured_at=cutoff + dt.timedelta(minutes=1),
                 used_percent=60.0)
    result = _hook(quota, cutoff + dt.timedelta(minutes=2))
    assert result.alert_work_pending, result
    assert dispatched == [], "history was dispatched over a damaged sidecar"
    assert 50 not in _live_events(ns, live_reset), _live_events(ns, live_reset)


def test_a_damaged_sidecar_runs_the_whole_history_pass_only_when_due(
    tmp_path, monkeypatch,
):
    """Detects a worker pinned to whole-history passes by a damaged sidecar:
    the alert work stays pending until a disarm, so every hook requests the
    worker (throttled to about ten minutes), and each run re-materialized the
    complete history (about 30 s of CPU natively) although nothing was due.
    A reporting pass over a current projection loads nothing; the periodic
    verification still runs once it is due."""
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    _no_spawn(monkeypatch)
    _damage("unreadable")
    for minutes in (1, 2):
        with raw_load_recorder(quota) as worker:
            _run_worker(monkeypatch, now + dt.timedelta(minutes=minutes))
        assert worker.unbounded_calls == 0, (
            f"the worker run {minutes} min after the last full pass loaded the "
            "complete history with no verification due")
        line = _worker_lines(ns)[-1]
        assert "result=error" in line and " pending=1" in line and (
            "projection=alert_obligations_unreadable" in line), line

    due = now + dt.timedelta(
        seconds=quota.CODEX_QUOTA_FULL_VERIFICATION_INTERVAL_SECONDS + 60)
    with raw_load_recorder(quota) as worker:
        _run_worker(monkeypatch, due)
    assert worker.unbounded_calls >= 1, "the due verification did not run"


def test_a_damaged_sidecar_worker_whose_pass_disarmed_reports_nothing_pending(
    tmp_path, monkeypatch,
):
    """Detects an outcome line that overstates pending work: the worker's
    reporting pass over a damaged sidecar executed the live disarm, which ends
    the damaged generation (the file is moved aside and an empty request set
    written), yet the line still said ``pending=1``."""
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    _no_spawn(monkeypatch)
    _damage("unreadable")
    _write_delivery_off()
    _run_worker(monkeypatch, now + dt.timedelta(minutes=1))
    assert _arming_rows(ns) == 0, "premise: the worker's pass did not disarm"
    assert _requests() == [], "premise: the disarm did not end the generation"
    line = _worker_lines(ns)[-1]
    assert " pending=0" in line and (
        "alert_obligations_unreadable" not in line), line


@pytest.mark.parametrize("site", ["roots", "locks"])
def test_an_unavailable_lifecycle_directory_logs_and_keeps_the_obligation(
    tmp_path, monkeypatch, site,
):
    """Detects an activation outcome lost to an ``OSError``: resolving the
    configured roots, or creating the lifecycle-lock directory (an ejected or
    unwritable volume), raised out of the worker before any ``op=quota-verify``
    line, so Doctor saw no outcome for the run. The worker logs the failure
    and leaves the obligation exactly as recorded."""
    ns, quota, now = _armed_store(tmp_path, monkeypatch)
    import _cctally_record  # after `_load`, which re-imports the modules
    import _lib_codex_hooks

    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    _set_axis(ns, quota, now, "gate_enabled")
    _hook(quota, now)
    raw = _sidecar_path().read_bytes()

    def unavailable(*args, **kwargs):
        raise OSError("lifecycle directory unavailable")

    if site == "roots":
        monkeypatch.setattr(_cctally_record, "_codex_lifecycle_roots", unavailable)
    else:
        monkeypatch.setattr(
            _lib_codex_hooks, "acquire_due_lifecycle_locks", unavailable)
    _run_worker(monkeypatch, now + dt.timedelta(minutes=1))
    lines = _worker_lines(ns)
    assert lines and "result=error" in lines[-1] and (
        "projection=alert_lifecycle_unavailable" in lines[-1]), lines
    assert _sidecar_path().read_bytes() == raw, "the obligation was changed"
    assert _stored_gate(ns) == 0


def test_a_removal_failure_dispatches_the_delayed_crossing_once_and_keeps_the_request(
    tmp_path, monkeypatch,
):
    """Detects a notification lost to request cleanup (Appendix B round 4,
    P2): removing the completed request runs after the commit and before the
    dispatch, so a failed removal must not raise. The delayed crossing is
    dispatched exactly once, the request and the counts survive, and the
    retry dispatches nothing twice and removes the request."""
    import os

    ns, quota, now0, live_reset = _delayed_crossing_store(
        tmp_path, monkeypatch, thresholds=_REENABLE_THRESHOLDS)
    jr = importlib.import_module("_cctally_journal")
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    cutoff = now0 + dt.timedelta(minutes=2)
    _hook(quota, cutoff)
    [first] = _requests()
    _append_live(ns, offset=10_001, captured_at=cutoff + dt.timedelta(minutes=1),
                 used_percent=60.0)
    name = _sidecar_path().name
    real_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        if os.fspath(dst).endswith(name):
            raise OSError("removal boom")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    _run_worker(monkeypatch, cutoff + dt.timedelta(minutes=2))
    monkeypatch.setattr(os, "replace", real_replace)
    assert [alert.get("threshold") for alert in dispatched] == [50], dispatched
    assert _requests() == [first]
    line = _worker_lines(ns)[-1]
    assert "result=error" in line and (
        "projection=alert_obligations_unwritable" in line), line
    assert " blocks=0 " not in line, line

    _run_worker(monkeypatch, cutoff + dt.timedelta(minutes=3))
    assert len(dispatched) == 1, dispatched
    assert _requests() == []
    assert "result=success" in _worker_lines(ns)[-1]


# ── #1034: the drain settlement's alerts at a matched logical time (Q9.3) ───

def _pin_reconcile_now(ns, monkeypatch, when):
    """Every reconcile the core makes evaluates at ``when``, so the two arms
    compare at one logical time rather than two wall clocks."""
    real = ns["reconcile_codex_quota_projection"]

    def pinned(**kwargs):
        kwargs["now"] = when
        return real(**kwargs)
    monkeypatch.setitem(ns, "reconcile_codex_quota_projection", pinned)


def _alerted_count(ns, threshold):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM quota_threshold_events WHERE source='codex'"
            " AND threshold=? AND disposition='alerted'",
            (threshold,)).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("case", [
    "ordinary_crossing", "account_continuity_adoption"])
def test_settlement_matches_a_hook_at_the_same_logical_time(
    tmp_path, monkeypatch, case,
):
    """Detects an alert the settlement loses, duplicates or evaluates
    differently from the hook it stands in for (#1034 §5.7): from one
    post-prune state, a settlement and a hook-kind core over every root at
    the same logical time must reach the same terminal identities,
    dispositions and dispatches, one of them positive, and the next hook on
    the settled copy must dispatch nothing again.

    ``ordinary_crossing``: a crossing captured after the identity's
    activation, ingested by each arm's own budgeted sync. The adoption case is
    the delete-induced account-continuity adoption of #1033 review 1, finding
    1: an unattributed observation shares W1 with accounts A and B, so it
    stays unattributed; deleting B's rollout lets A adopt it, and A newly
    crosses 15 %."""
    import collections
    import sys
    import test_codex_quota_incremental as inc
    from _codex_zstd_fixtures import token_records, write_rollout
    from test_codex_hook_lifecycle import _run_core
    base = (dt.datetime.now(UTC).replace(second=0, microsecond=0)
            - dt.timedelta(hours=3))
    activation = base + dt.timedelta(minutes=60)
    when = base + dt.timedelta(minutes=90)
    adoption = case == "account_continuity_adoption"
    ns, _quota, home, paths = inc.build_settlement_estate(
        tmp_path, monkeypatch, base=base, prime_now=activation,
        rollouts={"keeper": ("gpt-5", ((10, 10, 1),)),
                  "victim": ("gpt-5", ((20, 12, 1),))},
        accounts={"keeper": "acct-a", "victim": "acct-b"} if adoption
        else None)
    sent: list = []
    monkeypatch.setattr(  # the estate's own load replaced the module
        sys.modules["_cctally_journal"], "ALERT_DISPATCHER", lambda alerts: sent.extend(
            json.dumps(alert, sort_keys=True, default=str)
            for alert in alerts))
    late = inc._settle_rollout_path(home, 9, "late")
    late_records = token_records(2, session_id="late") + [
        inc.settle_rate_limit_event(base, 70, 30 if adoption else 47, 1)]
    if adoption:
        (home / "auth.json").unlink(missing_ok=True)
        write_rollout(late, late_records)
        inc.sync_settlement_estate(ns)
        inc.eligible_settlement_pass(ns, base + dt.timedelta(minutes=75))
        assert _alerted_count(ns, 15) == 0, (
            "premise: the ambiguous unattributed point alerted before the "
            "deletion")
        inc.prune_settlement_victim(ns, paths["victim"])
    else:
        inc.prune_settlement_victim(ns, paths["victim"])
        write_rollout(late, late_records)
    sent.clear()
    _pin_reconcile_now(ns, monkeypatch, when)
    inc._publish_quota_index()
    inc.snapshot_data_dir(ns, tmp_path / "post-prune")

    settled = _run_core(ns, kind="settlement")
    assert (settled.result, settled.reconciled) == ("success", True), settled
    settled_dump, settled_sent = inc._stats_dump(ns), list(sent)
    inc.snapshot_data_dir(ns, tmp_path / "settled")

    sent.clear()
    inc.restore_data_dir(ns, tmp_path / "post-prune")
    hooked = _run_core(ns, kind="hook")
    assert (hooked.result, hooked.reconciled) == ("success", True), hooked
    assert inc._stats_dump(ns) == settled_dump
    assert collections.Counter(sent) == collections.Counter(settled_sent)
    assert settled_sent, "the case dispatched nothing, so it proves nothing"

    sent.clear()
    inc.restore_data_dir(ns, tmp_path / "settled")
    _pin_reconcile_now(ns, monkeypatch, when + dt.timedelta(minutes=1))
    assert _run_core(ns, kind="hook").result == "success"
    assert sent == [], "the next hook re-dispatched a settled alert"


def test_settlement_evaluates_pending_claude_dollars_without_a_nested_ingest(
    tmp_path, monkeypatch,
):
    """Detects an authoritative ingest nested inside the settlement (#1034
    Q1): the reconcile's journal pipeline runs a pending Claude
    observation's dollar axes, whose projected Codex leg reads Codex spend.
    Outside the hook read policy that read syncs and reconciles again inside
    the outer ingest; inside it, the read uses the pinned snapshot and the
    expected notification still goes out."""
    import _cctally_journal as jr
    import _lib_journal as jl
    from test_968_gate_reader_contracts import BudgetHarness
    from test_codex_hook_lifecycle import _run_core
    h = BudgetHarness(tmp_path, monkeypatch, codex_budget={
        "amount_usd": 1000.0, "period": "calendar-month",
        "alerts_enabled": True, "projected_enabled": True,
        "alert_thresholds": [90]}, claude={
        "weekly_usd": 1000.0, "period": "calendar-month",
        "alerts_enabled": True, "projected_enabled": True,
        "alert_thresholds": [90]})
    h.rollout("seed", 3)
    h.seed_sync()
    monkeypatch.setitem(h.ns, "_sum_cost_for_range",
                        lambda start, end, **kw: 950.0)
    reconciles: list = []
    real_reconcile = h.ns["reconcile_codex_quota_projection"]
    monkeypatch.setitem(
        h.ns, "reconcile_codex_quota_projection",
        lambda **kw: reconciles.append(1) or real_reconcile(**kw))
    jr.append_record(jl.make_obs(
        at="2026-07-25T11:59:00Z", src="record-usage", provider="claude",
        payload={"weekly_percent": 12.0, "source": "statusline",
                 "resets_at": int(dt.datetime(
                     2026, 7, 28, tzinfo=UTC).timestamp())},
        account=None))

    outcome = _run_core(h.ns, kind="settlement")

    assert outcome.reconciled and outcome.pinned, outcome
    assert h.syncs == ["budgeted"], h.syncs
    assert h.direct_parses == []
    assert reconciles == [1], "a nested reconcile ran inside the settlement"
    assert h.dispatched, "the pending Claude dollar axes dispatched nothing"


# ── #1076: an off-hook reconcile's own ingest reads Codex spend defer-only ──
#
# Fixture: the #1070 armed quota store, real Codex accounting spend under its
# root (one row attributed to a real account), a Codex budget whose readers
# are each pending and would cross, a Claude budget the observation crosses,
# and one pending Claude `record-usage` observation. A reconcile outside the
# hook runs that observation's pipeline inside its own `run_stats_ingest`.

_OFF_HOOK_ACCOUNT_LABEL = "acct-1076"


def _bin_chain():
    import traceback
    return [f"{fr.filename.rsplit('/', 1)[-1]}:{fr.name}"
            for fr in traceback.extract_stack()[:-2]
            if "/bin/" in fr.filename]


def _seed_codex_spend(ns, account):
    """Two accounting rows under the store's root inside the live window's
    calendar week, the second attributed to ``account``. Returns the
    vendor-wide and per-account spend a complete read of them prices."""
    conn = ns["open_cache_db"]()
    try:
        for offset, acct in ((100_000, None), (100_001, account)):
            _fixture_builders.seed_codex_session_entry(
                conn, source_path=SOURCE_PATH, line_offset=offset,
                timestamp_utc=f"2026-07-01T10:0{offset % 10}:00Z",
                session_id="spend-1076", model="gpt-5",
                input_tokens=1_000_000, output_tokens=100_000,
                total_tokens=1_100_000, source_root_key=ROOT_KEY,
                account_key=acct)
        conn.commit()
    finally:
        conn.close()
    speed = ns["_resolve_codex_speed"]("auto")
    each = ns["_calculate_codex_entry_cost"](
        "gpt-5", 1_000_000, 0, 100_000, 0, speed=speed)
    assert each > 0
    return 2 * each, each


def _append_claude_obs(jr, jl, at, percent):
    jr.append_record(jl.make_obs(
        at=at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        src="record-usage", provider="claude",
        payload={"weekly_percent": percent, "source": "statusline",
                 "resets_at": int((at + dt.timedelta(days=3)).timestamp())}))


def _off_hook_store(tmp_path, monkeypatch, *, obligation, uncertified_row,
                    full_codex_budget):
    """Build the fixture; returns a dict of its handles."""
    import _lib_accounts
    ns, quota = _load(tmp_path, monkeypatch)
    cache = importlib.import_module("_cctally_cache")
    jr = importlib.import_module("_cctally_journal")
    import _cctally_core
    import _lib_journal as jl
    build_codex_quota_store(ns, closed_windows=3, observations_per_window=10)
    account = _lib_accounts.account_key("codex", _OFF_HOOK_ACCOUNT_LABEL)
    vendor_spend, account_spend = _seed_codex_spend(ns, account)
    alerts = {"enabled": True, "notifier": "none",
              "quota": {"enabled": True, "actual_thresholds": [2],
                        "projected_thresholds": [], "rules": []}}
    _cctally_core.CONFIG_PATH.write_text(json.dumps({"alerts": alerts}) + "\n")
    now = store_as_of(ns, before_reset=dt.timedelta(minutes=10))
    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys={ROOT_KEY})
    _no_spawn(monkeypatch)
    _lifecycle_roots(monkeypatch, tmp_path)
    if obligation:
        _stats_write(ns, "UPDATE quota_projection_ledger_state "
                         "   SET alerts_enabled=0 WHERE source='codex'")
        assert _hook(quota, now).alert_work_pending
        assert _requests(), "no obligation recorded"
    # Every Codex reader pending and sized to cross at its 50 % threshold;
    # calendar-week, so the projected axis is past its low-confidence floor.
    codex = {"amount_usd": vendor_spend, "period": "calendar-week",
             "alerts_enabled": True, "alert_thresholds": [50]}
    if full_codex_budget:
        codex.update(projected_enabled=True,
                     accounts={account: account_spend})
    _cctally_core.CONFIG_PATH.write_text(json.dumps({
        "display": {"tz": "utc"}, "alerts": alerts,
        "budget": {"weekly_usd": 100.0, "period": "calendar-month",
                   "alerts_enabled": True, "alert_thresholds": [50],
                   "codex": codex},
    }) + "\n")
    monkeypatch.setitem(ns, "_sum_cost_for_range",
                        lambda start, end, **kw: 60.0)
    dispatched: list = []
    monkeypatch.setitem(
        ns, "_dispatch_alert_notification",
        lambda payload, *, mode="real", **kw: dispatched.append(payload)
        or "queued")
    _append_claude_obs(jr, jl, now - dt.timedelta(minutes=1), 41.5)
    if uncertified_row:
        append_one_observation_to_live_window(ns)
    return {"ns": ns, "quota": quota, "cache": cache, "jr": jr, "jl": jl,
            "now": now, "account": account, "dispatched": dispatched}


def _nesting_seams(monkeypatch, fx):
    """Recorders for every Codex sync, reconcile, ingest-lock acquisition and
    off-hook deferral, each tagged with whether the ingest lock was held."""
    import _cctally_store
    quota, cache, jr, ns = fx["quota"], fx["cache"], fx["jr"], fx["ns"]
    held = _cctally_store.holds_ingest_lock
    seen = {"reconcile": [], "sync": [], "ingestLock": [], "deferrals": [],
            "dollarAxes": 0}
    depth = {"n": 0}
    real_reconcile = quota.reconcile_codex_quota_projection
    real_sync = cache.sync_codex_cache
    real_lock = jr._acquire_ingest_lock
    real_deferral = cache._OffHookCodexReadPolicy.record_deferral
    record = importlib.import_module("_cctally_record")
    real_axes = record._run_dollar_axes

    def reconcile(*a, **k):
        seen["reconcile"].append({"depth": depth["n"], "held": held(),
                                  "chain": _bin_chain()})
        depth["n"] += 1
        try:
            return real_reconcile(*a, **k)
        finally:
            depth["n"] -= 1

    def sync(*a, **k):
        seen["sync"].append({"depth": depth["n"], "held": held(),
                             "chain": _bin_chain()})
        return real_sync(*a, **k)

    def lock(*a, **k):
        seen["ingestLock"].append({"held": held()})
        return real_lock(*a, **k)

    def deferral(self, reader, reason):
        seen["deferrals"].append((reader, reason))
        return real_deferral(self, reader, reason)

    def axes(*a, **k):
        if depth["n"]:
            seen["dollarAxes"] += 1
        return real_axes(*a, **k)

    monkeypatch.setattr(quota, "reconcile_codex_quota_projection", reconcile)
    monkeypatch.setattr(cache, "sync_codex_cache", sync)
    monkeypatch.setattr(jr, "_acquire_ingest_lock", lock)
    monkeypatch.setattr(cache._OffHookCodexReadPolicy, "record_deferral",
                        deferral)
    monkeypatch.setattr(record, "_run_dollar_axes", axes)
    for name, fn in (("reconcile_codex_quota_projection", reconcile),
                     ("sync_codex_cache", sync), ("_run_dollar_axes", axes)):
        if name in ns:
            monkeypatch.setitem(ns, name, fn)
    return seen


def _assert_nothing_nested(seen):
    nested = {
        "sync": [s for s in seen["sync"] if s["held"] or s["depth"]],
        "reconcile": [r for r in seen["reconcile"] if r["held"] or r["depth"]],
        "ingestLock": [x for x in seen["ingestLock"] if x["held"]],
    }
    assert not any(nested.values()), nested
    # Positive control for the ingest-lock seam: the outer cycle's own
    # acquisition must have been recorded, or a renamed/bypassed
    # _acquire_ingest_lock would let the nested-lock check pass vacuously.
    assert any(not x["held"] for x in seen["ingestLock"]), (
        "the ingest-lock seam recorded no unheld acquisition", seen["ingestLock"])


def _worker_fields(ns):
    lines = _worker_lines(ns)
    assert lines, "the worker logged nothing"
    return dict(tok.split("=", 1) for tok in lines[-1].split()[1:]
                if "=" in tok)


def _codex_dispatches(dispatched):
    return sorted(
        (p.get("axis"), p.get("account_key") or "*")
        for p in dispatched
        if p.get("axis") == "codex_budget"
        or (p.get("axis") == "projected"
            and p.get("metric") == "codex_budget_usd"))


def _codex_budget_rows(ns):
    conn = ns["open_db"]()
    try:
        return sorted(
            [("codex_budget", row[0], row[1]) for row in conn.execute(
                "SELECT account_key, threshold FROM budget_milestones "
                " WHERE vendor='codex'")]
            + [("projected", "*", row[0]) for row in conn.execute(
                "SELECT threshold FROM projected_milestones "
                " WHERE metric='codex_budget_usd'")])
    finally:
        conn.close()


def _claude_budget_rows(ns):
    conn = ns["open_db"]()
    try:
        return [row[0] for row in conn.execute(
            "SELECT threshold FROM budget_milestones WHERE vendor='claude'")]
    finally:
        conn.close()


@pytest.mark.timeout(240)
@pytest.mark.parametrize("uncertified_row", [False, True],
                         ids=["certificate-current", "one-unprojected-row"])
def test_activation_never_nests_a_codex_read_inside_its_ingest(
        tmp_path, monkeypatch, uncertified_row):
    """#1076 (spec T1, A1/A3), adopted from the #1034 T8 RED probe. The verify
    worker's activation leg runs its reconcile outside the hook's read policy,
    so the pending Claude observation its ingest consumes reaches the dollar
    axes, whose Codex readers ran an unbudgeted `sync_codex_cache` inside the
    ingest and, with a stale certificate, a nested reconcile that waited 10 s
    for the ingest lock its own process held. Detects: any Codex sync,
    reconcile or ingest-lock attempt inside the activation's held ingest; any
    of the three Codex readers not deferring `off_hook_unpinned`; the worker
    line without `budget_deferral=`; the obligation left unsettled; a Codex
    crossing evaluated on an unproved read; the Claude crossing suppressed.
    Phase 2 is the positive control (each Codex crossing fires once through
    `record-usage`'s own ingest) and phase 3 proves it fires only once."""
    fx = _off_hook_store(tmp_path, monkeypatch, obligation=True,
                         uncertified_row=uncertified_row,
                         full_codex_budget=True)
    ns, jr, jl, now = fx["ns"], fx["jr"], fx["jl"], fx["now"]
    account = fx["account"]
    seen = _nesting_seams(monkeypatch, fx)

    # ── phase 1: the worker's activation ───────────────────────────────────
    _run_worker(monkeypatch, now + dt.timedelta(seconds=30))
    fields = _worker_fields(ns)
    assert seen["reconcile"] and seen["dollarAxes"] > 0, (
        "premise: the pending observation reached the dollar axes inside the "
        "activation's reconcile", seen)
    _assert_nothing_nested(seen)
    assert set(seen["deferrals"]) == {
        ("codex_budget:*", "off_hook_unpinned"),
        (f"codex_budget:{account}", "off_hook_unpinned"),
        ("codex_projected", "off_hook_unpinned"),
    }, seen["deferrals"]
    assert (fields.get("mode"), fields.get("result"),
            fields.get("budget_deferral")) == (
        "activation", "success", "off_hook_unpinned"), fields
    assert not _requests(), "the obligation was not settled"
    assert _codex_budget_rows(ns) == [], "a Codex crossing on an unproved read"
    assert _codex_dispatches(fx["dispatched"]) == []
    assert _claude_budget_rows(ns) == [50]
    assert any(p.get("axis") == "budget" for p in fx["dispatched"]), (
        "the Claude crossing was not dispatched", fx["dispatched"])

    # ── phase 2: record-usage's own ingest re-evaluates (positive control) ─
    _append_claude_obs(jr, jl, now, 42.0)
    assert jr.run_stats_ingest(mode="opportunistic").ran
    expected = [("codex_budget", "*", 50), ("codex_budget", account, 50),
                ("projected", "*", 50)]
    assert _codex_budget_rows(ns) == expected
    assert _codex_dispatches(fx["dispatched"]) == sorted(
        [("codex_budget", "*"), ("codex_budget", account),
         ("projected", "*")]), fx["dispatched"]

    # ── phase 3: nothing fires twice ────────────────────────────────────────
    before = list(fx["dispatched"])
    _append_claude_obs(jr, jl, now + dt.timedelta(seconds=10), 42.5)
    assert jr.run_stats_ingest(mode="opportunistic").ran
    assert _codex_dispatches(fx["dispatched"]) == _codex_dispatches(before)
    assert _codex_budget_rows(ns) == expected


@pytest.mark.timeout(240)
@pytest.mark.parametrize(
    "path", ["verify", "damaged-sidecar", "reporting", "sync-auto"])
def test_off_hook_reconcile_twins_never_nest(tmp_path, monkeypatch, path):
    """#1076 (spec T2, A4): the activation's twins. The plain verify leg, the
    damaged-sidecar leg, a reporting reconcile (dashboard tick, `codex quota`)
    and a plain `sync_codex_cache` whose auto tail reconciles each consumed
    the pending Claude observation inside an ingest of their own and nested a
    Codex sync and, with a stale certificate, a reconcile that waited 10 s.
    Detects that nesting, and proves each case's Codex reader was reached and
    deferred, so a passing case cannot come from a pipeline that never read."""
    fx = _off_hook_store(tmp_path, monkeypatch, obligation=False,
                         uncertified_row=True, full_codex_budget=False)
    ns, quota, cache, now = fx["ns"], fx["quota"], fx["cache"], fx["now"]
    if path == "damaged-sidecar":
        _damage("unreadable")
    seen = _nesting_seams(monkeypatch, fx)
    collector: list = []
    if path in ("verify", "damaged-sidecar"):
        _run_worker(monkeypatch, now + dt.timedelta(seconds=30))
        fields = _worker_fields(ns)
        if path == "damaged-sidecar":
            assert fields.get("mode") == "verify", fields
        assert fields.get("budget_deferral") == "off_hook_unpinned", fields
    elif path == "reporting":
        quota.reconcile_codex_quota_projection(
            now=now + dt.timedelta(seconds=30), _read_deferrals=collector)
        assert collector == ["off_hook_unpinned"], collector
    else:
        conn = ns["open_cache_db"]()
        try:
            cache.sync_codex_cache(conn)
        finally:
            conn.close()
    assert seen["dollarAxes"] > 0, ("premise: the observation was consumed "
                                    "inside a reconcile", seen)
    _assert_nothing_nested(seen)
    assert seen["deferrals"] == [("codex_budget:*", "off_hook_unpinned")], (
        seen["deferrals"])
    assert _codex_budget_rows(ns) == []


def test_off_hook_policy_reports_deferrals_when_the_reconcile_raises(
    tmp_path, monkeypatch,
):
    """#1076 D2: the collector is filled in `finally`. Detects a deferral
    lost because the reconcile raised after its pipeline deferred, and an
    off-hook policy left active after the raise."""
    fx = _off_hook_store(tmp_path, monkeypatch, obligation=False,
                         uncertified_row=True, full_codex_budget=False)
    quota, cache = fx["quota"], fx["cache"]

    def boom():
        raise RuntimeError("before-commit failure")

    collector: list = []
    with pytest.raises(RuntimeError, match="before-commit failure"):
        quota.reconcile_codex_quota_projection(
            now=fx["now"] + dt.timedelta(seconds=30),
            _before_stats_commit=boom, _read_deferrals=collector)
    assert collector == ["off_hook_unpinned"], collector
    assert cache._active_codex_read_policy() is None
