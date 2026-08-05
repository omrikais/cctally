"""Pure kernel deciding what an alert pass has to look at (public #5 §3).

Alert state is NOT a function of window dirtiness, so a projector bounded by the
change ledger would silently stop honouring four of the five things that can
make an alert eligible. Each axis below is a way the answer changes with no row
in ``quota_window_snapshots`` moving at all:

1. **Physical-window dirtiness** — the ledger. The only axis the ledger sees.
2. **Policy scope** — the resolved rule changed. ``quota_rule_fingerprint``
   already hashes the resolved thresholds, gates, root and logical limit, and
   is persisted per identity in ``quota_alert_arming``; comparing it against a
   freshly resolved one detects the change. An exact-rule change is scoped to
   its identities, a default/global change marks everything.
3. **Delivery gate** — the global or quota switch flipped. DISABLING must
   enumerate every Codex arming row, delete it and journal a disarm, and must do
   so even with zero dirty windows and zero lifecycle-eligible roots; that is
   why it needs no observation load and why the caller runs it before the
   eligibility fast path. ENABLING is the opposite: it needs a semantic pass
   over the affected identities regardless of physical dirtiness, because
   activation has to write ``suppressed_backfill`` terminal rows for
   already-satisfied thresholds instead of dispatching history.
4. **Scheduled time** — an observation captured in the future is skipped today,
   and becomes eligible when wall time passes it with no mutation to observe.
   Persisting that boundary and treating ``now >= boundary`` as dirty is what
   closes it. Unlike axes 2 and 3 this one fires on WALL CLOCK rather than on a
   configuration change. An ownership schedule lets the hook evaluate only the
   complete roots whose deadlines matured; scalar-only legacy state still
   defers rather than paying an unannounced whole-history pass on a blocking
   tick.
5. **Durable lifecycle state** — the existing arming rows and terminal events,
   unchanged. Represented here only as the fingerprints axis 2 compares.

The scope this returns is deliberately a SUPERSET decision: widening a bounded
pass is always safe, missing an identity is not.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Mapping


#: Ordered weakest to strongest; a later value absorbs an earlier one.
SCOPE_NONE = "none"
SCOPE_GROUPS = "groups"
SCOPE_ROOTS = "roots"
SCOPE_ALL = "all"

_ORDER = {SCOPE_NONE: 0, SCOPE_GROUPS: 1, SCOPE_ROOTS: 2, SCOPE_ALL: 3}

#: Recorded in place of ``"scheduled"`` when axis 4 came due but the caller
#: cannot honour it inline. A caller that sees it MUST carry the stored
#: boundary through untouched — the axis is postponed, not satisfied.
REASON_SCHEDULED_DEFERRED = "scheduled_deferred"


@dataclass(frozen=True)
class AlertDirtyScope:
    """What the pass has to load, and whether it has to disarm first."""

    disarm_all: bool
    scope: str
    roots: frozenset[str]
    reasons: tuple[str, ...]

    def widens(self, ledger_scope: str) -> bool:
        """True when the alert axes demand more than the ledger already gives."""
        return _ORDER[self.scope] > _ORDER[ledger_scope]


def _strongest(a: str, b: str) -> str:
    return a if _ORDER[a] >= _ORDER[b] else b


def alert_dirty_scope(
    *,
    ledger_groups: Iterable[object],
    stored_fingerprints: Mapping[tuple, str],
    resolved_fingerprints: Mapping[tuple, str],
    gate_before: "bool | None",
    gate_after: bool,
    now: dt.datetime,
    next_evaluation_at: "dt.datetime | None",
    scheduled_roots: "Iterable[str] | None" = None,
    defer_scheduled: bool = False,
) -> AlertDirtyScope:
    """Resolve the five axes into one decision.

    ``stored_fingerprints`` / ``resolved_fingerprints`` are keyed by the arming
    identity tuple ``(source, source_root_key, account_key, logical_limit_key,
    observed_slot, window_minutes)``; the ROOT is element 1, which is what an
    exact-rule change is scoped to.

    ``scheduled_roots`` is the validated ownership retained with axis 4. When
    present, a matured instant scopes to those roots even on the hook path.
    ``None`` is the legacy/unavailable-ownership shape; only that shape needs
    ``defer_scheduled`` to avoid an unannounced whole-history hook pass, and the
    caller then owes the scalar boundary a carry-through.
    """
    reasons: list[str] = []
    if not gate_after:
        # Nothing to evaluate: delivery is off, so the only work is making sure
        # no arming boundary survives to turn disabled-period evidence into a
        # later alert. It needs no observations at all.
        return AlertDirtyScope(
            disarm_all=True, scope=SCOPE_NONE, roots=frozenset(),
            reasons=("gate_disabled",),
        )

    scope = SCOPE_NONE
    roots: set[str] = set()

    if any(True for _ in ledger_groups):
        scope = _strongest(scope, SCOPE_GROUPS)
        reasons.append("window_dirty")

    if gate_before is not True:
        # Enabling — or a first pass with no recorded gate state, which cannot
        # be distinguished from one and must not be assumed to be a no-op.
        scope = _strongest(scope, SCOPE_ALL)
        reasons.append("gate_enabled")

    changed_roots = {
        identity[1]
        for identity, fingerprint in resolved_fingerprints.items()
        if stored_fingerprints.get(identity) != fingerprint
    }
    if changed_roots:
        scope = _strongest(scope, SCOPE_ROOTS)
        roots |= changed_roots
        reasons.append("rule_changed")

    if next_evaluation_at is not None and now >= next_evaluation_at:
        # Epoch 1007 records the roots owning each scheduled instant. A complete
        # semantic pass over those roots is bounded enough for the hook path and
        # is all axis 4 needs. ``None`` means legacy/unavailable ownership, where
        # the only honest scope remains everything (and therefore deferral on a
        # hook tick). An empty known set means the owning roots are not lifecycle
        # eligible on this tick; the stored axis remains due for a later tick.
        if scheduled_roots is not None:
            due_roots = {str(root) for root in scheduled_roots if str(root)}
            if due_roots:
                scope = _strongest(scope, SCOPE_ROOTS)
                roots |= due_roots
                reasons.append("scheduled")
        elif defer_scheduled:
            reasons.append(REASON_SCHEDULED_DEFERRED)
        else:
            scope = _strongest(scope, SCOPE_ALL)
            reasons.append("scheduled")

    return AlertDirtyScope(
        disarm_all=False, scope=scope, roots=frozenset(roots),
        reasons=tuple(reasons),
    )


def next_evaluation_boundary(
    *, capture_times: Iterable[dt.datetime], now: dt.datetime,
    stored: "dt.datetime | None", retain_due: bool = False,
) -> "dt.datetime | None":
    """The earliest still-future capture the projector must come back for.

    This is the legacy scalar helper. A bounded pass only sees dirty windows, so
    the STORED boundary is
    retained whenever it is still in the future: dropping it would forget a
    future-clocked observation sitting in a window this pass never loaded. Once
    wall time passes it the axis fires and the caller decides whether it has
    enough ownership to scope the pass.

    ``retain_due`` keeps a boundary that is ALREADY due, which is the case where
    "recomputed from complete evidence" is a lie: a reporting-only pass never
    reaches a threshold decision at all, and a BOUNDED hook tick that declined
    the widening deliberately did not look. Either would otherwise retire the
    axis on behalf of an evaluation nobody performed.

    Epoch 1007's per-root map is maintained by the projector rather than this
    helper. It closes the quiet-window gap by letting a hook tick replace only
    the roots it evaluated; scalar-only legacy state still uses ``retain_due``
    and the conservative full/deferred path.
    """
    candidates = [value for value in capture_times if value > now]
    if stored is not None and (retain_due or stored > now):
        candidates.append(stored)
    return min(candidates) if candidates else None
