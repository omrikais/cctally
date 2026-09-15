"""Cause-keyed retry state machine for degraded source generations (#830).

#834 S2. A ``partial`` source generation must not be handed back for the life
of the process. Refusing reuse outright is the correct statement of coherence
and is made in ``_lib_dashboard_sources._reusable_provider``; this module
answers the separate question that refusal raises, which is how often a cause
that is NOT going to clear on its own should pay for a full rebuild.

The kernel is pure and clock-injected: it performs no I/O, reads no wall
clock, and mutates nothing the caller passed in. It returns a decision and the
next state, and the caller holds that state for the life of its process.

The tree has more partial-producing states than "two producers" suggests:

* ``_lib_dashboard_sources.degrade_source_state`` retains a prior generation
  during a transient ingest failure, producing ``partial``/``stale``;
* a freshly built Codex source becomes ``partial``/``fresh`` on incomplete
  project metadata, a hero projection failure or an unreadable account
  registry (``_cctally_dashboard_sources``);
* the idle Codex clock can turn an otherwise healthy generation ``partial``
  when a retained cycle expires.

Those causes behave differently. An unreadable database is worth retrying at
once, while metadata that is structurally incomplete will still be incomplete
one second later and rebuilding for it on every tick buys nothing. The state
is therefore keyed by the pair of a normalized cause and the source
``data_version``, so a changed situation always retries immediately and only a
genuinely repeated one is throttled.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


RetryAction = Literal["rebuild", "retain"]

#: How long one repeated structural cause is retained before it pays for
#: another rebuild. Chosen to match the production ingest frontier interval
#: (120s, measured in #769 S6), because a structural Codex cause is cleared by
#: an ingest and retrying faster than ingest can change the answer spends a
#: full Codex rebuild to re-derive the same partial generation.
PARTIAL_RETRY_INTERVAL = dt.timedelta(seconds=120)

#: The cause recorded for a ``partial`` generation that states no warning at
#: all. The hydrating seed in ``_cctally_tui`` is one such generation.
NO_STATED_CAUSE = "none"

#: Normalized causes that are NEVER throttled. Each names a failure of the
#: read itself rather than a property of the data, so the next tick can
#: legitimately produce a different answer and must be allowed to try.
TRANSIENT_PARTIAL_CAUSES = frozenset((
    "ingest_failed",
    "ingest_contended",
    "transient_read_failure",
))

#: Normalized causes whose degraded generation MAY be retained under the
#: deadline. This is a positive allowlist and everything else — transient
#: causes, unrecognized codes, and every cause listed below — is rebuilt on
#: every tick, so an unclassified cause fails closed.
#:
#: Retention is safe only when nothing the tick re-derives can contradict the
#: retained generation. It does not hold in general. A reuse tick re-resolves
#: the account scope from the CURRENT registry while republishing the prior
#: ``data``, so retaining an ``account_scope_unresolved`` generation across a
#: registry that recovered would publish a scope naming two accounts beside a
#: ``data`` carrying no accounts and a warning still saying the scope is
#: unresolved (#819). A repaired quota projection must likewise trigger a
#: fresh build even when physical accounting did not move, so
#: ``projection_incoherent`` is not retainable either.
#:
#: ``metadata_incomplete`` is retainable because the generation is complete
#: except for project attribution, and no field the tick re-derives disagrees
#: with it. It is also the only cause for which refusing reuse adds rebuild
#: cost that was not already being paid: the two causes above were already
#: rebuilt on every tick before this kernel existed.
RETAINABLE_PARTIAL_CAUSES = frozenset((
    "metadata_incomplete",
))

#: Warning code -> normalized cause. Declared here rather than at the call
#: sites so one taxonomy governs every producer; an unrecognized code keeps
#: its own key through ``_OTHER_PREFIX`` instead of collapsing into a shared
#: bucket that would let two different situations throttle each other.
_CAUSE_BY_WARNING_CODE: Mapping[str, str] = MappingProxyType({
    "source_ingest_failed": "ingest_failed",
    "source_ingest_contended": "ingest_contended",
    "codex_metadata_incomplete": "metadata_incomplete",
    # No producer emits this code, and that is deliberate rather than an
    # oversight (#834 S2, verified by the browser gate). A transient
    # metadata-health failure never reaches this kernel as a warning, because
    # ``_reusable_provider`` refuses the generation outright on its
    # ``retryable`` carrier and the tick rebuilds. The entry stays so that a
    # producer added later gets the never-throttled behaviour its cause
    # deserves instead of falling into an ``other:`` bucket keyed by a string
    # nobody chose.
    "codex_metadata_health_unknown": "transient_read_failure",
    "codex_projection_incoherent": "projection_incoherent",
    "codex_account_scope_unresolved": "account_scope_unresolved",
    "codex_cycle_unavailable": "cycle_unavailable",
    "source_build_failed": "build_failed",
})

_OTHER_PREFIX = "other:"
_PROVIDERS = frozenset(("claude", "codex"))

EMPTY_RETRY_STATE: "PartialRetryState" = MappingProxyType({})


@dataclass(frozen=True)
class ProviderRetryEntry:
    """One provider's armed retry key and the instant it was first observed."""

    cause: str
    data_version: str
    first_seen: dt.datetime

    @property
    def key(self) -> tuple[str, str]:
        return (self.cause, self.data_version)

    def deadline(self, interval: dt.timedelta = PARTIAL_RETRY_INTERVAL) -> dt.datetime:
        return self.first_seen + interval


PartialRetryState = Mapping[str, ProviderRetryEntry]


