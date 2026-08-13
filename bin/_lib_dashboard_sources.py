"""Immutable, privacy-safe source dashboard contracts for #294 S4."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


PhysicalSource = Literal["claude", "codex"]
DashboardSelection = Literal["claude", "codex", "all"]
Availability = Literal["ok", "empty", "partial", "unavailable"]
Freshness = Literal["fresh", "stale"]
FreshnessDomain = Literal["hero", "quota", "sessions"]
CapabilityStatus = Literal[
    "supported", "derived", "unavailable", "deferred", "not_applicable",
]

# #429 §3.2 — bumped to 2 because `active[].captured_at` changed MEANING (the
# newest physical observation, not the interpreted baseline). The in-place
# update flow `execvp`s the server while the already-loaded client reconnects
# over its existing EventSource without reloading its JS
# (`UpdateRunningModal.tsx`, `store/sse.ts`), so an old client demonstrably
# does meet a new server; `docs/cli-contract.md` calls changing a value's
# meaning breaking. Version-aware client reaction is deliberately deferred.
# 2 -> 3 (public #5): the Codex source gained the additive `ingest_backlog`
# field. Additive and omitted-when-zero, so the normal payload is byte-identical
# — but the same `execvp` transition applies, so the bump ships as the signal it
# has always been rather than as a mechanism the client branches on.
# 3 -> 4 (#465): the Codex cache report retired its transitional
# `cache_hit_percent` alias and changed structurally inapplicable figures from
# numeric placeholders to null.
# 4 -> 5 (#556 S1): Claude's `hero.cost_usd` / `hero.total_tokens` changed
# MEANING — they were a thirty-day accounting rollup and are now current-cycle
# actuals, matching what the same-named Codex fields have always meant. The All
# source's `data.combined` also gained a required `legs` object and the optional
# `qualifications` / `combined_unavailable` companions, which supersedes normal-
# payload byte identity for this version (spec §3.5). No client branches on this
# number; after an in-place `execvp` update a still-loaded old client renders the
# new figure under old copy until it reloads, and that one-reconnect transient is
# accepted, consistent with the 2 -> 3 precedent above.
SOURCE_SCHEMA_VERSION = 5
DEFAULT_SOURCE = "claude"
SOURCE_ORDER = ("claude", "codex", "all")
SOURCE_FRESHNESS_DOMAINS = ("hero", "quota", "sessions")

_PHYSICAL_SOURCES = frozenset(("claude", "codex"))
_SELECTIONS = frozenset(SOURCE_ORDER)
_AVAILABILITY = frozenset(("ok", "empty", "partial", "unavailable"))
_FRESHNESS = frozenset(("fresh", "stale"))
_CAPABILITY_STATUSES = frozenset((
    "supported", "derived", "unavailable", "deferred", "not_applicable",
))
_RESOURCE_RE = re.compile(r"[a-z][a-z0-9_]*\Z")


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def validate_physical_source(source: object) -> PhysicalSource:
    """Validate one storage provider; ``all`` never reaches physical layers."""
    if source not in _PHYSICAL_SOURCES:
        raise ValueError("source must be one of ['claude', 'codex']")
    return source  # type: ignore[return-value]


def validate_dashboard_selection(source: object) -> DashboardSelection:
    """Validate a dashboard presentation selection, including ``all``."""
    if source not in _SELECTIONS:
        raise ValueError("source must be one of ['all', 'claude', 'codex']")
    return source  # type: ignore[return-value]


def _freeze(value: object) -> object:
    """Recursively freeze the published, request-thread-readable value tree."""
    if type(value) is MappingProxyType:
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class SourceDashboardWarning:
    """A stable, public-safe provider degradation diagnostic."""

    code: str
    message: str
    domain: str | None = None

    def __post_init__(self) -> None:
        _nonempty_string(self.code, "code")
        _nonempty_string(self.message, "message")
        if self.domain is not None:
            _nonempty_string(self.domain, "domain")


@dataclass(frozen=True)
class CapabilityRecord:
    """Descriptive support state, deliberately not an ambiguous boolean."""

    status: CapabilityStatus
    semantics: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _CAPABILITY_STATUSES:
            raise ValueError("unsupported capability status")
        if self.semantics is not None:
            _nonempty_string(self.semantics, "semantics")


@dataclass(frozen=True)
class SourceDashboardState:
    """One atomically-published source read model."""

    source: DashboardSelection
    availability: Availability
    freshness: Freshness
    warnings: tuple[SourceDashboardWarning, ...]
    data_version: str
    last_success_at: dt.datetime | None
    capabilities: Mapping[str, CapabilityRecord]
    data: Mapping[str, object] | None
    # Domain freshness is orthogonal to provider-generation coherence. Legacy
    # constructors may omit it; they deterministically inherit the provider
    # value for every known domain.
    domain_freshness: Mapping[str, Freshness] | None = None
    # Immutable, server-only facts used to advance an idle presentation clock.
    # They are deliberately separate from ``data`` so no internal accounting
    # evidence becomes part of the public source-envelope contract.
    clock_data: Mapping[str, object] | None = None
    # #556 S1 §3.8 — the provider's authoritative REAL account count, resolved
    # by the builder and carried here so `compose_all_state` can apply the
    # single-account gate. Server-only, in the same class as `clock_data`:
    # deliberately outside `data`, so no account cardinality enters the public
    # source envelope. Shape: ``{"real_account_count": int}``.
    #
    # ``None`` means UNRESOLVED and must fail closed (withhold the combined
    # figure), never "undecorated". Inferring decoration from the published
    # `data.accounts` is forbidden for exactly this reason: both physical
    # builders swallow a decoration-read failure and fall back to the
    # undecorated shape, so a two-account install whose account read failed
    # would present as single-account and publish the one number §3.2 forbids,
    # on precisely the install where it is wrong.
    account_scope: Mapping[str, object] | None = None
    # Request-gated transcript content. This mapping is frozen with the source
    # generation but is deliberately outside ``data``: source serialization
    # publishes only ``data``, then the HTTP/SSE envelope layer injects a label
    # into its request-local copies when that request's transcript gate is open.
    private_session_labels: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        validate_dashboard_selection(self.source)
        if self.availability not in _AVAILABILITY:
            raise ValueError("unsupported availability")
        if self.freshness not in _FRESHNESS:
            raise ValueError("unsupported freshness")
        domain_freshness = (
            {domain: self.freshness for domain in SOURCE_FRESHNESS_DOMAINS}
            if self.domain_freshness is None else dict(self.domain_freshness)
        )
        if set(domain_freshness) != set(SOURCE_FRESHNESS_DOMAINS):
            raise ValueError(
                "domain freshness must contain exactly hero, quota, and sessions"
            )
        if any(value not in _FRESHNESS for value in domain_freshness.values()):
            raise ValueError("unsupported domain freshness")
        if not isinstance(self.data_version, str):
            raise ValueError("data_version must be a string")
        if self.availability != "unavailable":
            _nonempty_string(self.data_version, "data_version")
        if self.last_success_at is not None:
            if self.last_success_at.tzinfo is None or self.last_success_at.utcoffset() is None:
                raise ValueError("last_success_at must be timezone-aware")
            object.__setattr__(
                self, "last_success_at", self.last_success_at.astimezone(dt.timezone.utc),
            )
        warnings = tuple(self.warnings)
        if not all(isinstance(item, SourceDashboardWarning) for item in warnings):
            raise ValueError("warnings must contain SourceDashboardWarning values")
        capabilities = {
            _nonempty_string(name, "capability name"): value
            for name, value in self.capabilities.items()
        }
        if not all(isinstance(value, CapabilityRecord) for value in capabilities.values()):
            raise ValueError("capabilities must contain CapabilityRecord values")
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "capabilities", _freeze(capabilities))
        object.__setattr__(self, "domain_freshness", _freeze(domain_freshness))
        if self.data is not None:
            object.__setattr__(self, "data", _freeze(self.data))
        if self.clock_data is not None:
            object.__setattr__(self, "clock_data", _freeze(self.clock_data))
        if self.account_scope is not None:
            object.__setattr__(self, "account_scope", _freeze(self.account_scope))
        if self.private_session_labels is not None:
            private_session_labels = {
                _nonempty_string(key, "private session label key"):
                _nonempty_string(value, "private session label")
                for key, value in self.private_session_labels.items()
            }
            object.__setattr__(
                self, "private_session_labels", _freeze(private_session_labels),
            )


@dataclass(frozen=True)
class SourceDashboardBundle:
    """The complete source state published once with a dashboard snapshot."""

    source_schema_version: int
    default_source: DashboardSelection
    source_order: tuple[DashboardSelection, ...]
    sources: Mapping[DashboardSelection, SourceDashboardState]

    def __post_init__(self) -> None:
        if self.source_schema_version != SOURCE_SCHEMA_VERSION:
            raise ValueError("unsupported source schema version")
        if self.default_source != DEFAULT_SOURCE:
            raise ValueError("default source must be claude")
        if tuple(self.source_order) != SOURCE_ORDER:
            raise ValueError("source order must be ('claude', 'codex', 'all')")
        sources = dict(self.sources)
        if set(sources) != set(SOURCE_ORDER):
            raise ValueError("sources must contain exactly claude, codex, and all")
        for source, state in sources.items():
            validate_dashboard_selection(source)
            if not isinstance(state, SourceDashboardState) or state.source != source:
                raise ValueError("source state must match its source key")
        object.__setattr__(self, "sources", _freeze(sources))


def _typed_identity_part(value: object) -> object:
    """Return an unambiguous, canonical JSON-safe identity fragment."""
    if value is None:
        return ["null", None]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("identity float must be finite")
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", _nonempty_string(value, "identity part")]
    if isinstance(value, (tuple, list)):
        return ["sequence", [_typed_identity_part(item) for item in value]]
    raise ValueError("identity parts must be typed JSON scalar or sequence values")


def dashboard_resource_key(resource: object, source: object, *identity_parts: object) -> str:
    """Build a non-reversible, provider-qualified resource identifier.

    The digest covers typed values, so e.g. ``1`` cannot collide with ``"1"``.
    Raw roots, native IDs, and compound identity values are never encoded in
    the returned key.
    """
    kind = _nonempty_string(resource, "resource")
    if not _RESOURCE_RE.fullmatch(kind):
        raise ValueError("resource must use lowercase snake-case")
    provider = validate_physical_source(source)
    if not identity_parts:
        raise ValueError("at least one identity part is required")
    canonical = json.dumps(
        {
            "identity": [_typed_identity_part(part) for part in identity_parts],
            "resource": kind,
            "source": provider,
            "version": 1,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(b"cctally-dashboard-resource-v1\0" + canonical).digest()
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{kind}:{token}"


def degrade_source_state(
    prior: SourceDashboardState,
    warning: SourceDashboardWarning,
) -> SourceDashboardState:
    """Retain one complete prior source generation during a transient failure."""
    if not isinstance(prior, SourceDashboardState):
        raise ValueError("prior must be a SourceDashboardState")
    if not isinstance(warning, SourceDashboardWarning):
        raise ValueError("warning must be a SourceDashboardWarning")
    # There must be a coherent prior generation to retain. An unavailable prior
    # carries no data and an empty ``data_version``; degrading it to ``partial``
    # would build an invalid state (the non-empty-data_version invariant only
    # exempts ``unavailable``) and raise. Stay unavailable, carrying the new
    # warning — this is the 2nd+ consecutive failing sync of a degraded provider.
    if prior.availability == "unavailable" or not prior.data_version:
        return unavailable_source_state(prior.source, warning)
    return SourceDashboardState(
        source=prior.source,
        availability="partial",
        freshness="stale",
        warnings=(warning,),
        data_version=prior.data_version,
        last_success_at=prior.last_success_at,
        capabilities=prior.capabilities,
        data=prior.data,
        domain_freshness={
            domain: "stale" for domain in SOURCE_FRESHNESS_DOMAINS
        },
        clock_data=prior.clock_data,
        # #556 S1 §3.8: a degraded generation must not LOSE the count. Dropping
        # it here would turn a transient provider failure into
        # `account_scope_unresolved` on an install whose count read fine.
        account_scope=prior.account_scope,
        private_session_labels=prior.private_session_labels,
    )


def unavailable_source_state(
    source: PhysicalSource,
    warning: SourceDashboardWarning,
) -> SourceDashboardState:
    """Return a safe failure state when no coherent generation exists yet."""
    validate_physical_source(source)
    if not isinstance(warning, SourceDashboardWarning):
        raise ValueError("warning must be a SourceDashboardWarning")
    return SourceDashboardState(
        source=source,
        availability="unavailable",
        freshness="stale",
        warnings=(warning,),
        data_version="",
        last_success_at=None,
        capabilities={},
        data=None,
        domain_freshness={
            domain: "stale" for domain in SOURCE_FRESHNESS_DOMAINS
        },
    )


def source_domain_freshness(
    state: SourceDashboardState,
    domain: FreshnessDomain,
) -> Freshness:
    """Return one domain value with the frozen legacy-provider fallback."""
    if domain not in SOURCE_FRESHNESS_DOMAINS:
        raise ValueError("unsupported freshness domain")
    mapping = getattr(state, "domain_freshness", None)
    if isinstance(mapping, Mapping):
        value = mapping.get(domain)
        if value in _FRESHNESS:
            return value
    provider = getattr(state, "freshness", "stale")
    return provider if provider in _FRESHNESS else "stale"


def _coherent_provider(state: SourceDashboardState) -> bool:
    return (
        state.availability in ("ok", "empty", "partial")
        and state.freshness == "fresh"
        and bool(state.data_version)
        and state.data is not None
    )


def reuse_coherent_source_state(
    prior: SourceDashboardState | None,
    *,
    data_version: str,
) -> SourceDashboardState | None:
    """Return the exact prior object only for an unchanged coherent source.

    A stale/partial object deliberately does not qualify: the next coherent
    rebuild must construct a replacement so it clears the transient warning
    rather than preserving an old degraded generation indefinitely.
    """
    if prior is None:
        return None
    if not isinstance(prior, SourceDashboardState):
        raise ValueError("prior must be a SourceDashboardState or None")
    return prior if _coherent_provider(prior) and prior.data_version == data_version else None


# === #556 S1 — the typed combined outcome (spec §3.5, §3.7) =================
#
# `combined` is the sum, over both providers, of that provider's accounting
# actuals within its OWN current cycle: Claude's subscription week and Codex's
# native 7-day cycle. The two legs are deliberately not one shared range — the
# property bought is that each leg reconciles with its provider tab, and each
# leg therefore names the cycle it covers.

_PROVIDER_LABELS: Mapping[str, str] = MappingProxyType(
    {"claude": "Claude", "codex": "Codex"},
)
_LEG_PERIOD_KINDS: Mapping[str, tuple[str, str, str, str]] = MappingProxyType({
    # provider -> (kind, label, hero container key, (start key, end key))
    "claude": ("subscription_week", "Claude subscription week",
               "current_week", "week_start_at|reset_at_utc"),
    "codex": ("native_7_day_cycle", "Codex native 7-day cycle",
              "cycle", "start_at|resets_at"),
})


@dataclass(frozen=True)
class _CombinedCause:
    """One reason the combined figure is withheld, with its precedence rank."""

    precedence: int
    provider: PhysicalSource
    code: str
    detail: Mapping[str, object] | None = None


def _cause_message(cause: _CombinedCause) -> str:
    """Public-safe prose for one cause. Never echoes a rejected value."""
    provider = _PROVIDER_LABELS.get(cause.provider, cause.provider)
    detail = cause.detail or {}
    if cause.code == "provider_incoherent":
        return (
            f"{provider} data is not current, so a combined total is withheld."
        )
    if cause.code == "account_scope_unresolved":
        return (
            f"{provider}'s account count could not be read, so a combined "
            "total is withheld."
        )
    if cause.code == "multi_account_unsupported":
        count = detail.get("account_count")
        return (
            f"{provider} has {count} accounts on separate cycles, so a "
            "combined total is not published; see the per-account cards."
        )
    if cause.code == "claude_cycle_unresolved":
        return "Claude's current subscription week could not be resolved."
    if cause.code == "codex_projection_incoherent":
        return "Codex quota projection is unavailable."
    if cause.code == "codex_cycle_unavailable":
        return "Codex native reset cycle is unavailable."
    if cause.code == "invalid_counter":
        return (
            f"{provider} reported an unusable {detail.get('field')} counter "
            f"({detail.get('reason')})."
        )
    return f"{provider} data cannot contribute to a combined total."


def _combined_hero(state: SourceDashboardState) -> Mapping[str, object] | None:
    data = state.data
    if not isinstance(data, Mapping):
        return None
    hero = data.get("hero")
    return hero if isinstance(hero, Mapping) else None


def _real_account_count(state: SourceDashboardState) -> int | None:
    """The provider's authoritative REAL account count, or ``None``.

    ``None`` is UNRESOLVED and fails closed (§3.8). Decoration is never
    inferred from published data, because both physical builders swallow a
    decoration-read failure and fall back to the undecorated shape.
    """
    scope = getattr(state, "account_scope", None)
    if not isinstance(scope, Mapping):
        return None
    count = scope.get("real_account_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count


def _counter_reason(value: object, *, integral: bool) -> str | None:
    """Return why ``value`` is unusable as a counter, or ``None`` if it is fine.

    Booleans are rejected: ``True`` is an ``int`` in Python, and a flag that
    leaked into a counter slot would otherwise be summed as 1.
    """
    if value is None:
        return "missing"
    if isinstance(value, bool):
        return "non_integer"
    if integral:
        if not isinstance(value, int):
            return "non_integer"
    else:
        if not isinstance(value, (int, float)):
            return "non_integer"
        if not math.isfinite(value):
            return "non_finite"
    return "negative" if value < 0 else None


def _period_instant(value: object) -> str | None:
    """One canonical UTC spelling for a published period bound.

    The two providers reach this with different spellings of the same
    convention — Claude's bounds arrive from the legacy envelope's `_iso_z`
    (`...Z`) and Codex's from `datetime.isoformat()` (`...+00:00`). Publishing
    both would make every client parse two forms of one field for no reason.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _leg_period(
    provider: PhysicalSource, hero: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    """The named cycle a `current` leg covers, or ``None`` when unresolvable."""
    kind, label, container_key, bound_keys = _LEG_PERIOD_KINDS[provider]
    container = hero.get(container_key) if isinstance(hero, Mapping) else None
    if not isinstance(container, Mapping):
        return None
    start_key, end_key = bound_keys.split("|")
    start_at = _period_instant(container.get(start_key))
    end_at = _period_instant(container.get(end_key))
    if start_at is None or end_at is None:
        return None
    return {
        "kind": kind, "label": label, "start_at": start_at, "end_at": end_at,
    }


_CODEX_HERO_FAILURE_ORDER: tuple[str, ...] = (
    "codex_projection_incoherent", "codex_cycle_unavailable",
)


def _codex_hero_failure_codes(state: SourceDashboardState) -> tuple[str, ...]:
    """EVERY Codex hero failure present, in the order §3.7 fixes.

    Codex computes projection coherence and cycle resolution independently and
    emits BOTH warnings, so §3.5 lists both as causes and §3.7 fixes their
    order: an incoherent projection certificate invalidates the evidence the
    cycle resolution rests on, and is therefore the earlier cause. The first
    element is the winner, so the top-level `code` is unchanged by listing the
    rest.
    """
    codes = {warning.code for warning in state.warnings}
    found = tuple(code for code in _CODEX_HERO_FAILURE_ORDER if code in codes)
    if found:
        return found
    # No warning names the failure — fall back to the hero capability's own
    # semantics, which can only describe one of the two.
    capability = state.capabilities.get("hero")
    semantics = capability.semantics if capability is not None else None
    if semantics == "projection-incoherent":
        return ("codex_projection_incoherent",)
    return ("codex_cycle_unavailable",)


def _combined_leg(
    state: SourceDashboardState, provider: PhysicalSource,
) -> tuple[Mapping[str, object] | None, tuple[_CombinedCause, ...]]:
    """Build one leg, or the causes that stop it contributing.

    A leg is `empty` when the provider reports no accounting AND no cycle:
    `availability == "empty"` and no period resolves. Both halves are needed.
    `availability` alone is not the fact, because it is computed over the
    dashboard's VISIBLE range while Codex's hero is a separate cycle-bounded
    read — a Codex provider can be `empty` in the visible range while its
    current cycle holds real spend. An unresolved period alone is not the fact
    either, because for Claude that is a FAILURE when accounting exists
    (§3.7, "empty versus unresolved").
    """
    hero = _combined_hero(state)
    capability = state.capabilities.get("hero")
    if capability is None or capability.status not in {"supported", "derived"}:
        codes = (
            _codex_hero_failure_codes(state) if provider == "codex"
            else ("claude_cycle_unresolved",)
        )
        return None, tuple(
            _CombinedCause(4, provider, code) for code in codes
        )
    cost = hero.get("cost_usd") if hero is not None else None
    tokens = hero.get("total_tokens") if hero is not None else None
    period = _leg_period(provider, hero)
    if period is None and state.availability == "empty":
        # Numeric zeros and no period, so nothing presents `$0` as observed
        # spend inside a named cycle.
        return {"state": "empty", "cost_usd": 0.0, "total_tokens": 0}, ()
    if provider == "claude" and cost is None and tokens is None:
        # Accounting exists but no subscription week resolved. Claude's hero
        # capability stays `supported` in that state, so this is its own
        # detection rather than the capability branch above.
        return None, (_CombinedCause(4, provider, "claude_cycle_unresolved"),)
    causes = tuple(
        _CombinedCause(
            5, provider, "invalid_counter", {"field": field, "reason": reason},
        )
        for field, reason in (
            ("cost_usd", _counter_reason(cost, integral=False)),
            ("total_tokens", _counter_reason(tokens, integral=True)),
        )
        if reason is not None
    )
    if causes:
        return None, causes
    return {
        "state": "current",
        "cost_usd": float(cost),
        "total_tokens": int(tokens),
        **({"period": period} if period is not None else {}),
    }, ()


def _combined_qualifications(
    legs: Mapping[str, Mapping[str, object]],
    claude: SourceDashboardState,
    codex: SourceDashboardState,
) -> tuple[Mapping[str, object], ...]:
    """Notes that qualify a PUBLISHED figure (§4.3). Empty means omit the key."""
    qualifications: list[Mapping[str, object]] = []
    for provider in ("claude", "codex"):
        if legs[provider].get("state") == "empty":
            label = _PROVIDER_LABELS[provider]
            qualifications.append({
                "code": "provider_empty",
                "message": f"{label} has no accounting in its current cycle.",
                "provider": provider,
            })
    # public #5: the Codex ingest backlog is LIFTED here rather than read from
    # the provider field by the All surfaces, so the figure and its disclosure
    # cannot disagree. The provider field stays published for the Codex tab.
    data = codex.data
    backlog = data.get("ingest_backlog") if isinstance(data, Mapping) else None
    if isinstance(backlog, Mapping) and backlog:
        qualifications.append({
            "code": "codex_ingest_backlog",
            "message": (
                "Codex has pending accounting to ingest, so its cycle total "
                "may be incomplete."
            ),
            "provider": "codex",
        })
    return tuple(qualifications)


def _combined_outcome(
    claude: SourceDashboardState,
    codex: SourceDashboardState,
) -> tuple[Mapping[str, object] | None, Mapping[str, object] | None]:
    """Return ``(combined, combined_unavailable)`` — exactly one is not None.

    Cause precedence (§3.7), first match wins, Claude before Codex at equal
    precedence: provider incoherence, unresolved account scope, decoration, a
    hero capability outside {supported, derived} reported as the provider's own
    reason, then an invalid counter. Every cause found is listed, ordered so
    that `causes[0]` is always the winner.
    """
    pairs: tuple[tuple[PhysicalSource, SourceDashboardState], ...] = (
        ("claude", claude), ("codex", codex),
    )
    causes: list[_CombinedCause] = []
    for provider, state in pairs:
        if not _coherent_provider(state):
            causes.append(_CombinedCause(1, provider, "provider_incoherent"))
    for provider, state in pairs:
        count = _real_account_count(state)
        if count is None:
            causes.append(
                _CombinedCause(2, provider, "account_scope_unresolved"))
        elif count > 1:
            causes.append(_CombinedCause(
                3, provider, "multi_account_unsupported",
                {"account_count": count},
            ))
    legs: dict[str, Mapping[str, object]] = {}
    for provider, state in pairs:
        if not _coherent_provider(state):
            # An incoherent generation's data cannot be trusted to yield a leg
            # OR a leg-level cause; precedence 1 already withholds the figure.
            continue
        leg, leg_causes = _combined_leg(state, provider)
        causes.extend(leg_causes)
        if leg is not None:
            legs[provider] = leg
    if causes:
        ordered = sorted(
            causes,
            key=lambda cause: (
                cause.precedence, 0 if cause.provider == "claude" else 1,
            ),
        )
        return None, {
            "code": ordered[0].code,
            "message": _cause_message(ordered[0]),
            "causes": tuple(
                {
                    "provider": cause.provider,
                    "code": cause.code,
                    **({"detail": cause.detail} if cause.detail else {}),
                }
                for cause in ordered
            ),
        }
    qualifications = _combined_qualifications(legs, claude, codex)
    return {
        "cost_usd": float(legs["claude"]["cost_usd"]) + float(legs["codex"]["cost_usd"]),
        "total_tokens": int(legs["claude"]["total_tokens"]) + int(legs["codex"]["total_tokens"]),
        "legs": legs,
        **({"qualifications": qualifications} if qualifications else {}),
    }, None


def _combined_alert_rows(
    claude: SourceDashboardState,
    codex: SourceDashboardState,
) -> tuple[Mapping[str, object], ...]:
    """Merge only provider-owned public alert rows with stable tie breaking."""
    ordered: list[Mapping[str, object]] = []
    for source, state in (("claude", claude), ("codex", codex)):
        if not isinstance(state.data, Mapping):
            continue
        alerts = state.data.get("alerts")
        rows = alerts.get("rows") if isinstance(alerts, Mapping) else None
        if not isinstance(rows, (tuple, list)):
            continue
        for row in rows:
            if not isinstance(row, Mapping) or row.get("source") != source:
                continue
            ordered.append(row)
    # Python's stable sort preserves declared source order, then each source's
    # native order, when alert timestamps tie.
    return tuple(sorted(
        ordered,
        key=lambda row: str(row.get("created_at") or ""),
        reverse=True,
    ))


def compose_all_state(
    claude: SourceDashboardState,
    codex: SourceDashboardState,
) -> SourceDashboardState:
    """Compose provider-labeled sections without inventing blended semantics."""
    if claude.source != "claude" or codex.source != "codex":
        raise ValueError("all composition requires Claude and Codex provider states")
    combined, combined_unavailable = _combined_outcome(claude, codex)
    providers_coherent = _coherent_provider(claude) and _coherent_provider(codex)
    if providers_coherent:
        availability: Availability = (
            "partial"
            if combined is None or "partial" in (claude.availability, codex.availability)
            else (
                "empty"
                if claude.availability == "empty" and codex.availability == "empty"
                else "ok"
            )
        )
        freshness: Freshness = "fresh"
    else:
        availability = "partial"
        freshness = "stale"
    version_material = json.dumps(
        [
            claude.data_version, claude.availability, claude.freshness,
            [
                source_domain_freshness(claude, domain)
                for domain in SOURCE_FRESHNESS_DOMAINS
            ],
            codex.data_version, codex.availability, codex.freshness,
            [
                source_domain_freshness(codex, domain)
                for domain in SOURCE_FRESHNESS_DOMAINS
            ],
            # #556 S1: the WHOLE outcome, not merely whether one exists. The
            # legs, their periods, the qualifications and the withheld cause
            # are all published, and the account scope and hero counters that
            # produce them are not otherwise in this material — so hashing only
            # `combined is not None` would leave materially different All
            # states sharing one `data_version` (invariant 6).
            combined, combined_unavailable,
        ],
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    data_version = "all:" + hashlib.sha256(version_material).hexdigest()[:24]
    successes = (claude.last_success_at, codex.last_success_at)
    # §4.6: `None` unless BOTH providers have one, otherwise the older.
    # Filtering `None` before `min` let one provider's success masquerade as
    # All's while the client keys "no successful snapshot yet" on null.
    last_success_at = None if None in successes else min(successes)
    return SourceDashboardState(
        source="all",
        availability=availability,
        freshness=freshness,
        # §4.2: the All-local `combined_totals_stale` warning is retired. The
        # combined figure's own disclosure now travels in `data.combined`
        # (`qualifications`) and `data.combined_unavailable`, which is typed and
        # has provenance — All flattens both providers' warnings into one tuple
        # with no provenance field, so warning order could never carry it.
        warnings=tuple((*claude.warnings, *codex.warnings)),
        data_version=data_version,
        last_success_at=last_success_at,
        capabilities={
            "hero": CapabilityRecord("derived", "compatible-provider-totals"),
            "quota": CapabilityRecord("not_applicable", "provider-native"),
            "budget": CapabilityRecord("not_applicable", "provider-native"),
            "alerts": CapabilityRecord("derived", "provider-native-union"),
        },
        data={
            "combined": combined,
            # Emitted iff the figure is withheld; omitted-when-inapplicable.
            **({"combined_unavailable": combined_unavailable}
               if combined is None else {}),
            "alerts": {"rows": _combined_alert_rows(claude, codex)},
            "providers": {
                "claude": claude.data,
                "codex": codex.data,
            },
        },
        domain_freshness={
            domain: (
                "fresh"
                if all(
                    source_domain_freshness(state, domain) == "fresh"
                    for state in (claude, codex)
                )
                else "stale"
            )
            for domain in SOURCE_FRESHNESS_DOMAINS
        },
    )


@dataclass(frozen=True)
class ProjectionCoherence:
    """Typed result for a Codex physical-to-projection coherence check."""

    coherent: bool
    reason: str | None = None


# The column order is the approved cross-database identity contract.  Keep the
# relation sequence and tuples fixed: neither SQLite insertion order nor
# surrogate/provenance/reconciliation-only fields may perturb the digest.
_CODEX_STATS_DIGEST_RELATIONS: tuple[tuple[str, str], ...] = (
    (
        "quota_projection_state",
        "SELECT source_root_key, physical_signature "
        "FROM quota_projection_state "
        "ORDER BY source_root_key, physical_signature",
    ),
    (
        "quota_window_blocks",
        "SELECT source, source_root_key, logical_limit_key, observed_slot, "
        "window_minutes, limit_id, limit_name, resets_at_utc, nominal_start_at_utc, "
        "first_observed_at_utc, last_observed_at_utc, first_percent, current_percent, "
        "orphaned_at FROM quota_window_blocks WHERE source='codex' "
        "ORDER BY source, source_root_key, logical_limit_key, observed_slot, "
        "window_minutes, limit_id, limit_name, resets_at_utc, nominal_start_at_utc, "
        "first_observed_at_utc, last_observed_at_utc, first_percent, current_percent, orphaned_at",
    ),
    (
        "quota_percent_milestones",
        "SELECT source, source_root_key, logical_limit_key, observed_slot, "
        "window_minutes, resets_at_utc, percent_threshold, captured_at_utc, "
        "high_water_percent, orphaned_at FROM quota_percent_milestones "
        "WHERE source='codex' ORDER BY source, source_root_key, logical_limit_key, "
        "observed_slot, window_minutes, resets_at_utc, percent_threshold, captured_at_utc, "
        "high_water_percent, orphaned_at",
    ),
    (
        "quota_threshold_events",
        "SELECT source, source_root_key, logical_limit_key, observed_slot, "
        "window_minutes, resets_at_utc, threshold, qualifying_kind, qualifying_percent, "
        "projected_percent, severity, created_at_utc, disposition, alerted_at, suppressed_at, "
        "orphaned_at FROM quota_threshold_events WHERE source='codex' "
        "ORDER BY source, source_root_key, logical_limit_key, observed_slot, window_minutes, "
        "resets_at_utc, threshold, qualifying_kind, qualifying_percent, projected_percent, "
        "severity, created_at_utc, disposition, alerted_at, suppressed_at, orphaned_at",
    ),
    (
        "budget_milestones",
        "SELECT vendor, period_start_at, period, threshold, budget_usd, spent_usd, "
        "consumption_pct, crossed_at_utc, alerted_at FROM budget_milestones "
        "WHERE vendor='codex' ORDER BY vendor, period_start_at, period, threshold, "
        "budget_usd, spent_usd, consumption_pct, crossed_at_utc, alerted_at",
    ),
    (
        "projected_milestones",
        "SELECT week_start_at, period, metric, threshold, projected_value, denominator, "
        "crossed_at_utc, alerted_at FROM projected_milestones "
        "WHERE metric='codex_budget_usd' ORDER BY week_start_at, period, metric, threshold, "
        "projected_value, denominator, crossed_at_utc, alerted_at",
    ),
)


def codex_stats_digest(stats_conn: sqlite3.Connection) -> str:
    """Hash exact, canonically ordered Codex-derived stats relations.

    A missing table is an empty relation so an older/fresh stats database has a
    stable digest. Other SQLite failures remain visible to the builder, which
    then follows the source all-or-prior failure matrix instead of publishing a
    guessed identity.
    """
    relations: list[list[list[object]]] = []
    for _name, query in _CODEX_STATS_DIGEST_RELATIONS:
        try:
            rows = stats_conn.execute(query).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            rows = ()
        relations.append([list(row) for row in rows])
    canonical = json.dumps(
        relations,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def assess_codex_projection_coherence(
    *,
    active_root_keys: tuple[str, ...] | list[str] | set[str],
    physical_signatures: Mapping[str, str],
    projection_signatures: Mapping[str, str],
) -> ProjectionCoherence:
    """Require a complete, exact physical-signature match for every root."""
    for root_key in sorted(active_root_keys):
        physical = physical_signatures.get(root_key)
        if physical is None:
            return ProjectionCoherence(False, "missing_physical_signature")
        projection = projection_signatures.get(root_key)
        if projection is None:
            return ProjectionCoherence(False, "missing_projection_state")
        if physical != projection:
            return ProjectionCoherence(False, "physical_signature_mismatch")
    return ProjectionCoherence(True)
