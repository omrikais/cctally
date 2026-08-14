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
# 5 -> 6 (#556 S2): the All source gained a required `aggregates` object —
# one `range` describing the shared absolute interval every cross-provider
# ranking covers, plus a typed `available`/`withheld` outcome for Projects and
# for Daily. Two rows-only siblings appeared beside it on the Claude provider
# domain, `projects.aggregate` and `periods.daily_aggregate`, whose rows are
# folded over that same interval. Because those fields are REQUIRED on a v6
# payload, this supersedes normal-payload byte identity for this version,
# exactly as S1's `legs` object did for v5; the additive-omission discipline
# continues to govern every genuinely optional field. Nothing existing changed
# shape: `projects.current_week`, `projects.trend`, the flat route-lookup
# `projects.rows` and `periods.daily` are untouched. No client branches on this
# number — the bump ships as the signal it has always been, which is exactly
# why the wire change is additive: after an in-place `execvp` update a
# still-loaded old client renders precisely what it renders today until it
# reloads, and no forced page reload is required.
# 6 -> 7 (#556 S3): every Codex alert row gained `alerted_at`, the canonical
# firing instant, and `created_at` became an equal-valued compatibility alias
# for it rather than the crossing instant it used to carry. The All source's
# alert union is ordered by that instant across both providers instead of by a
# field only one of them wrote. `alerted_at` is additive and `created_at`
# remains present, so a pre-v7 client reading `created_at` keeps working — but
# the VALUE it reads changed on two of the three Codex legs, which is why this
# is a version bump and not a silent addition.
SOURCE_SCHEMA_VERSION = 7
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
    # #556 S2 §3.6 — the per-aggregate carrier. Server-only, in the same class
    # as ``clock_data`` and ``account_scope``: the resolved shared range never
    # enters a provider ``data`` domain, because composition embeds each
    # provider's ``data`` under the All source and a range inside it would be
    # published three times over. Shape:
    #
    #   {"range": {"kind", "label", "start_at", "end_at"},
    #    "projects": {"state": "ok"} | {"state": "failed", "code": ...},
    #    "daily":    {"state": "ok"} | {"state": "failed", "code": ...}}
    #
    # ``account_scope`` is the right precedent for STORAGE CLASS and the wrong
    # one for LIFECYCLE. Account scope is deliberately reattached from the
    # current tick after every build, reuse and degrade branch; doing that here
    # would overwrite the range that describes RETAINED rows with the range of a
    # tick that produced none. This carrier therefore travels with the rows it
    # describes and is never re-derived on a reuse or degrade path. Explicit
    # constructors must copy it.
    aggregate_scope: Mapping[str, object] | None = None

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
        if self.aggregate_scope is not None:
            object.__setattr__(
                self, "aggregate_scope", _freeze(self.aggregate_scope),
            )
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
        # #556 S2 §3.6: the carrier travels with the rows it describes. A
        # degraded generation retains `prior.data`, so it must retain the range
        # that bounded those rows — re-deriving it from the current tick would
        # publish a range the retained rows do not cover.
        aggregate_scope=prior.aggregate_scope,
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


