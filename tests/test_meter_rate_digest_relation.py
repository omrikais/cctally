"""#769 S6 / #765 — the metering-rate relation in both stats digest registries.

`meter_rate_change_events` is published in the dashboard alert envelope
(`bin/_cctally_dashboard_envelope.py`), but no numeric leg of the dispatch
signature moves when a rate-change row is written: the stats legs are `MAX(id)`
over the two weekly snapshot tables plus the reset-event change signal, and the
two digest registries listed only the milestone tables. A write therefore left
the idle short-circuit serving the previous envelope.

The relation is added to BOTH registries, each provider-filtered, over exactly
the twelve columns the envelope publishes. `notified_at` is excluded because it
is not published, so arming a notification must NOT move the digest.
"""
from __future__ import annotations

import pytest

import _lib_dashboard_sources as sources
from conftest import load_script, redirect_paths


# The twelve columns `_alert_meter_rate_change_rows` publishes. `id` and
# `notified_at` are deliberately absent: `id` is a surrogate and `notified_at`
# is not published.
PUBLISHED_COLUMNS = (
    "provider",
    "account_key",
    "effective_from",
    "previous_units_per_point",
    "new_units_per_point",
    "severity",
    "detected_at_utc",
    "created_at_utc",
    "withholding_status",
    "detector_input_causes",
    "composition_provenance",
    "baseline_withheld_days",
)


def _relation(relations, name):
    for relation_name, query in relations:
        if relation_name == name:
            return query
    return None


@pytest.mark.parametrize("relations,expected_filter", [
    (sources._CLAUDE_STATS_DIGEST_RELATIONS, "provider='claude'"),
    (sources._CODEX_STATS_DIGEST_RELATIONS, "provider='codex'"),
])
def test_both_registries_carry_the_relation_each_provider_filtered(
    relations, expected_filter,
):
    query = _relation(relations, "meter_rate_change_events")
    assert query is not None, (
        "the registry does not list meter_rate_change_events at all"
    )
    assert expected_filter in query, query
    assert "ORDER BY" in query, query


def test_the_relation_selects_exactly_the_twelve_published_columns():
    """Not the whole table: the digest is an identity over what is published."""
    for relations in (sources._CLAUDE_STATS_DIGEST_RELATIONS,
                      sources._CODEX_STATS_DIGEST_RELATIONS):
        query = _relation(relations, "meter_rate_change_events")
        assert query is not None
        selected = query.split(" FROM ")[0]
        assert selected.upper().startswith("SELECT ")
        columns = tuple(
            part.strip() for part in selected[len("SELECT "):].split(",")
        )
        assert columns == PUBLISHED_COLUMNS, columns
        # `notified_at` is not published, so it must not enter the digest at
        # all — not in the projection and not in the ordering.
        assert "notified_at" not in query, query


@pytest.fixture
def ns(monkeypatch, tmp_path):
    namespace = load_script()
    redirect_paths(namespace, monkeypatch, tmp_path)
    return namespace


def _seed_rate_change(
    conn,
    *,
    provider,
    effective_from="2026-09-01T00:00:00Z",
    previous=1.0,
    new=2.0,
    severity="info",
    account_key="unattributed",
    notified_at=None,
):
    conn.execute(
        "INSERT INTO meter_rate_change_events "
        "(provider, account_key, effective_from, previous_units_per_point, "
        " new_units_per_point, severity, detected_at_utc, created_at_utc, "
        " notified_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '2026-09-01T00:05:00Z', "
        "        '2026-09-01T00:05:00Z', ?)",
        (provider, account_key, effective_from, previous, new, severity,
         notified_at),
    )


def _digests(stats):
    return (
        sources.claude_stats_digest(stats),
        sources.codex_stats_digest(stats),
    )


@pytest.mark.parametrize("provider,index", [("claude", 0), ("codex", 1)])
def test_an_isolated_rate_change_insert_moves_that_providers_digest(
    ns, provider, index,
):
    """An otherwise idle store, one insert, no unrelated write."""
    stats = ns["open_db"]()
    try:
        before = _digests(stats)
        _seed_rate_change(stats, provider=provider)
        stats.commit()
        after = _digests(stats)
    finally:
        stats.close()
    assert before[index] != after[index], (
        f"the {provider} digest did not move on a {provider} rate-change insert"
    )
    other = 1 - index
    assert before[other] == after[other], (
        "the other provider's digest moved on a row it does not publish"
    )


@pytest.mark.parametrize("provider,index", [("claude", 0), ("codex", 1)])
def test_an_isolated_rate_change_insert_moves_the_dispatch_signature(
    ns, provider, index,
):
    """The signature is what `/api/data`'s idle short-circuit compares.

    `_snapshot_data_version` folds both digests, so a moved digest is what
    republishes the envelope on the next tick.
    """
    import _lib_snapshot_cache as sc
    import _cctally_tui as tui

    stats = ns["open_db"]()
    cache = ns["open_cache_db"]()
    try:
        def signature():
            return sc.compute_signature(
                cache, stats, generation=0,
                claude_stats_digest=sources.claude_stats_digest(stats),
                codex_stats_digest=sources.codex_stats_digest(stats),
            )

        before = signature()
        _seed_rate_change(stats, provider=provider)
        stats.commit()
        after = signature()
    finally:
        cache.close()
        stats.close()
    assert before != after
    assert tui._snapshot_data_version(before) != tui._snapshot_data_version(after)


@pytest.mark.parametrize("provider,index", [("claude", 0), ("codex", 1)])
def test_a_semantic_update_moves_the_digest(ns, provider, index):
    """An in-place rate correction adds no row anywhere."""
    stats = ns["open_db"]()
    try:
        _seed_rate_change(stats, provider=provider)
        stats.commit()
        before = _digests(stats)
        stats.execute(
            "UPDATE meter_rate_change_events "
            "SET new_units_per_point = 3.5, severity = 'warn' "
            "WHERE provider = ?",
            (provider,),
        )
        stats.commit()
        after = _digests(stats)
    finally:
        stats.close()
    assert before[index] != after[index]


@pytest.mark.parametrize("provider,index", [("claude", 0), ("codex", 1)])
def test_a_delete_moves_the_digest(ns, provider, index):
    stats = ns["open_db"]()
    try:
        _seed_rate_change(stats, provider=provider)
        stats.commit()
        before = _digests(stats)
        stats.execute(
            "DELETE FROM meter_rate_change_events WHERE provider = ?",
            (provider,),
        )
        stats.commit()
        after = _digests(stats)
    finally:
        stats.close()
    assert before[index] != after[index]


@pytest.mark.parametrize("provider,index", [("claude", 0), ("codex", 1)])
def test_a_notified_at_only_change_does_not_move_the_digest(ns, provider, index):
    """`notified_at` is not published, so arming a notification is not a
    republication reason. Folding it in would rebuild the whole envelope on
    every notifier pass."""
    stats = ns["open_db"]()
    try:
        _seed_rate_change(stats, provider=provider)
        stats.commit()
        before = _digests(stats)
        stats.execute(
            "UPDATE meter_rate_change_events "
            "SET notified_at = '2026-09-01T00:09:00Z' WHERE provider = ?",
            (provider,),
        )
        stats.commit()
        after = _digests(stats)
    finally:
        stats.close()
    assert before == after
