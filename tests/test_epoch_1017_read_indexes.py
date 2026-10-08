"""#901 §5.3 (T3) — epoch 1016 -> 1017: stats read indexes.

Every stats digest relation sorted its whole relation through a temp b-tree on
every dashboard build, and ``_fetch_current_week_snapshots`` grouped every
boundary-aware and legacy-date snapshot the same way. Epoch 1017 adds, for each
statement, an index in that statement's own order. No table or column changes,
and no digest byte or current-week row moves (asserted with and without the
indexes). The stats registry is frozen at 13, so this is an epoch bump, never a
migration. The same epoch carries Amendment 1's history-read indexes (items 2-7
and 9, ``c901-design-2``; item 2's second, single-root index from Amendment 1c,
``c901-design-3``), whose equivalence and plans
tests/test_901_stats_history_reads.py asserts.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import _sql_plan_guard as guard
from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 9, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
NOW = dt.datetime(2026, 7, 20, 12, tzinfo=dt.timezone.utc)
LEGACY_NOW = dt.datetime(2026, 6, 3, 12, tzinfo=dt.timezone.utc)
PREVIOUS_EPOCH = 1016
NEW_EPOCH = 1017
READ_INDEXES = (
    "idx_quota_projection_state_digest", "idx_quota_blocks_digest",
    "idx_quota_milestones_digest", "idx_quota_threshold_events_digest",
    "idx_budget_milestones_codex_digest",
    "idx_projected_milestones_codex_digest",
    "idx_percent_milestones_alert_digest",
    "idx_five_hour_milestones_alert_digest",
    "idx_budget_milestones_alert_digest",
    "idx_projected_milestones_alert_digest",
    "idx_project_budget_milestones_alert_digest",
    "idx_usage_week_boundary_group", "idx_usage_week_date_group",
    # Amendment 1 items 2-7 and 9 (``c901-design-2``): the history reads the
    # A1 guard escalated.
    "idx_quota_blocks_weekly_reset_order",
    "idx_quota_blocks_weekly_single_root_order",
    "idx_percent_milestones_week_date",
    "idx_quota_milestones_root", "idx_quota_threshold_events_root",
    "idx_quota_blocks_root_group_pairs", "idx_week_reset_events_effective_order",
    "idx_usage_subscription_anchor_order", "idx_usage_subscription_anchor_pick",
)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed_journal():
    """An epoch mismatch with NO journal is a hard error, never a silent
    rebuild-to-empty, so the rebuild test has to supply one."""
    import _cctally_journal as jr
    import _lib_journal as J

    jr.append_record(
        J.make_obs(
            at="2026-09-12T09:00:00Z", src="record-usage", provider="claude",
            payload={"weekly_percent": 12.0, "source": "statusline"},
        ),
        now_utc=FIXED,
    )


def _index_names(conn) -> set:
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


def _seed_weeks(conn) -> None:
    rows = []
    for hour in range(6):
        for held in (0, 1):
            rows.append((
                f"2026-07-1{5 + hour % 3}T0{hour}:00:00Z", "2026-07-14",
                "2026-07-21", "2026-07-14T00:00:00+00:00",
                "2026-07-21T00:00:00+00:00", 10.0 + hour, "{}",
                "unattributed", held))
    for day in range(1, 8):
        rows.append((
            f"2026-06-0{day}T09:00:00Z", "2026-06-01", "2026-06-08", None,
            None, 5.0 + day, "{}", "unattributed", 0))
    conn.executemany(
        "INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date,"
        " week_end_date, week_start_at, week_end_at, weekly_percent,"
        " payload_json, account_key, weekly_observation_held)"
        " VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def _seed_quota_milestones(conn) -> None:
    conn.executemany(
        "INSERT INTO quota_percent_milestones (source, source_root_key,"
        " logical_limit_key, observed_slot, window_minutes, resets_at_utc,"
        " percent_threshold, captured_at_utc, source_path, line_offset,"
        " high_water_percent, generation, orphaned_at, account_key)"
        " VALUES ('codex',?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("r" * 32, key, "primary", 10080, "2026-07-21T00:00:00Z", threshold,
          f"2026-07-1{threshold % 9}T00:00:00Z", "/p.jsonl", threshold,
          threshold, "g1", None, "unattributed")
         for key in ("limit-b", "limit-a") for threshold in range(1, 6)])
    conn.commit()


def test_the_epoch_is_bumped_and_the_legacy_registry_is_untouched():
    import _cctally_core
    import _cctally_db

    assert _cctally_core.STATS_INDEX_EPOCH == NEW_EPOCH
    assert _cctally_core.LEGACY_STATS_HEAD == 13
    assert len(_cctally_db._STATS_MIGRATIONS) == 13, "the registry is FROZEN"


def test_a_fresh_index_carries_every_read_index(ns):
    conn = ns["open_db"]()
    try:
        names = _index_names(conn)
    finally:
        conn.close()
    assert set(READ_INDEXES) <= names


def test_the_rebuild_contract_names_every_read_index():
    import _cctally_journal as jr

    assert set(READ_INDEXES) <= jr._REBUILD_REQUIRED_INDEXES


def test_the_schema_fingerprint_moved_with_the_epoch(ns):
    import _cctally_journal as jr

    conn = ns["open_db"]()
    try:
        fingerprint = jr._stats_schema_fingerprint(conn)
        assert fingerprint == jr._REBUILD_SCHEMA_FINGERPRINT, (
            f"the shipped schema hashes to {fingerprint}")
        conn.execute("DROP INDEX idx_usage_week_date_group")
        assert jr._stats_schema_fingerprint(conn) != (
            jr._REBUILD_SCHEMA_FINGERPRINT), (
            "the committed fingerprint does not cover the read indexes")
    finally:
        conn.close()


def test_every_digest_relation_streams_through_an_index(ns):
    import _cctally_core
    import _lib_dashboard_sources as lds

    ns["open_db"]().close()
    with guard.capture_sql_plans() as recorder:
        conn = sqlite3.connect(_cctally_core.DB_PATH)
        try:
            lds._stats_relations_digest(conn, lds._CODEX_STATS_DIGEST_RELATIONS)
            lds._stats_relations_digest(conn, lds._CLAUDE_STATS_DIGEST_RELATIONS)
        finally:
            conn.close()
    digests = [statement for statement in recorder.statements
               if "_lib_dashboard_sources._stats_relations_digest"
               in statement.call_sites]
    assert len(digests) == (len(lds._CODEX_STATS_DIGEST_RELATIONS)
                            + len(lds._CLAUDE_STATS_DIGEST_RELATIONS))
    offenders = [(statement.sql, statement.plan) for statement in digests
                 if any(marker in detail for detail in statement.plan
                        for marker in guard.TEMP_MARKERS)]
    assert offenders == []


def test_current_week_reads_stream_and_only_bounded_sorts_remain(ns):
    import _cctally_core
    import _cctally_forecast as forecast

    conn = ns["open_db"]()
    try:
        _seed_weeks(conn)
    finally:
        conn.close()
    with guard.capture_sql_plans() as recorder:
        conn = sqlite3.connect(_cctally_core.DB_PATH)
        try:
            for account_key in (None, "unattributed"):
                for include_held in (False, True):
                    assert forecast._fetch_current_week_snapshots(
                        conn, NOW, account_key=account_key,
                        include_held=include_held) is not None
            # No boundary-aware week contains this clock: the legacy-date leg.
            assert forecast._fetch_current_week_snapshots(
                conn, LEGACY_NOW) is not None
        finally:
            conn.close()
    grouping = [statement for statement in recorder.statements
                if "GROUP BY week_start" in statement.sql]
    assert len(grouping) >= 5, [statement.sql for statement in grouping]
    unexplained, _stale = guard.classify(
        recorder.statements, allowlist=guard.ALLOWLIST, pending=())
    assert unexplained == [], guard.format_findings(unexplained)


def test_results_do_not_depend_on_the_read_indexes(ns):
    import _cctally_core
    import _cctally_forecast as forecast
    import _lib_dashboard_sources as lds

    conn = ns["open_db"]()
    try:
        _seed_weeks(conn)
        _seed_quota_milestones(conn)
    finally:
        conn.close()

    def observe():
        connection = sqlite3.connect(_cctally_core.DB_PATH)
        try:
            return (
                lds._stats_relations_digest(
                    connection, lds._CODEX_STATS_DIGEST_RELATIONS),
                lds._stats_relations_digest(
                    connection, lds._CLAUDE_STATS_DIGEST_RELATIONS),
                [forecast._fetch_current_week_snapshots(
                    connection, NOW, account_key=account_key,
                    include_held=include_held)
                 for account_key in (None, "unattributed")
                 for include_held in (False, True)],
                forecast._fetch_current_week_snapshots(connection, LEGACY_NOW),
            )
        finally:
            connection.close()

    with_indexes = observe()
    connection = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        for name in READ_INDEXES:
            connection.execute(f"DROP INDEX {name}")
        connection.commit()
    finally:
        connection.close()
    assert observe() == with_indexes


def test_the_indexes_reach_an_upgraded_install_via_rebuild(ns):
    import _cctally_core
    import _cctally_store as store

    _seed_journal()
    conn = ns["open_db"]()
    try:
        for name in READ_INDEXES:
            conn.execute(f"DROP INDEX {name}")
        conn.execute(f"PRAGMA user_version = {PREVIOUS_EPOCH}")
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == PREVIOUS_EPOCH
        assert not (_index_names(conn) & set(READ_INDEXES))
    finally:
        conn.close()
    conn = store.resolve_stats_epoch_mismatch()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == NEW_EPOCH
        names = _index_names(conn)
    finally:
        conn.close()
    assert set(READ_INDEXES) <= names
