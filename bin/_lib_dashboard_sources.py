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
CapabilityStatus = Literal[
    "supported", "derived", "unavailable", "deferred", "not_applicable",
]

SOURCE_SCHEMA_VERSION = 1
DEFAULT_SOURCE = "claude"
SOURCE_ORDER = ("claude", "codex", "all")

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
    # Immutable, server-only facts used to advance an idle presentation clock.
    # They are deliberately separate from ``data`` so no internal accounting
    # evidence becomes part of the public source-envelope contract.
    clock_data: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        validate_dashboard_selection(self.source)
        if self.availability not in _AVAILABILITY:
            raise ValueError("unsupported availability")
        if self.freshness not in _FRESHNESS:
            raise ValueError("unsupported freshness")
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
        if self.data is not None:
            object.__setattr__(self, "data", _freeze(self.data))
        if self.clock_data is not None:
            object.__setattr__(self, "clock_data", _freeze(self.clock_data))


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
        clock_data=prior.clock_data,
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
    )


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


def _combined_metrics(
    claude: SourceDashboardState,
    codex: SourceDashboardState,
) -> Mapping[str, object] | None:
    if not (_coherent_provider(claude) and _coherent_provider(codex)):
        return None
    for state in (claude, codex):
        hero_capability = state.capabilities.get("hero")
        if hero_capability is None or hero_capability.status not in {"supported", "derived"}:
            return None
    try:
        claude_hero = claude.data["hero"]
        codex_hero = codex.data["hero"]
        if not isinstance(claude_hero, Mapping) or not isinstance(codex_hero, Mapping):
            return None
        claude_cost = claude_hero["cost_usd"]
        codex_cost = codex_hero["cost_usd"]
        claude_tokens = claude_hero["total_tokens"]
        codex_tokens = codex_hero["total_tokens"]
        if (
            isinstance(claude_cost, bool) or not isinstance(claude_cost, (int, float))
            or isinstance(codex_cost, bool) or not isinstance(codex_cost, (int, float))
            or isinstance(claude_tokens, bool) or not isinstance(claude_tokens, int)
            or isinstance(codex_tokens, bool) or not isinstance(codex_tokens, int)
        ):
            return None
    except (KeyError, TypeError):
        return None
    return {
        "cost_usd": float(claude_cost) + float(codex_cost),
        "total_tokens": claude_tokens + codex_tokens,
    }


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
    combined = _combined_metrics(claude, codex)
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
            codex.data_version, codex.availability, codex.freshness,
            combined is not None,
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    data_version = "all:" + hashlib.sha256(version_material).hexdigest()[:24]
    successes = (item for item in (claude.last_success_at, codex.last_success_at) if item is not None)
    last_success_at = min(successes, default=None)
    return SourceDashboardState(
        source="all",
        availability=availability,
        freshness=freshness,
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
            "alerts": {"rows": _combined_alert_rows(claude, codex)},
            "providers": {
                "claude": claude.data,
                "codex": codex.data,
            },
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
