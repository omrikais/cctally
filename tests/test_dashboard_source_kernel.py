"""Pure immutable source-dashboard contracts for #294 S4 Stage 1."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest
import _lib_dashboard_sources as source_kernel

from _lib_dashboard_sources import (
    SOURCE_SCHEMA_VERSION,
    CapabilityRecord,
    SourceDashboardBundle,
    SourceDashboardState,
    SourceDashboardWarning,
    dashboard_resource_key,
    validate_dashboard_selection,
    validate_physical_source,
)


UTC = dt.timezone.utc

# #556 S1: the two legs are deliberately NOT one shared range — each names the
# cycle it covers, so the fixtures give them different bounds.
CLAUDE_WEEK_START = "2026-07-13T14:00:00Z"
CLAUDE_WEEK_END = "2026-07-20T14:00:00Z"
CODEX_CYCLE_START = "2026-07-15T09:00:00Z"
CODEX_CYCLE_END = "2026-07-22T09:00:00Z"

_DEFAULT_SCOPE: dict[str, object] = {"real_account_count": 1}


def _state(source: str = "codex") -> SourceDashboardState:
    return SourceDashboardState(
        source=source,
        availability="ok",
        freshness="fresh",
        warnings=(SourceDashboardWarning("source_build_failed", "source build failed", "quota"),),
        data_version="version-1",
        last_success_at=dt.datetime(2026, 7, 16, tzinfo=UTC),
        capabilities={"quota": CapabilityRecord("supported", "native-windows")},
        data={"rows": [{"value": 1}], "labels": ["one"]},
    )


def _provider_state(
    source: str,
    *,
    availability: str = "ok",
    freshness: str = "fresh",
    domain_freshness: dict[str, str] | None = None,
    cost_usd: float = 0.0,
    total_tokens: int = 0,
    alerts: tuple[dict[str, object], ...] = (),
    hero_capability: CapabilityRecord | None = None,
    cycle_freshness: str | None = None,
    account_scope: dict[str, object] | None = _DEFAULT_SCOPE,
    warnings: tuple[SourceDashboardWarning, ...] = (),
    period: bool = True,
    ingest_backlog: dict[str, object] | None = None,
    last_success_at: dt.datetime | None = dt.datetime(2026, 7, 16, tzinfo=UTC),
) -> SourceDashboardState:
    """One provider state in the #556 S1 v5 shape.

    ``account_scope`` defaults to a single REAL account so every legacy case in
    this module keeps composing; pass ``None`` for the fail-closed unresolved
    path and ``{"real_account_count": n}`` for decoration.
    """
    hero: dict[str, object] = {
        "cost_usd": cost_usd,
        "total_tokens": total_tokens,
        **({"cycle_freshness": cycle_freshness} if cycle_freshness else {}),
    }
    if period:
        if source == "claude":
            hero["current_week"] = {
                "week_start_at": CLAUDE_WEEK_START,
                "reset_at_utc": CLAUDE_WEEK_END,
            }
        else:
            hero["cycle"] = {
                "window_minutes": 10_080,
                "start_at": CODEX_CYCLE_START,
                "resets_at": CODEX_CYCLE_END,
            }
    return SourceDashboardState(
        source=source,
        availability=availability,
        freshness=freshness,
        domain_freshness=domain_freshness,
        warnings=warnings,
        data_version=f"{source}-version",
        last_success_at=last_success_at,
        capabilities={
            "hero": hero_capability or CapabilityRecord("supported", "native-reset-cycle"),
            "quota": CapabilityRecord("supported", "native-windows"),
        },
        data={
            "hero": hero,
            "quota": {"label": f"{source} native quota"},
            "budget": {"label": f"{source} calendar budget"},
            "alerts": {"rows": alerts},
            **({"ingest_backlog": ingest_backlog} if ingest_backlog else {}),
        },
        account_scope=account_scope,
    )


def test_source_dashboard_state_recursively_freezes_published_values():
    state = _state()

    with pytest.raises(TypeError):
        state.capabilities["quota"] = CapabilityRecord("derived", "other")
    with pytest.raises(TypeError):
        state.data["rows"] = ()
    with pytest.raises(TypeError):
        state.data["rows"][0]["value"] = 2
    assert state.data["labels"] == ("one",)


def test_source_dashboard_state_freezes_and_validates_domain_freshness():
    state = _provider_state(
        "codex",
        domain_freshness={"hero": "fresh", "quota": "stale", "sessions": "fresh"},
    )

    assert dict(state.domain_freshness) == {
        "hero": "fresh",
        "quota": "stale",
        "sessions": "fresh",
    }
    with pytest.raises(TypeError):
        state.domain_freshness["quota"] = "fresh"
    with pytest.raises(ValueError, match="domain freshness"):
        _provider_state(
            "codex",
            domain_freshness={"hero": "fresh", "quota": "aging", "sessions": "fresh"},
        )
    with pytest.raises(ValueError, match="domain freshness"):
        _provider_state(
            "codex",
            domain_freshness={"hero": "fresh", "quota": "stale"},
        )


def test_domain_freshness_legacy_fallback_is_provider_freshness():
    legacy = _provider_state("codex", freshness="stale")
    object.__setattr__(legacy, "domain_freshness", None)

    assert source_kernel.source_domain_freshness(legacy, "hero") == "stale"
    assert source_kernel.source_domain_freshness(legacy, "quota") == "stale"
    assert source_kernel.source_domain_freshness(legacy, "sessions") == "stale"


def test_source_dashboard_bundle_is_frozen_with_stage_one_constants():
    claude = _provider_state("claude")
    codex = _provider_state("codex")
    all_sources = source_kernel.compose_all_state(claude, codex)
    bundle = SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={"claude": claude, "codex": codex, "all": all_sources},
    )

    assert bundle.source_schema_version == SOURCE_SCHEMA_VERSION
    assert bundle.default_source == "claude"
    assert bundle.source_order == ("claude", "codex", "all")
    with pytest.raises(TypeError):
        bundle.sources["codex"] = codex


def test_source_dashboard_bundle_rejects_a_torn_provider_map():
    codex = _provider_state("codex")

    with pytest.raises(ValueError, match="exactly claude, codex, and all"):
        SourceDashboardBundle(
            source_schema_version=SOURCE_SCHEMA_VERSION,
            default_source="claude",
            source_order=("claude", "codex", "all"),
            sources={"codex": codex},
        )


@pytest.mark.parametrize(
    "status",
    ("supported", "derived", "unavailable", "deferred", "not_applicable"),
)
def test_capability_record_accepts_exact_stage_one_status_vocabulary(status: str):
    assert CapabilityRecord(status, "native").status == status


@pytest.mark.parametrize("status", ("not applicable", "missing", "ok", ""))
def test_capability_record_rejects_noncanonical_statuses(status: str):
    with pytest.raises(ValueError):
        CapabilityRecord(status, "native")


def test_physical_sources_exclude_presentation_only_all_but_selection_allows_it():
    assert validate_physical_source("claude") == "claude"
    assert validate_physical_source("codex") == "codex"
    with pytest.raises(ValueError):
        validate_physical_source("all")
    assert validate_dashboard_selection("all") == "all"


def test_dashboard_resource_key_is_domain_separated_and_nonrevealing():
    root_key = "a" * 32
    native_id = "session-native-id-canary"
    session = dashboard_resource_key("session", "codex", root_key, native_id)

    assert session.startswith("session:")
    assert root_key not in session
    assert native_id not in session
    assert session == dashboard_resource_key("session", "codex", root_key, native_id)
    assert session != dashboard_resource_key("session", "claude", native_id)
    assert session != dashboard_resource_key("project", "codex", root_key, native_id)
    assert session != dashboard_resource_key("session", "codex", native_id, root_key)


@pytest.mark.parametrize(
    "resource,source,parts",
    (("", "codex", ("native",)), ("session", "all", ("native",)), ("session", "codex", ("",))),
)
def test_dashboard_resource_key_rejects_invalid_resource_source_and_parts(
    resource: str, source: str, parts: tuple[str, ...],
):
    with pytest.raises(ValueError):
        dashboard_resource_key(resource, source, *parts)


def test_all_composition_sums_only_compatible_cost_and_tokens():
    claude = _provider_state("claude", cost_usd=2.5, total_tokens=30)
    codex = _provider_state("codex", cost_usd=3.75, total_tokens=70)

    combined = source_kernel.compose_all_state(claude, codex)

    assert combined.source == "all"
    assert combined.availability == "ok"
    assert combined.freshness == "fresh"
    assert combined.data["combined"]["cost_usd"] == 6.25
    assert combined.data["combined"]["total_tokens"] == 100
    assert combined.data["providers"]["claude"]["quota"]["label"] == "claude native quota"
    assert combined.data["providers"]["codex"]["budget"]["label"] == "codex calendar budget"
    assert "quota" not in combined.data["combined"]
    assert "budget" not in combined.data["combined"]
    assert dict(combined.domain_freshness) == {
        "hero": "fresh",
        "quota": "fresh",
        "sessions": "fresh",
    }


def test_all_composition_aggregates_each_domain_without_staling_the_provider():
    claude = _provider_state("claude", cost_usd=2.5, total_tokens=30)
    codex = _provider_state(
        "codex",
        domain_freshness={"hero": "fresh", "quota": "stale", "sessions": "fresh"},
        cost_usd=3.75,
        total_tokens=70,
    )

    combined = source_kernel.compose_all_state(claude, codex)

    assert (combined.availability, combined.freshness) == ("ok", "fresh")
    assert combined.data["combined"]["cost_usd"] == 6.25
    assert dict(combined.domain_freshness) == {
        "hero": "fresh",
        "quota": "stale",
        "sessions": "fresh",
    }


def test_all_version_identity_includes_domain_freshness():
    claude = _provider_state("claude", cost_usd=2.5, total_tokens=30)
    fresh = source_kernel.compose_all_state(
        claude,
        _provider_state("codex", cost_usd=3.75, total_tokens=70),
    )
    quota_stale = source_kernel.compose_all_state(
        claude,
        _provider_state(
            "codex",
            domain_freshness={"hero": "fresh", "quota": "stale", "sessions": "fresh"},
            cost_usd=3.75,
            total_tokens=70,
        ),
    )

    assert quota_stale.data == fresh.data
    assert quota_stale.data_version != fresh.data_version


@pytest.mark.parametrize(
    "codex_availability,codex_freshness",
    (("partial", "stale"), ("unavailable", "stale")),
)
def test_all_composition_never_blends_current_and_stale_provider_data(
    codex_availability: str, codex_freshness: str,
):
    combined = source_kernel.compose_all_state(
        _provider_state("claude", cost_usd=2.5, total_tokens=30),
        _provider_state(
            "codex",
            availability=codex_availability,
            freshness=codex_freshness,
            cost_usd=3.75,
            total_tokens=70,
        ),
    )

    assert combined.availability == "partial"
    assert combined.freshness == "stale"
    assert combined.data["combined"] is None
    assert set(combined.data["providers"]) == {"claude", "codex"}


def test_fresh_partial_provider_is_reusable_and_contributes_all_totals():
    codex = _provider_state(
        "codex", availability="partial", freshness="fresh", cost_usd=2.0, total_tokens=20,
    )
    claude = _provider_state(
        "claude", availability="ok", freshness="fresh", cost_usd=1.0, total_tokens=10,
    )

    assert source_kernel.reuse_coherent_source_state(
        codex, data_version=codex.data_version,
    ) is codex
    combined = source_kernel.compose_all_state(claude, codex)

    assert (combined.availability, combined.freshness) == ("partial", "fresh")
    assert combined.data["combined"]["cost_usd"] == 3.0
    assert combined.data["combined"]["total_tokens"] == 30


def test_domain_staleness_does_not_disable_provider_reuse():
    codex = _provider_state(
        "codex",
        freshness="fresh",
        domain_freshness={"hero": "fresh", "quota": "stale", "sessions": "fresh"},
    )

    assert source_kernel.reuse_coherent_source_state(
        codex, data_version=codex.data_version,
    ) is codex


def test_all_composition_keeps_fresh_provider_sections_when_codex_hero_is_unavailable():
    claude = _provider_state("claude", cost_usd=1.0, total_tokens=10)
    codex = _provider_state(
        "codex",
        availability="partial",
        freshness="fresh",
        cost_usd=None,
        total_tokens=None,
        hero_capability=CapabilityRecord("unavailable", "missing-or-conflicting-native-cycle"),
    )

    combined = source_kernel.compose_all_state(claude, codex)

    assert combined.availability == "partial"
    assert combined.freshness == "fresh"
    assert combined.data["combined"] is None
    assert set(combined.data["providers"]) == {"claude", "codex"}


def test_a_stale_provider_cycle_no_longer_qualifies_the_combined_figure():
    """#556 S1 §4.2 — `combined_totals_stale` retires.

    It was gated on two clocks forty times apart (`stale_after_seconds` 90 for
    the Claude percent observation against 3600 for the Codex weekly one),
    combined through `all(... == "fresh")`, so it was on nearly always. The
    counters it qualified are backward-looking actuals that a stale percent
    clock does not invalidate, and B2's category error and B3's
    self-contradicting accessible name both disappear with the sentence.
    """
    claude = _provider_state("claude", cost_usd=2.5, total_tokens=30,
                             cycle_freshness="stale")
    codex = _provider_state(
        "codex",
        domain_freshness={"hero": "stale", "quota": "stale", "sessions": "fresh"},
        cost_usd=3.75,
        total_tokens=70,
        cycle_freshness="stale",
    )

    combined = source_kernel.compose_all_state(claude, codex)

    assert combined.data["combined"]["cost_usd"] == 6.25
    assert "qualifications" not in combined.data["combined"]
    assert [warning.code for warning in combined.warnings] == []
    # The Codex envelope itself is NOT degraded by All composition.
    assert (codex.availability, codex.freshness, codex.warnings) == ("ok", "fresh", ())
    assert source_kernel.source_domain_freshness(codex, "sessions") == "fresh"
    assert combined.data["providers"]["codex"]["hero"]["cost_usd"] == 3.75


def test_all_composition_is_unchanged_when_the_codex_cycle_is_fresh():
    claude = _provider_state("claude", cost_usd=2.5, total_tokens=30)
    codex = _provider_state("codex", cost_usd=3.75, total_tokens=70)

    combined = source_kernel.compose_all_state(claude, codex)

    assert combined.data["combined"]["cost_usd"] == 6.25
    assert (combined.availability, combined.freshness) == ("ok", "fresh")
    assert combined.warnings == ()


def test_an_incoherent_provider_withholds_without_an_all_local_warning():
    """An incoherent provider publishes its own reason and already withholds
    the number, so All adds no warning of its own on top of it."""
    claude = _provider_state("claude", cost_usd=2.5, total_tokens=30)
    codex = _provider_state(
        "codex", availability="partial", freshness="stale",
        cost_usd=3.75, total_tokens=70, cycle_freshness="stale",
    )

    combined = source_kernel.compose_all_state(claude, codex)

    assert combined.data["combined"] is None
    assert combined.data["combined_unavailable"]["code"] == "provider_incoherent"
    assert (combined.availability, combined.freshness) == ("partial", "stale")
    assert combined.warnings == ()


def test_stale_partial_provider_is_not_reusable_or_composable():
    codex = _provider_state(
        "codex", availability="partial", freshness="stale", cost_usd=2.0, total_tokens=20,
    )

    assert source_kernel.reuse_coherent_source_state(
        codex, data_version=codex.data_version,
    ) is None


def test_all_composition_reports_both_empty_as_successful_empty_data():
    combined = source_kernel.compose_all_state(
        _provider_state("claude", availability="empty"),
        _provider_state("codex", availability="empty"),
    )

    assert combined.availability == "empty"
    assert combined.freshness == "fresh"
    assert combined.data["combined"]["cost_usd"] == 0.0
    assert combined.data["combined"]["total_tokens"] == 0


def test_all_composition_exposes_a_source_tagged_stably_sorted_alert_union():
    claude = _provider_state(
        "claude",
        alerts=(
            {"source": "claude", "key": "claude-old", "created_at": "2026-07-16T09:00:00Z"},
            {"source": "claude", "key": "claude-same", "created_at": "2026-07-16T10:00:00Z"},
        ),
    )
    codex = _provider_state(
        "codex",
        alerts=(
            {"source": "codex", "key": "codex-same", "created_at": "2026-07-16T10:00:00Z"},
            {"source": "codex", "key": "codex-new", "created_at": "2026-07-16T11:00:00Z"},
        ),
    )

    combined = source_kernel.compose_all_state(claude, codex)

    assert combined.capabilities["alerts"] == CapabilityRecord(
        "derived", "provider-native-union",
    )
    assert [row["key"] for row in combined.data["alerts"]["rows"]] == [
        "codex-new", "claude-same", "codex-same", "claude-old",
    ]
    assert all(row["source"] in {"claude", "codex"} for row in combined.data["alerts"]["rows"])


def test_prior_state_degradation_retains_whole_prior_data_and_version():
    prior = _provider_state("codex", cost_usd=3.75, total_tokens=70)
    warning = SourceDashboardWarning("codex_projection_incoherent", "Codex projection is incoherent.")

    degraded = source_kernel.degrade_source_state(prior, warning)

    assert degraded is not prior
    assert degraded.availability == "partial"
    assert degraded.freshness == "stale"
    assert degraded.data is prior.data
    assert degraded.data_version == prior.data_version
    assert degraded.last_success_at == prior.last_success_at
    assert degraded.warnings == (warning,)
    assert set(degraded.domain_freshness.values()) == {"stale"}


def test_degrading_an_unavailable_prior_stays_unavailable_not_invalid_partial():
    """An unavailable prior (no coherent generation, empty data_version) cannot
    be retained as a ``partial`` state — copying its empty ``data_version`` into
    a partial state trips the non-empty-data_version validator and raises.

    Regression for the dashboard ``source-bundle: data_version must be a
    non-empty string`` sync error, which fired on the 2nd (and every later)
    consecutive failing sync of a degraded provider: the 1st failure produces an
    unavailable prior, and the next failure degrades THAT prior.
    """
    warning = SourceDashboardWarning("source_ingest_failed", "Source ingest failed.")
    prior_unavailable = source_kernel.unavailable_source_state("codex", warning)
    assert prior_unavailable.availability == "unavailable"
    assert prior_unavailable.data_version == ""

    degraded = source_kernel.degrade_source_state(prior_unavailable, warning)

    assert degraded.source == "codex"
    assert degraded.availability == "unavailable"
    assert degraded.data_version == ""
    assert degraded.data is None
    assert degraded.warnings == (warning,)
    assert set(degraded.domain_freshness.values()) == {"stale"}


def test_unavailable_source_has_no_data_or_success_version():
    warning = SourceDashboardWarning("source_ingest_failed", "Source ingest failed.")

    unavailable = source_kernel.unavailable_source_state("codex", warning)

    assert unavailable.availability == "unavailable"
    assert unavailable.freshness == "stale"
    assert unavailable.data is None
    assert unavailable.data_version == ""
    assert unavailable.last_success_at is None
    assert unavailable.warnings == (warning,)
    assert set(unavailable.domain_freshness.values()) == {"stale"}


# --- #556 S1 Task 3 — authoritative account-count metadata (spec §3.8) -----


def test_account_scope_defaults_to_none_for_a_state_built_without_it():
    """Absent metadata is UNRESOLVED, never "undecorated".

    Every state constructed before this field existed — and every legacy
    constructor that still omits it — must reach composition as unresolved so
    the combined figure fails closed instead of publishing under an assumed
    single account.
    """
    legacy = SourceDashboardState(
        source="claude",
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="claude-version",
        last_success_at=None,
        capabilities={},
        data={"hero": {"cost_usd": 1.0, "total_tokens": 1}},
    )

    assert legacy.account_scope is None


def test_account_scope_is_frozen_like_clock_data():
    state = _provider_state("claude", account_scope={"real_account_count": 2})

    assert dict(state.account_scope) == {"real_account_count": 2}
    with pytest.raises(TypeError):
        state.account_scope["real_account_count"] = 1


def test_degrade_source_state_preserves_the_account_scope():
    """A degraded generation must not lose the count.

    `degrade_source_state` rebuilds the state field by field, so an omitted
    field silently becomes `None` — which would turn a transient provider
    failure into `account_scope_unresolved` on an install whose count was
    perfectly readable.
    """
    prior = _provider_state(
        "claude", cost_usd=2.5, total_tokens=30,
        account_scope={"real_account_count": 3},
    )
    warning = SourceDashboardWarning("source_ingest_failed", "Source ingest failed.")

    degraded = source_kernel.degrade_source_state(prior, warning)

    assert degraded.availability == "partial"
    assert dict(degraded.account_scope) == {"real_account_count": 3}


def test_unavailable_source_state_has_no_account_scope():
    """No coherent generation exists, so no count may be asserted for one."""
    warning = SourceDashboardWarning("source_ingest_failed", "Source ingest failed.")

    unavailable = source_kernel.unavailable_source_state("claude", warning)

    assert unavailable.account_scope is None


def test_reuse_carries_the_account_scope_by_identity():
    prior = _provider_state(
        "claude", cost_usd=1.0, total_tokens=10,
        account_scope={"real_account_count": 1},
    )

    reused = source_kernel.reuse_coherent_source_state(
        prior, data_version=prior.data_version,
    )

    assert reused is prior
    assert dict(reused.account_scope) == {"real_account_count": 1}


def test_account_scope_never_reaches_the_published_envelope():
    """Account cardinality is server-only, in the same class as `clock_data`."""
    from _cctally_dashboard_envelope import _source_state_to_wire

    wire = _source_state_to_wire(
        _provider_state("claude", account_scope={"real_account_count": 4242}),
    )

    assert "account_scope" not in wire
    serialized = json.dumps(wire)
    assert "real_account_count" not in serialized
    assert "4242" not in serialized


def test_unchanged_coherent_provider_state_is_reused_by_identity():
    prior = _provider_state("codex", cost_usd=3.75, total_tokens=70)

    reused = source_kernel.reuse_coherent_source_state(
        prior, data_version="codex-version",
    )

    assert reused is prior


@pytest.mark.parametrize(
    "availability,freshness",
    (("partial", "stale"), ("unavailable", "stale"), ("ok", "stale")),
)
def test_stale_or_unavailable_provider_state_is_not_reused_for_recovery(
    availability: str, freshness: str,
):
    prior = _provider_state(
        "codex", availability=availability, freshness=freshness,
        cost_usd=3.75, total_tokens=70,
    )

    assert source_kernel.reuse_coherent_source_state(
        prior, data_version="codex-version",
    ) is None


def _stats_digest_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE quota_projection_state (
            id INTEGER PRIMARY KEY, source_root_key TEXT, physical_signature TEXT,
            generation TEXT, completed_at_utc TEXT
        );
        CREATE TABLE quota_window_blocks (
            id INTEGER PRIMARY KEY, source TEXT, source_root_key TEXT,
            logical_limit_key TEXT, observed_slot TEXT, window_minutes INTEGER,
            limit_id TEXT, limit_name TEXT, resets_at_utc TEXT,
            nominal_start_at_utc TEXT, first_observed_at_utc TEXT,
            last_observed_at_utc TEXT, first_percent REAL, current_percent REAL,
            orphaned_at TEXT, last_source_path TEXT, last_line_offset INTEGER,
            generation TEXT
        );
        CREATE TABLE quota_percent_milestones (
            id INTEGER PRIMARY KEY, source TEXT, source_root_key TEXT,
            logical_limit_key TEXT, observed_slot TEXT, window_minutes INTEGER,
            resets_at_utc TEXT, percent_threshold INTEGER, captured_at_utc TEXT,
            high_water_percent REAL, orphaned_at TEXT, source_path TEXT,
            line_offset INTEGER, generation TEXT
        );
        CREATE TABLE quota_threshold_events (
            id INTEGER PRIMARY KEY, source TEXT, source_root_key TEXT,
            logical_limit_key TEXT, observed_slot TEXT, window_minutes INTEGER,
            resets_at_utc TEXT, threshold INTEGER, qualifying_kind TEXT,
            qualifying_percent REAL, projected_percent REAL, severity TEXT,
            created_at_utc TEXT, disposition TEXT, alerted_at TEXT,
            suppressed_at TEXT, orphaned_at TEXT
        );
        CREATE TABLE budget_milestones (
            id INTEGER PRIMARY KEY, vendor TEXT, period_start_at TEXT, period TEXT,
            threshold REAL, budget_usd REAL, spent_usd REAL, consumption_pct REAL,
            crossed_at_utc TEXT, alerted_at TEXT
        );
        CREATE TABLE projected_milestones (
            id INTEGER PRIMARY KEY, week_start_at TEXT, period TEXT, metric TEXT,
            threshold REAL, projected_value REAL, denominator REAL,
            crossed_at_utc TEXT, alerted_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO quota_projection_state VALUES (99, 'root-a', 'physical-a', 'generation-a', '2026-07-16T01:00:00Z')"
    )
    conn.execute(
        "INSERT INTO quota_window_blocks VALUES (88, 'codex', 'root-a', 'limit-a', 'five_hour', 300, 'id-a', 'Five hour', '2026-07-17T00:00:00Z', '2026-07-16T19:00:00Z', '2026-07-16T20:00:00Z', '2026-07-16T21:00:00Z', 20, 40, NULL, '/private/path.jsonl', 123, 'generation-a')"
    )
    conn.execute(
        "INSERT INTO quota_percent_milestones VALUES (77, 'codex', 'root-a', 'limit-a', 'five_hour', 300, '2026-07-17T00:00:00Z', 40, '2026-07-16T21:00:00Z', 40, NULL, '/private/path.jsonl', 123, 'generation-a')"
    )
    conn.execute(
        "INSERT INTO quota_threshold_events VALUES (66, 'codex', 'root-a', 'limit-a', 'five_hour', 300, '2026-07-17T00:00:00Z', 80, 'actual', 80, NULL, 'warn', '2026-07-16T21:00:00Z', 'alerted', '2026-07-16T21:01:00Z', NULL, NULL)"
    )
    conn.execute(
        "INSERT INTO budget_milestones VALUES (55, 'codex', '2026-07-01T00:00:00Z', 'calendar-month', 90, 100, 91, 91, '2026-07-16T21:00:00Z', '2026-07-16T21:01:00Z')"
    )
    conn.execute(
        "INSERT INTO projected_milestones VALUES (44, '2026-07-13T00:00:00Z', 'calendar-week', 'codex_budget_usd', 100, 101, 100, '2026-07-16T21:00:00Z', '2026-07-16T21:01:00Z')"
    )
    conn.commit()
    return conn


def test_codex_stats_digest_tracks_only_selected_semantic_columns():
    conn = _stats_digest_db()
    try:
        baseline = source_kernel.codex_stats_digest(conn)

        conn.execute("UPDATE quota_projection_state SET generation='generation-b', completed_at_utc='2026-07-16T22:00:00Z'")
        conn.execute("UPDATE quota_window_blocks SET id=1, last_source_path='/leak/canary', last_line_offset=999, generation='generation-b'")
        conn.execute("UPDATE quota_percent_milestones SET id=2, source_path='/leak/canary', line_offset=999, generation='generation-b'")
        assert source_kernel.codex_stats_digest(conn) == baseline

        conn.execute("UPDATE quota_window_blocks SET orphaned_at='2026-07-16T22:00:00Z'")
        assert source_kernel.codex_stats_digest(conn) != baseline
        conn.execute("UPDATE quota_window_blocks SET orphaned_at=NULL")
        conn.execute("UPDATE quota_threshold_events SET alerted_at='2026-07-16T22:00:00Z'")
        assert source_kernel.codex_stats_digest(conn) != baseline
    finally:
        conn.close()


def test_codex_stats_digest_is_order_independent_but_detects_semantic_deletes():
    first = _stats_digest_db()
    second = _stats_digest_db()
    try:
        # Re-insertion with a different surrogate identity and insertion order
        # cannot affect the canonical selected-column array.
        row = second.execute("SELECT * FROM quota_window_blocks").fetchone()
        second.execute("DELETE FROM quota_window_blocks")
        second.execute(
            "INSERT INTO quota_window_blocks VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row[1:],
        )
        assert source_kernel.codex_stats_digest(first) == source_kernel.codex_stats_digest(second)

        second.execute("DELETE FROM projected_milestones")
        assert source_kernel.codex_stats_digest(first) != source_kernel.codex_stats_digest(second)
    finally:
        first.close()
        second.close()


def test_projection_coherence_requires_every_active_root_to_match_physical_signature():
    coherent = source_kernel.assess_codex_projection_coherence(
        active_root_keys=("root-a", "root-b"),
        physical_signatures={"root-a": "a", "root-b": "b"},
        projection_signatures={"root-a": "a", "root-b": "b"},
    )
    missing = source_kernel.assess_codex_projection_coherence(
        active_root_keys=("root-a",),
        physical_signatures={"root-a": "a"},
        projection_signatures={},
    )
    mismatch = source_kernel.assess_codex_projection_coherence(
        active_root_keys=("root-a",),
        physical_signatures={"root-a": "a"},
        projection_signatures={"root-a": "b"},
    )

    assert coherent.coherent is True
    assert coherent.reason is None
    assert missing.coherent is False and missing.reason == "missing_projection_state"
    assert mismatch.coherent is False and mismatch.reason == "physical_signature_mismatch"


# === #556 S1 Task 5 — the typed combined outcome (spec §3.5, §3.7, §4.2-4.6) ==


def _combined_pair(*, claude_kwargs=None, codex_kwargs=None):
    return source_kernel.compose_all_state(
        _provider_state("claude", **{
            "cost_usd": 2.5, "total_tokens": 30, **(claude_kwargs or {})}),
        _provider_state("codex", **{
            "cost_usd": 3.75, "total_tokens": 70, **(codex_kwargs or {})}),
    )


_CODEX_PROJECTION_WARNING = SourceDashboardWarning(
    "codex_projection_incoherent", "Codex quota projection is unavailable.", "hero",
)
_CODEX_CYCLE_WARNING = SourceDashboardWarning(
    "codex_cycle_unavailable", "Codex native reset cycle is unavailable.", "hero",
)
_HERO_UNAVAILABLE = CapabilityRecord(
    "unavailable", "missing-or-conflicting-native-cycle",
)


def test_combined_publishes_both_legs_with_their_own_named_periods():
    """§3.5 — the sum, plus the cycle each leg covers."""
    combined = _combined_pair()

    assert (combined.availability, combined.freshness) == ("ok", "fresh")
    payload = combined.data["combined"]
    assert payload["cost_usd"] == 6.25
    assert payload["total_tokens"] == 100
    assert dict(payload["legs"]["claude"]) == {
        "state": "current",
        "cost_usd": 2.5,
        "total_tokens": 30,
        "period": {
            "kind": "subscription_week",
            "label": "Claude subscription week",
            "start_at": CLAUDE_WEEK_START,
            "end_at": CLAUDE_WEEK_END,
        },
    }
    assert dict(payload["legs"]["codex"]) == {
        "state": "current",
        "cost_usd": 3.75,
        "total_tokens": 70,
        "period": {
            "kind": "native_7_day_cycle",
            "label": "Codex native 7-day cycle",
            "start_at": CODEX_CYCLE_START,
            "end_at": CODEX_CYCLE_END,
        },
    }
    # Optional keys are omitted when inapplicable.
    assert "qualifications" not in payload
    assert "combined_unavailable" not in combined.data
    # Nothing that is not summable is ever summed.
    assert set(payload) == {"cost_usd", "total_tokens", "legs"}
    # rev2's derivable fields are gone.
    assert "contributors" not in payload
    assert "empty_providers" not in payload


def test_retired_stale_cycle_machinery_is_gone():
    """§4.2 — `combined_totals_stale` and its two helpers leave the kernel."""
    assert not hasattr(source_kernel, "_hero_cycle_is_stale")
    assert not hasattr(source_kernel, "_stale_cycle_providers")


# --- §3.7 publication matrix, one case per row -----------------------------

_MATRIX = (
    # (label, claude_kwargs, codex_kwargs, expected_code)
    (
        "claude decorated",
        {"account_scope": {"real_account_count": 2}}, {},
        "multi_account_unsupported",
    ),
    (
        "codex decorated",
        {}, {"account_scope": {"real_account_count": 3}},
        "multi_account_unsupported",
    ),
    (
        "provider incoherent",
        {}, {"availability": "partial", "freshness": "stale"},
        "provider_incoherent",
    ),
    (
        "provider unavailable",
        {}, {"availability": "unavailable", "freshness": "stale"},
        "provider_incoherent",
    ),
    (
        "codex cycle unavailable",
        {},
        {
            "cost_usd": None, "total_tokens": None, "period": False,
            "availability": "partial",
            "hero_capability": _HERO_UNAVAILABLE,
            "warnings": (_CODEX_CYCLE_WARNING,),
        },
        "codex_cycle_unavailable",
    ),
    (
        "codex projection incoherent",
        {},
        {
            "cost_usd": None, "total_tokens": None, "period": False,
            "availability": "partial",
            "hero_capability": CapabilityRecord(
                "unavailable", "projection-incoherent"),
            "warnings": (_CODEX_PROJECTION_WARNING,),
        },
        "codex_projection_incoherent",
    ),
    (
        "claude accounting present but no cycle",
        {"cost_usd": None, "total_tokens": None, "period": False}, {},
        "claude_cycle_unresolved",
    ),
    (
        "invalid claude counter",
        {"cost_usd": float("inf")}, {},
        "invalid_counter",
    ),
    (
        "boolean codex token counter",
        {}, {"total_tokens": True},
        "invalid_counter",
    ),
    (
        "negative codex cost",
        {}, {"cost_usd": -1.0},
        "invalid_counter",
    ),
    (
        "account count unresolvable",
        {"account_scope": None}, {},
        "account_scope_unresolved",
    ),
)


@pytest.mark.parametrize(
    "label,claude_kwargs,codex_kwargs,expected_code",
    _MATRIX,
    ids=[row[0].replace(" ", "-") for row in _MATRIX],
)
def test_combined_withholding_matrix_resolves_one_winning_code(
    label: str, claude_kwargs: dict, codex_kwargs: dict, expected_code: str,
):
    """§3.7 — every withholding row names its own reason.

    `combined` is withheld and `combined_unavailable.code` is the winning cause
    under the stated precedence. The first listed cause always equals the
    winner, so `causes` is ordered rather than a bag.
    """
    combined = _combined_pair(
        claude_kwargs=claude_kwargs, codex_kwargs=codex_kwargs)

    assert combined.data["combined"] is None, label
    unavailable = combined.data["combined_unavailable"]
    assert unavailable["code"] == expected_code, label
    assert unavailable["message"]
    assert unavailable["causes"][0]["code"] == expected_code, label
    assert all(
        cause["provider"] in {"claude", "codex"}
        for cause in unavailable["causes"]
    ), label


def test_multi_account_cause_carries_the_provider_and_its_count():
    combined = _combined_pair(
        codex_kwargs={"account_scope": {"real_account_count": 4}})

    unavailable = combined.data["combined_unavailable"]
    assert unavailable["code"] == "multi_account_unsupported"
    assert [dict(cause) for cause in unavailable["causes"]] == [{
        "provider": "codex",
        "code": "multi_account_unsupported",
        "detail": {"account_count": 4},
    }]


def test_invalid_counter_cause_names_the_field_and_never_echoes_the_value():
    combined = _combined_pair(claude_kwargs={"total_tokens": "1234567"})

    cause = combined.data["combined_unavailable"]["causes"][0]
    assert cause["provider"] == "claude"
    assert cause["code"] == "invalid_counter"
    assert dict(cause["detail"]) == {
        "field": "total_tokens", "reason": "non_integer"}
    assert "1234567" not in json.dumps(_jsonable(combined.data["combined_unavailable"]))


@pytest.mark.parametrize(
    "value,reason",
    (
        (float("nan"), "non_finite"),
        (float("-inf"), "non_finite"),
        (-0.5, "negative"),
        ("12.0", "non_integer"),
        (True, "non_integer"),
    ),
)
def test_invalid_cost_counter_reasons(value, reason: str):
    combined = _combined_pair(codex_kwargs={"cost_usd": value})

    cause = combined.data["combined_unavailable"]["causes"][0]
    assert cause["code"] == "invalid_counter"
    assert dict(cause["detail"]) == {"field": "cost_usd", "reason": reason}


# --- the two overlap cases -------------------------------------------------


def test_co_occurring_codex_failures_list_both_causes_projection_first():
    """§3.5 / §3.7 — `causes` lists EVERY cause found, in a fixed order.

    Coherence and cycle resolution are computed independently and both warnings
    are emitted, so both are real causes. Collapsing them to one would drop a
    fact the reader needs while leaving the winner unchanged, which is why the
    list — not just `code` — is asserted here.
    """
    combined = _combined_pair(codex_kwargs={
        "cost_usd": None, "total_tokens": None, "period": False,
        "availability": "partial",
        "hero_capability": CapabilityRecord("unavailable", "projection-incoherent"),
        "warnings": (_CODEX_PROJECTION_WARNING, _CODEX_CYCLE_WARNING),
    })

    unavailable = combined.data["combined_unavailable"]
    assert unavailable["code"] == "codex_projection_incoherent"
    assert [cause["code"] for cause in unavailable["causes"]] == [
        "codex_projection_incoherent", "codex_cycle_unavailable",
    ]
    assert all(
        cause["provider"] == "codex" for cause in unavailable["causes"])


def test_a_lone_codex_hero_failure_lists_only_itself():
    """The plural cause list is driven by the warnings actually present."""
    combined = _combined_pair(codex_kwargs={
        "cost_usd": None, "total_tokens": None, "period": False,
        "availability": "partial",
        "hero_capability": _HERO_UNAVAILABLE,
        "warnings": (_CODEX_CYCLE_WARNING,),
    })

    unavailable = combined.data["combined_unavailable"]
    assert [cause["code"] for cause in unavailable["causes"]] == [
        "codex_cycle_unavailable",
    ]


def test_incoherent_generation_beats_decoration_on_the_same_provider():
    """§3.7 — precedence 1 beats precedence 3."""
    combined = _combined_pair(codex_kwargs={
        "availability": "partial",
        "freshness": "stale",
        "account_scope": {"real_account_count": 2},
    })

    unavailable = combined.data["combined_unavailable"]
    assert unavailable["code"] == "provider_incoherent"
    assert [cause["code"] for cause in unavailable["causes"]] == [
        "provider_incoherent", "multi_account_unsupported",
    ]
    assert (combined.availability, combined.freshness) == ("partial", "stale")


def test_claude_causes_are_listed_before_codex_causes_at_the_same_precedence():
    combined = _combined_pair(
        claude_kwargs={"account_scope": {"real_account_count": 2}},
        codex_kwargs={"account_scope": {"real_account_count": 5}},
    )

    causes = combined.data["combined_unavailable"]["causes"]
    assert [cause["provider"] for cause in causes] == ["claude", "codex"]
    assert [cause["detail"]["account_count"] for cause in causes] == [2, 5]


# --- published-but-qualified states ----------------------------------------


def test_a_stale_but_future_codex_cycle_publishes_with_no_staleness_marker():
    """§3.7 / acceptance 5 — stale percent evidence never withholds actuals."""
    combined = _combined_pair(codex_kwargs={
        "cycle_freshness": "stale",
        "domain_freshness": {"hero": "fresh", "quota": "stale", "sessions": "fresh"},
    })

    assert combined.data["combined"]["cost_usd"] == 6.25
    assert "qualifications" not in combined.data["combined"]
    assert combined.warnings == ()
    assert (combined.availability, combined.freshness) == ("ok", "fresh")


def test_a_codex_ingest_backlog_becomes_a_combined_qualification():
    """§4.3 — composition lifts the note; the All surfaces stop reading the
    provider field for this purpose."""
    combined = _combined_pair(codex_kwargs={
        "ingest_backlog": {"files": 3, "bytes": 8192, "since": "2026-07-16T09:00:00Z"},
    })

    qualifications = [dict(q) for q in combined.data["combined"]["qualifications"]]
    assert [q["code"] for q in qualifications] == ["codex_ingest_backlog"]
    assert qualifications[0]["provider"] == "codex"
    assert qualifications[0]["message"]


def test_an_empty_leg_is_explicit_and_qualifies_the_published_figure():
    """§3.7 — the present leg publishes; the absent one says so."""
    combined = _combined_pair(codex_kwargs={
        "availability": "empty", "cost_usd": 0.0, "total_tokens": 0, "period": False,
    })

    payload = combined.data["combined"]
    assert payload["cost_usd"] == 2.5
    assert payload["total_tokens"] == 30
    assert dict(payload["legs"]["codex"]) == {
        "state": "empty", "cost_usd": 0.0, "total_tokens": 0,
    }
    assert [q["code"] for q in payload["qualifications"]] == ["provider_empty"]
    assert payload["qualifications"][0]["provider"] == "codex"


def test_both_providers_empty_publish_zeros_as_an_empty_availability():
    combined = source_kernel.compose_all_state(
        _provider_state("claude", availability="empty", period=False,
                        cost_usd=None, total_tokens=None),
        _provider_state("codex", availability="empty", period=False),
    )

    payload = combined.data["combined"]
    assert combined.availability == "empty"
    assert (payload["cost_usd"], payload["total_tokens"]) == (0.0, 0)
    assert [leg["state"] for leg in payload["legs"].values()] == ["empty", "empty"]
    assert [q["provider"] for q in payload["qualifications"]] == ["claude", "codex"]


def test_a_numeric_zero_inside_a_resolved_period_is_ordinary_spend():
    combined = _combined_pair(codex_kwargs={"cost_usd": 0.0, "total_tokens": 0})

    payload = combined.data["combined"]
    assert payload["legs"]["codex"]["state"] == "current"
    assert "period" in payload["legs"]["codex"]
    assert "qualifications" not in payload


def test_a_partial_provider_outside_the_hero_domain_still_publishes():
    combined = _combined_pair(codex_kwargs={
        "availability": "partial",
        "warnings": (SourceDashboardWarning(
            "codex_metadata_incomplete", "metadata incomplete", "projects"),),
    })

    assert combined.data["combined"]["cost_usd"] == 6.25
    assert (combined.availability, combined.freshness) == ("partial", "fresh")


# --- §4.6 last_success_at ---------------------------------------------------


def test_last_success_at_is_none_unless_both_providers_have_one():
    """§4.6 — filtering `None` before `min` lets one provider's success
    masquerade as All's while the client keys "never succeeded" on null."""
    combined = _combined_pair(codex_kwargs={"last_success_at": None})

    assert combined.last_success_at is None