def canonical_alerted_at(value: object) -> str:
    """Normalize an aware ISO-8601 firing instant to one UTC ``Z`` spelling.

    #556 S3 §2.2. The union sorted on a field one writer never wrote, so a
    missing or malformed value must raise here rather than degrade to a
    sentinel that sorts silently. Sub-second precision is truncated, so two
    alerts firing in the same second compare equal and fall back to source
    order; no writer emits it today.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"alerted_at must be a non-empty ISO-8601 string, got {value!r}")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"unparseable alerted_at {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"naive alerted_at {value!r}; an aware instant is required")
    return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_alerted_at_sql(column: str = "alerted_at") -> str:
    """The SQL twin of :func:`canonical_alerted_at`, for ordering in SQLite.

    #556 S3 §2.3. Every per-axis ``LIMIT`` decides MEMBERSHIP, not merely
    order, so a row excluded by a textual comparison of two spellings of one
    instant cannot be recovered by any later projection. SQLite parses the
    timezone indicator, so for every aware spelling this returns the same
    canonical UTC ``Z`` string the Python helper returns —
    ``tests/test_556_s3_alert_ordering.py`` pins that agreement over the
    committed estate. The twins diverge on exactly one input: SQLite reads a
    naive value as UTC where the Python helper raises, so such a row is ordered
    here and rejected later. An unparseable value yields SQL ``NULL``, which
    sorts last under ``DESC``, so the ``LIMIT`` usually drops that row before
    composition can reject it — corruption is truncated away, not surfaced.
    """
    return f"strftime('%Y-%m-%dT%H:%M:%SZ', {column})"


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


# === #556 S2 — the shared cross-provider aggregates (spec §3.5.1, §3.7) =====
#
# Two range rules coexist. A combined TOTAL sums provider-native cycles (S1,
# above). A cross-provider RANKING uses one shared absolute calendar range, and
# that is what these aggregates publish. Both are deliberate.
#
# Withholding is a typed outcome rather than an empty list, because an empty
# list renders as honest emptiness and a range problem is not emptiness.

AGGREGATE_NAMES: tuple[str, ...] = ("projects", "daily")
AGGREGATE_RANGE_KIND = "absolute_range"
AGGREGATE_RANGE_LABEL = "Shared range"

# Precedence is TOTAL and follows the declared code order (§3.5.1). The first
# two are ordered so the predicates stay mutually exclusive: `_coherent_provider`
# already subsumes unavailability, so testing incoherence first would make
# `provider_unavailable` unreachable.
_AGGREGATE_CAUSE_RANK: Mapping[str, int] = MappingProxyType({
    "range_unresolved": 1,
    "provider_unavailable": 2,
    "provider_incoherent": 3,
    "claude_fold_failed": 4,
    "retained_range_mismatch": 5,
})

# One server warning maps to one published qualification. The figure stays
# AVAILABLE: the server already publishes a qualified Codex projects subset in
# this state, and withholding the whole ranking over it would discard real data.
_AGGREGATE_QUALIFYING_WARNINGS: Mapping[str, tuple[str, str]] = MappingProxyType({
    # warning code -> (published qualification code, aggregate it qualifies)
    "codex_metadata_incomplete": ("codex_project_metadata_partial", "projects"),
})


def aggregate_range(start_at: object, end_at: object) -> dict | None:
    """Canonicalise one resolved absolute range, or ``None`` if it does not."""
    start = _period_instant(start_at)
    end = _period_instant(end_at)
    if start is None or end is None:
        return None
    return {
        "kind": AGGREGATE_RANGE_KIND,
        "label": AGGREGATE_RANGE_LABEL,
        "start_at": start,
        "end_at": end,
    }


def build_aggregate_scope(
    published_range: Mapping[str, object] | None,
    outcomes: Mapping[str, object] | None = None,
) -> dict:
    """The server-only carrier a freshly built provider generation gets."""
    scope: dict = {"range": dict(published_range) if published_range else None}
    for name in AGGREGATE_NAMES:
        entry = (outcomes or {}).get(name)
        scope[name] = dict(entry) if isinstance(entry, Mapping) else {"state": "ok"}
    return scope


def aggregate_scope_failed(value: object) -> bool:
    """Whether a provider generation records a failed aggregate fold.

    Accepts either a ``SourceDashboardState`` or a raw carrier mapping, because
    both gates need the predicate and one of them runs before the state exists.

    A failure must not become permanent. A locally caught fold failure leaves an
    otherwise `ok` and `fresh` provider, and that bundle would qualify for idle
    reuse while exact-version provider reuse returns the prior object unchanged
    — so one transient failure would withhold the aggregate for the life of the
    process. This predicate is read at BOTH gates.
    """
    scope = (
        value if isinstance(value, Mapping)
        else getattr(value, "aggregate_scope", None)
    )
    if not isinstance(scope, Mapping):
        return False
    for name in AGGREGATE_NAMES:
        entry = scope.get(name)
        if isinstance(entry, Mapping) and entry.get("state") != "ok":
            return True
    return False


def aggregate_scope_identity(scope: object) -> str:
    """The version fragment a provider's aggregate carrier contributes.

    Carries the resolved range START and the per-aggregate outcome, so a failed
    and a successful fold over the same database signature can never publish
    different rows under one ``data_version``.

    ``end_at`` is deliberately EXCLUDED. It is ``now_utc``, which advances on
    every tick by construction, so folding it in would make every provider
    version unique per tick and defeat `reuse_coherent_source_state` on every
    path — including the reuse §3.6 itself reasons about. The START is the bound
    that can actually move (a display-day rollover), and it is folded into BOTH
    providers' versions so they rebuild in lockstep and a coherent pair can
    never disagree about it.

    The start participates as the EXACT canonical instant, at the same
    granularity `compose_all_aggregates` compares it. That is the point: the
    composition publishes a range only when every coherent provider's canonical
    ``start_at`` is the same string, and this identity is what forces the two
    providers to rebuild in lockstep so they can be. A coarser identity would
    make a difference the composition rejects invisible to the gate that is
    supposed to resolve it — an unchanged provider would keep reusing the old
    carrier while a rebuilt one recorded the new instant, and both aggregates
    would be withheld as ``retained_range_mismatch`` on every subsequent tick.
    That is the original defect, and one value read at two granularities is its
    structural shape.

    An earlier revision folded the start at DAY granularity to protect against
    a ``now_utc - 30 days`` fallback that advanced on every tick. That fallback
    is gone: every producer of this bound now floors to display-timezone
    midnight — `resolve_shared_range` on both its branches, and
    `_tui_build_source_bundle`'s own fallback, which resolves through the same
    helper. So the exact instant changes at most once per display day, which is
    a tick that must rebuild anyway because the daily panel rolled over.

    ``end_at`` is the only value still excluded, for the reason above.
    """
    if not isinstance(scope, Mapping):
        return "none"
    published = scope.get("range")
    start = (
        published.get("start_at") if isinstance(published, Mapping) else None
    )
    parts = [str(start or "")]
    for name in AGGREGATE_NAMES:
        entry = scope.get(name)
        state = entry.get("state") if isinstance(entry, Mapping) else None
        code = entry.get("code") if isinstance(entry, Mapping) else None
        parts.append(f"{name}:{state or 'unknown'}" + (f":{code}" if code else ""))
    return "|".join(parts)


def _aggregate_scope_range(state: SourceDashboardState) -> dict | None:
    scope = getattr(state, "aggregate_scope", None)
    if not isinstance(scope, Mapping):
        return None
    published = scope.get("range")
    if not isinstance(published, Mapping):
        return None
    return aggregate_range(published.get("start_at"), published.get("end_at"))


def _aggregate_fold_failed(state: SourceDashboardState, name: str) -> bool:
    scope = getattr(state, "aggregate_scope", None)
    if not isinstance(scope, Mapping):
        return False
    entry = scope.get(name)
    return isinstance(entry, Mapping) and entry.get("state") != "ok"


def _aggregate_qualifications(
    claude: SourceDashboardState, codex: SourceDashboardState, name: str,
) -> list[dict]:
    """Notes that qualify a PUBLISHED aggregate. Empty means omit the key."""
    qualifications: list[dict] = []
    for provider, state in (("claude", claude), ("codex", codex)):
        for warning in state.warnings:
            mapped = _AGGREGATE_QUALIFYING_WARNINGS.get(warning.code)
            if mapped is not None and mapped[1] == name:
                qualifications.append(
                    {"code": mapped[0], "provider": provider},
                )
    return qualifications


def compose_all_aggregates(
    claude: SourceDashboardState, codex: SourceDashboardState,
) -> dict:
    """The single public ``sources.all.data.aggregates`` object (§3.5.1).

    The outcome carries STATE AND REASON only; the rows live on the provider
    domains and the client composes them. That is what keeps exactly one public
    copy of the range and one public copy of the rows.

    Every cause is evaluated per aggregate, so a Projects fold failure cannot
    withhold Daily.
    """
    pairs: tuple[tuple[PhysicalSource, SourceDashboardState], ...] = (
        ("claude", claude), ("codex", codex),
    )
    coherent = [
        (provider, state) for provider, state in pairs
        if _coherent_provider(state)
    ]
    ranges = {
        provider: _aggregate_scope_range(state) for provider, state in coherent
    }
    resolved = [value for value in ranges.values() if value is not None]
    starts = {value["start_at"] for value in resolved}

    shared: list[tuple[int, int, str, PhysicalSource | None]] = []
    if coherent and len(resolved) != len(coherent):
        # A coherent provider whose rows are not bounded by a known range.
        shared.append((_AGGREGATE_CAUSE_RANK["range_unresolved"], 0,
                       "range_unresolved", None))
    for rank_provider, (provider, state) in enumerate(pairs):
        if state.availability == "unavailable":
            shared.append((_AGGREGATE_CAUSE_RANK["provider_unavailable"],
                           rank_provider, "provider_unavailable", provider))
    for rank_provider, (provider, state) in enumerate(pairs):
        if state.availability != "unavailable" and not _coherent_provider(state):
            shared.append((_AGGREGATE_CAUSE_RANK["provider_incoherent"],
                           rank_provider, "provider_incoherent", provider))
    if len(starts) > 1:
        shared.append((_AGGREGATE_CAUSE_RANK["retained_range_mismatch"], 0,
                       "retained_range_mismatch", None))

    published_range: dict | None = None
    if coherent and len(resolved) == len(coherent) and len(starts) == 1:
        # Published only when EVERY coherent provider supplied a range and they
        # agree. Publishing one leg's range while the other's is unresolved or
        # different would state a span the composed rows do not cover.
        #
        # A reused provider provably has no new accounting rows — its physical
        # signature is part of the version that made the reuse legal — so the
        # later of the two ends is the instant BOTH legs are complete to.
        published_range = {
            **resolved[0],
            "end_at": max(value["end_at"] for value in resolved),
        }

    aggregates: dict = {"range": published_range}
    for name in AGGREGATE_NAMES:
        causes = list(shared)
        if _aggregate_fold_failed(claude, name):
            causes.append((_AGGREGATE_CAUSE_RANK["claude_fold_failed"], 0,
                           "claude_fold_failed", "claude"))
        if causes:
            _rank, _provider_rank, code, provider = min(
                causes, key=lambda cause: (cause[0], cause[1]),
            )
            aggregates[name] = {
                "state": "withheld",
                "code": code,
                **({"provider": provider} if provider is not None else {}),
            }
            continue
        qualifications = _aggregate_qualifications(claude, codex, name)
        aggregates[name] = {
            "state": "available",
            **({"qualifications": qualifications} if qualifications else {}),
        }
    return aggregates


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

    def _instant(row: Mapping[str, object]) -> str:
        try:
            return canonical_alerted_at(row.get("alerted_at"))
        except ValueError as exc:
            identity = row.get("id") if row.get("id") is not None else row.get("key")
            raise ValueError(
                f"{row.get('source')!r} alert row {identity!r}: {exc}"
            ) from exc

    # #556 S3 §2.5: composition is the chokepoint. The previous sort keyed on
    # `created_at`, which the Claude projection never wrote, so every Claude
    # row collapsed to "" and sorted last. Validating here means a future leg,
    # axis or provider that omits the canonical instant fails visibly instead.
    # Python's stable sort preserves declared source order, then each source's
    # native order, when firing instants tie.
    return tuple(sorted(ordered, key=_instant, reverse=True))


def compose_all_state(
    claude: SourceDashboardState,
    codex: SourceDashboardState,
) -> SourceDashboardState:
    """Compose provider-labeled sections without inventing blended semantics."""
    if claude.source != "claude" or codex.source != "codex":
        raise ValueError("all composition requires Claude and Codex provider states")
    combined, combined_unavailable = _combined_outcome(claude, codex)
    aggregates = compose_all_aggregates(claude, codex)
    # Computed ONCE: the same ordered union is hashed into the version below
    # and published in `data` further down, so the identity and the rows can
    # never describe different orderings.
    combined_alerts = _combined_alert_rows(claude, codex)
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
            # #556 S2 §3.6: the COMPLETE `AllAggregates` value, not merely the
            # range. A failed and a successful fold over the same database
            # signature and the same bounds publish different rows, so hashing
            # only the range would leave them sharing one `data_version`.
            aggregates,
            # #556 S3 §2.9: the ordered alert union's identity. Without it the
            # version material omitted alerts entirely, so two materially
            # different unions — a different order, a different membership, a
            # newly fired alert — collided on one `data_version`.
            [
                (str(row.get("source")), str(row.get("id") or row.get("key")),
                 canonical_alerted_at(row.get("alerted_at")))
                for row in combined_alerts
            ],
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
            "alerts": {"rows": combined_alerts},
            # #556 S2 §3.5.1: the ONE public copy of the shared range and of
            # both aggregate outcomes. The rows stay on the provider domains
            # under `providers` below, so nothing is published twice.
            "aggregates": aggregates,
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


# #556 S3 §2.9. The Claude twin of the relation table above, over the five
# alert tables the Claude projection reads. `codex_stats_digest` already covers
# Codex's alert rows; Claude's were covered by nothing, and the dispatch
# signature's stats legs are `MAX(id)` over the two weekly snapshot tables plus
# the reset-event change signal — none of which a milestone INSERT or an
# `alerted_at` arming UPDATE touches. A fired Claude alert could therefore
# leave the idle path short-circuiting on a retained prior bundle. Measured
# before the leg was added: inserting a `budget_milestones` row with
# `vendor='claude'` left every existing leg byte-identical.
#
# Only the alert-bearing columns are selected, for the same reason the Codex
# table selects a fixed list: the digest is an identity over what the surface
# publishes, not a checksum of the table file.
_CLAUDE_STATS_DIGEST_RELATIONS: tuple[tuple[str, str], ...] = (
    (
        "percent_milestones",
        "SELECT week_start_date, percent_threshold, captured_at_utc, "
        "cumulative_cost_usd, reset_event_id, account_key, alerted_at "
        "FROM percent_milestones WHERE alerted_at IS NOT NULL "
        "ORDER BY week_start_date, percent_threshold, reset_event_id, account_key, "
        "captured_at_utc, cumulative_cost_usd, alerted_at",
    ),
    (
        "five_hour_milestones",
        "SELECT five_hour_window_key, percent_threshold, captured_at_utc, "
        "block_cost_usd, reset_event_id, account_key, alerted_at "
        "FROM five_hour_milestones WHERE alerted_at IS NOT NULL "
        "ORDER BY five_hour_window_key, percent_threshold, reset_event_id, account_key, "
        "captured_at_utc, block_cost_usd, alerted_at",
    ),
    (
        "budget_milestones",
        "SELECT vendor, period_start_at, period, threshold, budget_usd, spent_usd, "
        "consumption_pct, crossed_at_utc, account_key, alerted_at "
        "FROM budget_milestones WHERE vendor <> 'codex' AND alerted_at IS NOT NULL "
        "ORDER BY vendor, period_start_at, period, threshold, account_key, "
        "budget_usd, spent_usd, consumption_pct, crossed_at_utc, alerted_at",
    ),
    (
        "projected_milestones",
        "SELECT week_start_at, period, metric, threshold, projected_value, denominator, "
        "crossed_at_utc, account_key, alerted_at FROM projected_milestones "
        "WHERE metric <> 'codex_budget_usd' AND alerted_at IS NOT NULL "
        "ORDER BY week_start_at, period, metric, threshold, account_key, "
        "projected_value, denominator, crossed_at_utc, alerted_at",
    ),
    (
        "project_budget_milestones",
        "SELECT week_start_at, project_key, threshold, budget_usd, spent_usd, "
        "consumption_pct, crossed_at_utc, account_key, alerted_at "
        "FROM project_budget_milestones WHERE alerted_at IS NOT NULL "
        "ORDER BY week_start_at, project_key, threshold, account_key, "
        "budget_usd, spent_usd, consumption_pct, crossed_at_utc, alerted_at",
    ),
)


def _stats_relations_digest(
    stats_conn: sqlite3.Connection,
    relations: tuple[tuple[str, str], ...],
) -> str:
    relation_rows: list[list[list[object]]] = []
    for _name, query in relations:
        try:
            rows = stats_conn.execute(query).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            rows = ()
        relation_rows.append([list(row) for row in rows])
    canonical = json.dumps(
        relation_rows,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def claude_stats_digest(stats_conn: sqlite3.Connection) -> str:
    """Hash the Claude-owned alert relations, canonically ordered.

    A missing table is an empty relation, so an older or fresh stats database
    still has a stable digest — the same posture ``codex_stats_digest`` takes.
    """
    return _stats_relations_digest(stats_conn, _CLAUDE_STATS_DIGEST_RELATIONS)


def codex_stats_digest(stats_conn: sqlite3.Connection) -> str:
    """Hash exact, canonically ordered Codex-derived stats relations.

    A missing table is an empty relation so an older/fresh stats database has a
    stable digest. Other SQLite failures remain visible to the builder, which
    then follows the source all-or-prior failure matrix instead of publishing a
    guessed identity.
    """
    return _stats_relations_digest(stats_conn, _CODEX_STATS_DIGEST_RELATIONS)


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
