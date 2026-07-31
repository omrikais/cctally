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
   configuration change, which is why the hook path defers it
   (``defer_scheduled``) instead of paying an unannounced whole-history pass on
   a blocking tick.
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
    defer_scheduled: bool = False,
) -> AlertDirtyScope:
    """Resolve the five axes into one decision.

    ``stored_fingerprints`` / ``resolved_fingerprints`` are keyed by the arming
    identity tuple ``(source, source_root_key, account_key, logical_limit_key,
    observed_slot, window_minutes)``; the ROOT is element 1, which is what an
    exact-rule change is scoped to.

    ``defer_scheduled`` is the hook path's (``full_pass="defer"``). Axes 2 and 3
    are driven by a configuration change the user just made, so widening for
    them is bounded and expected; axis 4 is driven by WALL CLOCK, which makes it
    the one route into a whole-history pass that can land on a blocking hook
    tick with nothing to have predicted it. Under this flag it is recorded as
    ``REASON_SCHEDULED_DEFERRED`` and does NOT strengthen the scope — and the
    caller owes the stored boundary a carry-through, because a deferral that
    lets the boundary be recomputed is a silent drop.
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
        # A future-clocked observation just became eligible. Which identity it
        # belongs to is not recorded — only the instant — so the honest scope is
        # everything, and on the hook path "everything" is precisely what may
        # not run.
        if defer_scheduled:
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

    A bounded pass only sees the dirty windows, so the STORED boundary is
    retained whenever it is still in the future: dropping it would forget a
    future-clocked observation sitting in a window this pass never loaded. Once
    wall time passes it the axis fires, the pass widens to everything, and the
    boundary is recomputed from complete evidence — so a retained value can only
    ever cost one extra pass, never a missed one.

    ``retain_due`` keeps a boundary that is ALREADY due, which is the case where
    "recomputed from complete evidence" is a lie: a reporting-only pass never
    reaches a threshold decision at all, and a BOUNDED hook tick that declined
    the widening deliberately did not look. Either would otherwise retire the
    axis on behalf of an evaluation nobody performed.

    A due value sorts before every future candidate, so it stays until a pass
    that genuinely looked at everything retires it — in practice a hook tick
    that widened to whole-history for axis 2 or 3, since carrying alert
    eligibility is what separates such a pass from a reporting-only one and the
    hook is the only production caller that carries it.

    It does NOT stay "until a pass that can act on it does", and that gap is
    open rather than closed: on a hook-only install with a steady enabled gate,
    unchanged rules and a quiet ledger, no qualifying pass ever runs and the
    instant is retained indefinitely. The cost is bounded — the tick stays
    bounded and fast, and the window is re-evaluated as soon as it goes
    ledger-dirty again, which for a live window is continuous — so the exposure
    is a future-clocked capture in a window that then goes permanently quiet
    never qualifying a threshold. Under-alerting, never a stall or a burst.
    """
    candidates = [value for value in capture_times if value > now]
    if stored is not None and (retain_due or stored > now):
        candidates.append(stored)
    return min(candidates) if candidates else None
