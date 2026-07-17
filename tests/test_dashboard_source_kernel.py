"""Pure immutable source-dashboard contracts for #294 S4 Stage 1."""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest
import _lib_dashboard_sources as source_kernel

from _lib_dashboard_sources import (
    CapabilityRecord,
    SourceDashboardBundle,
    SourceDashboardState,
    SourceDashboardWarning,
    dashboard_resource_key,
    validate_dashboard_selection,
    validate_physical_source,
)


UTC = dt.timezone.utc


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
    cost_usd: float = 0.0,
    total_tokens: int = 0,
    alerts: tuple[dict[str, object], ...] = (),
) -> SourceDashboardState:
    return SourceDashboardState(
        source=source,
        availability=availability,
        freshness=freshness,
        warnings=(),
        data_version=f"{source}-version",
        last_success_at=dt.datetime(2026, 7, 16, tzinfo=UTC),
        capabilities={"quota": CapabilityRecord("supported", "native-windows")},
        data={
            "hero": {"cost_usd": cost_usd, "total_tokens": total_tokens},
            "quota": {"label": f"{source} native quota"},
            "budget": {"label": f"{source} calendar budget"},
            "alerts": {"rows": alerts},
        },
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


def test_source_dashboard_bundle_is_frozen_with_stage_one_constants():
    claude = _provider_state("claude")
    codex = _provider_state("codex")
    all_sources = source_kernel.compose_all_state(claude, codex)
    bundle = SourceDashboardBundle(
        source_schema_version=1,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={"claude": claude, "codex": codex, "all": all_sources},
    )

    assert bundle.source_schema_version == 1
    assert bundle.default_source == "claude"
    assert bundle.source_order == ("claude", "codex", "all")
    with pytest.raises(TypeError):
        bundle.sources["codex"] = codex


def test_source_dashboard_bundle_rejects_a_torn_provider_map():
    codex = _provider_state("codex")

    with pytest.raises(ValueError, match="exactly claude, codex, and all"):
        SourceDashboardBundle(
            source_schema_version=1,
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
    assert combined.data["combined"] == {"cost_usd": 6.25, "total_tokens": 100}
    assert combined.data["providers"]["claude"]["quota"]["label"] == "claude native quota"
    assert combined.data["providers"]["codex"]["budget"]["label"] == "codex calendar budget"
    assert "quota" not in combined.data["combined"]
    assert "budget" not in combined.data["combined"]


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


def test_all_composition_reports_both_empty_as_successful_empty_data():
    combined = source_kernel.compose_all_state(
        _provider_state("claude", availability="empty"),
        _provider_state("codex", availability="empty"),
    )

    assert combined.availability == "empty"
    assert combined.freshness == "fresh"
    assert combined.data["combined"] == {"cost_usd": 0.0, "total_tokens": 0}


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


def test_unavailable_source_has_no_data_or_success_version():
    warning = SourceDashboardWarning("source_ingest_failed", "Source ingest failed.")

    unavailable = source_kernel.unavailable_source_state("codex", warning)

    assert unavailable.availability == "unavailable"
    assert unavailable.freshness == "stale"
    assert unavailable.data is None
    assert unavailable.data_version == ""
    assert unavailable.last_success_at is None
    assert unavailable.warnings == (warning,)


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