def test_last_success_at_is_the_older_of_the_two():
    older = dt.datetime(2026, 7, 14, tzinfo=UTC)
    newer = dt.datetime(2026, 7, 18, tzinfo=UTC)

    assert source_kernel.compose_all_state(
        _provider_state("claude", cost_usd=1.0, total_tokens=1,
                        last_success_at=newer),
        _provider_state("codex", cost_usd=1.0, total_tokens=1,
                        last_success_at=older),
    ).last_success_at == older


# --- invariant 6: materially different states get different versions -------


@pytest.mark.parametrize(
    "codex_kwargs",
    (
        {"account_scope": {"real_account_count": 2}},
        {"cost_usd": None, "total_tokens": None, "period": False,
         "availability": "partial", "hero_capability": _HERO_UNAVAILABLE,
         "warnings": (_CODEX_CYCLE_WARNING,)},
        {"ingest_backlog": {"files": 1, "bytes": 10, "since": "2026-07-16T09:00:00Z"}},
        {"availability": "empty", "cost_usd": 0.0, "total_tokens": 0, "period": False},
    ),
    ids=("decorated", "cycle-unavailable", "backlog", "empty"),
)
def test_every_new_combined_input_moves_the_all_data_version(codex_kwargs: dict):
    baseline = _combined_pair()

    assert _combined_pair(
        codex_kwargs=codex_kwargs).data_version != baseline.data_version


def _jsonable(value):
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value