@dataclass(frozen=True)
class PartialRetryDecision:
    """What the tick must do with this provider's degraded generation."""

    action: RetryAction
    cause: str
    data_version: str
    #: ``None`` for a transient cause, which arms no deadline at all.
    first_seen: dt.datetime | None
    deadline: dt.datetime | None
    throttled: bool

    @property
    def rebuild(self) -> bool:
        return self.action == "rebuild"


def _warning_code(item: object) -> str:
    """Return one warning's code, accepting a bare string or a warning object."""
    if isinstance(item, str):
        return item
    code = getattr(item, "code", None)
    if isinstance(code, str) and code:
        return code
    raise ValueError("a partial cause must be a code string or carry .code")


def normalize_partial_cause(cause: object) -> str:
    """Collapse one generation's stated causes into a single stable key.

    ``cause`` is a warning code, a warning object, an iterable of either, or
    ``None``. The result is order-independent and deduplicated, so the key
    changes when the SET of causes changes and not when a producer happens to
    append its warnings in a different order.
    """
    if cause is None:
        return NO_STATED_CAUSE
    if isinstance(cause, str) or not isinstance(cause, Iterable):
        items: tuple[object, ...] = (cause,)
    else:
        items = tuple(cause)
    if not items:
        return NO_STATED_CAUSE
    normalized = {
        _CAUSE_BY_WARNING_CODE.get(code, _OTHER_PREFIX + code)
        for code in (_warning_code(item) for item in items)
    }
    return "+".join(sorted(normalized))


def is_transient_partial_cause(cause: str) -> bool:
    """Whether a normalized cause names a failure of the read itself.

    A composite key is transient when ANY of its members is: the tick must be
    allowed to retry a failed read, and the structural cause travelling beside
    it loses nothing by being retried too.
    """
    return any(part in TRANSIENT_PARTIAL_CAUSES for part in cause.split("+"))


def is_retainable_partial_cause(cause: str) -> bool:
    """Whether a degraded generation with this cause may be retained at all.

    A composite key is retainable only when EVERY member is: one member that
    would publish a self-contradictory envelope is enough to disqualify the
    whole generation, which is the direction an allowlist has to fail.
    """
    return bool(cause) and all(
        part in RETAINABLE_PARTIAL_CAUSES for part in cause.split("+")
    )


def _validated_state(state: object, provider: object) -> tuple[dict, str]:
    if provider not in _PROVIDERS:
        raise ValueError("provider must be one of ['claude', 'codex']")
    if state is None:
        current: dict = {}
    elif isinstance(state, Mapping):
        current = dict(state)
    else:
        raise ValueError("state must be a mapping or None")
    return current, provider  # type: ignore[return-value]


def _validated_now(now: object) -> dt.datetime:
    if not isinstance(now, dt.datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now


def clear_partial_retry(
    state: PartialRetryState | None, *, provider: str,
) -> PartialRetryState:
    """Drop one provider's key entirely, as a healthy publish must.

    Recovery has to clear the key, not merely stop consulting it. A key left
    armed across a healthy generation would still be inside its deadline when
    the same cause returned, so the first partial after a recovery would be
    RETAINED — the exact permanent-freeze behaviour this kernel exists to end,
    reached by a different route.
    """
    current, provider = _validated_state(state, provider)
    current.pop(provider, None)
    return MappingProxyType(current)


def plan_partial_retry(
    state: PartialRetryState | None,
    *,
    provider: str,
    cause: object,
    data_version: str,
    now: dt.datetime,
    interval: dt.timedelta = PARTIAL_RETRY_INTERVAL,
) -> tuple[PartialRetryDecision, PartialRetryState]:
    """Decide whether a degraded generation is rebuilt or retained this tick.

    Returns ``(decision, next_state)``. The caller's ``state`` is never
    mutated.

    The rules, in order:

    * a cause that is not retainable — a transient read failure, an
      unrecognized code, or one of the causes whose retention would publish a
      self-contradictory envelope — always rebuilds and arms nothing, and
      additionally clears any armed key so a retainable cause interrupted by
      one of them is not throttled by a deadline that kept running while it
      was absent;
    * the FIRST observation of a structural key always rebuilds and arms the
      deadline from this instant;
    * a repeat inside the deadline retains and leaves ``first_seen`` exactly
      where it was, because a deadline re-derived from ``now`` never expires
      while its cause persists;
    * a repeat at or after the deadline rebuilds and re-arms from this
      instant, which starts the next throttle window rather than extending the
      last one.
    """
    current, provider = _validated_state(state, provider)
    now = _validated_now(now)
    if not isinstance(data_version, str):
        raise ValueError("data_version must be a string")
    normalized = normalize_partial_cause(cause)

    if not is_retainable_partial_cause(normalized):
        current.pop(provider, None)
        return (
            PartialRetryDecision(
                action="rebuild",
                cause=normalized,
                data_version=data_version,
                first_seen=None,
                deadline=None,
                throttled=False,
            ),
            MappingProxyType(current),
        )

    prior = current.get(provider)
    if prior is not None and prior.key == (normalized, data_version):
        deadline = prior.deadline(interval)
        if now < deadline:
            return (
                PartialRetryDecision(
                    action="retain",
                    cause=normalized,
                    data_version=data_version,
                    first_seen=prior.first_seen,
                    deadline=deadline,
                    throttled=True,
                ),
                MappingProxyType(current),
            )

    entry = ProviderRetryEntry(
        cause=normalized, data_version=data_version, first_seen=now,
    )
    current[provider] = entry
    return (
        PartialRetryDecision(
            action="rebuild",
            cause=normalized,
            data_version=data_version,
            first_seen=now,
            deadline=entry.deadline(interval),
            throttled=False,
        ),
        MappingProxyType(current),
    )
