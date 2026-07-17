"""Per-migration goldens for stats 013 Codex quota projection state."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


MIGRATION = "013_codex_quota_projection_state"
PREDECESSOR = "012_unify_budget_milestones_vendor"
FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

# Required by the registry-completeness guard.  The rerun assertion below
# exercises the handler's post-DDL/pre-central-stamp idempotency.
IDEMPOTENCY_COVERED = True


def _handler(ns):
    for migration in ns["_STATS_MIGRATIONS"]:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"missing {MIGRATION}")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _user_indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA index_list({table})")
        if not str(row[1]).startswith("sqlite_autoindex")
    }


def test_013_is_contiguous_after_012():
    ns = load_script()
    assert [migration.name for migration in ns["_STATS_MIGRATIONS"]][-2:] == [
        PREDECESSOR,
        MIGRATION,
    ]
    assert ns["_STATS_MIGRATIONS"][-1].seq == 13


def test_fresh_schema_has_all_quota_projection_tables_and_invariants(tmp_path, monkeypatch):
    ns = load_script()
    from conftest import redirect_paths

    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    try:
        expected = {
            "quota_window_blocks": {
                "source", "source_root_key", "logical_limit_key", "observed_slot",
                "window_minutes", "resets_at_utc", "first_observed_at_utc",
                "last_observed_at_utc", "first_percent", "current_percent",
                "last_source_path", "last_line_offset", "generation", "orphaned_at",
            },
            "quota_percent_milestones": {
                "source", "source_root_key", "logical_limit_key", "observed_slot",
                "window_minutes", "resets_at_utc", "percent_threshold",
                "captured_at_utc", "source_path", "line_offset", "high_water_percent",
                "generation", "orphaned_at",
            },
            "quota_threshold_events": {
                "source", "source_root_key", "logical_limit_key", "observed_slot",
                "window_minutes", "resets_at_utc", "threshold", "qualifying_kind",
                "disposition", "alerted_at", "suppressed_at", "orphaned_at",
            },
            "quota_projection_state": {
                "source_root_key", "generation", "physical_signature", "completed_at_utc",
            },
            "quota_alert_arming": {
                "source", "source_root_key", "logical_limit_key", "observed_slot",
                "window_minutes", "rule_fingerprint", "activated_at_utc",
            },
        }
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(expected) <= names
        for table, columns in expected.items():
            assert columns <= _table_columns(conn, table), table

        assert "idx_quota_blocks_active" in _user_indexes(conn, "quota_window_blocks")
        assert "idx_quota_milestones_active" in _user_indexes(
            conn, "quota_percent_milestones"
        )
        assert "idx_quota_threshold_events_active" in _user_indexes(
            conn, "quota_threshold_events"
        )

        # Stable keys must exclude label metadata and threshold kind so a label
        # update cannot create duplicate state and actual/projected share one
        # terminal threshold lifecycle.
        conn.execute(
            """INSERT INTO quota_window_blocks
               (source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, resets_at_utc, nominal_start_at_utc,
                first_observed_at_utc, last_observed_at_utc, first_percent,
                current_percent, last_source_path, last_line_offset, generation)
               VALUES ('codex', 'root-a', 'limit-a', 'primary', 300,
                       '2026-07-15T15:00:00Z', '2026-07-15T10:00:00Z',
                       '2026-07-15T10:00:00Z', '2026-07-15T10:00:00Z',
                       10, 10, '/rollout.jsonl', 1, 'generation-a')"""
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO quota_window_blocks
                   (source, source_root_key, logical_limit_key, observed_slot,
                    window_minutes, resets_at_utc, nominal_start_at_utc,
                    first_observed_at_utc, last_observed_at_utc, first_percent,
                    current_percent, last_source_path, last_line_offset, generation)
                   VALUES ('codex', 'root-a', 'limit-a', 'primary', 300,
                           '2026-07-15T15:00:00Z', '2026-07-15T10:00:00Z',
                           '2026-07-15T10:00:00Z', '2026-07-15T10:00:00Z',
                           10, 10, '/rollout.jsonl', 1, 'generation-b')"""
            )
        conn.rollback()

        conn.execute(
            """INSERT INTO quota_threshold_events
               (source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, resets_at_utc, threshold, qualifying_kind,
                qualifying_percent, severity, created_at_utc, disposition, alerted_at)
               VALUES ('codex', 'root-a', 'limit-a', 'primary', 300,
                       '2026-07-15T15:00:00Z', 90, 'actual', 95, 'warn',
                       '2026-07-15T10:00:00Z', 'alerted', '2026-07-15T10:00:00Z')"""
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO quota_threshold_events
                   (source, source_root_key, logical_limit_key, observed_slot,
                    window_minutes, resets_at_utc, threshold, qualifying_kind,
                    projected_percent, severity, created_at_utc, disposition, suppressed_at)
                   VALUES ('codex', 'root-a', 'limit-a', 'primary', 300,
                           '2026-07-15T15:00:00Z', 90, 'projected', 95, 'warn',
                           '2026-07-15T10:01:00Z', 'suppressed_backfill',
                           '2026-07-15T10:01:00Z')"""
            )
        conn.rollback()
    finally:
        conn.close()


def test_fresh_schema_enforces_all_projection_stable_keys_and_terminal_checks(
    tmp_path, monkeypatch
):
    ns = load_script()
    from conftest import redirect_paths

    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    try:
        def assert_duplicate_rejected(sql, params, conflicting_params):
            conn.execute(sql, params)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, conflicting_params)
            conn.rollback()

        assert_duplicate_rejected(
            """
            INSERT INTO quota_window_blocks (
                source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, resets_at_utc, nominal_start_at_utc,
                first_observed_at_utc, last_observed_at_utc, first_percent,
                current_percent, last_source_path, last_line_offset, generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "codex", "root", "limit", "primary", 300,
                "2026-07-20T00:00:00Z", "2026-07-13T00:00:00Z",
                "2026-07-15T00:00:00Z", "2026-07-15T00:00:00Z",
                12.5, 12.5, "/tmp/source.jsonl", 1, "generation",
            ),
            (
                "codex", "root", "limit", "primary", 300,
                "2026-07-20T00:00:00Z", "2026-07-13T00:00:00Z",
                "2026-07-15T00:00:00Z", "2026-07-15T00:01:00Z",
                12.5, 13.0, "/tmp/updated-source.jsonl", 2, "next-generation",
            ),
        )
        assert_duplicate_rejected(
            """
            INSERT INTO quota_percent_milestones (
                source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, resets_at_utc, percent_threshold,
                captured_at_utc, source_path, line_offset, high_water_percent,
                generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "codex", "root", "limit", "primary", 300,
                "2026-07-20T00:00:00Z", 12, "2026-07-15T00:00:00Z",
                "/tmp/source.jsonl", 1, 12, "generation",
            ),
            (
                "codex", "root", "limit", "primary", 300,
                "2026-07-20T00:00:00Z", 12, "2026-07-15T00:01:00Z",
                "/tmp/updated-source.jsonl", 2, 13, "next-generation",
            ),
        )
        assert_duplicate_rejected(
            """
            INSERT INTO quota_projection_state (
                source_root_key, generation, physical_signature, completed_at_utc
            ) VALUES (?, ?, ?, ?)
            """,
            ("root", "generation", "signature", "2026-07-15T00:00:00Z"),
            ("root", "next-generation", "next-signature", "2026-07-15T00:01:00Z"),
        )
        assert_duplicate_rejected(
            """
            INSERT INTO quota_alert_arming (
                source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, rule_fingerprint, activated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "codex", "root", "limit", "primary", 300, "rule",
                "2026-07-15T00:00:00Z",
            ),
            (
                "codex", "root", "limit", "primary", 300, "next-rule",
                "2026-07-15T00:01:00Z",
            ),
        )
        assert_duplicate_rejected(
            """
            INSERT INTO quota_threshold_events (
                source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, resets_at_utc, threshold, qualifying_kind,
                qualifying_percent, severity, created_at_utc, disposition, alerted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "codex", "root", "limit", "primary", 300,
                "2026-07-20T00:00:00Z", 90, "actual", 90.0, "warn",
                "2026-07-15T00:00:00Z", "alerted", "2026-07-15T00:01:00Z",
            ),
            (
                "codex", "root", "limit", "primary", 300,
                "2026-07-20T00:00:00Z", 90, "projected", 90.0, "warn",
                "2026-07-15T00:01:00Z", "alerted", "2026-07-15T00:02:00Z",
            ),
        )

        invalid_terminal_events = (
            (91, "alerted", None, None),
            (92, "suppressed_backfill", None, None),
            (
                93,
                "alerted",
                "2026-07-15T00:01:00Z",
                "2026-07-15T00:02:00Z",
            ),
        )
        for threshold, disposition, alerted_at, suppressed_at in invalid_terminal_events:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO quota_threshold_events (
                        source, source_root_key, logical_limit_key, observed_slot,
                        window_minutes, resets_at_utc, threshold, qualifying_kind,
                        qualifying_percent, severity, created_at_utc, disposition,
                        alerted_at, suppressed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "codex", "root", "limit", "primary", 300,
                        "2026-07-20T00:00:00Z", threshold, "actual",
                        float(threshold), "warn", "2026-07-15T00:00:00Z",
                        disposition, alerted_at, suppressed_at,
                    ),
                )
            conn.rollback()
    finally:
        conn.close()


def test_013_handler_matches_golden_and_is_idempotent(tmp_path):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    ns = load_script()
    work = tmp_path / "stats.sqlite"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _handler(ns)(conn)
        conn.commit()
        actual = "\n".join(conn.iterdump())
        expected_conn = sqlite3.connect(POST_DB)
        try:
            expected = "\n".join(expected_conn.iterdump())
        finally:
            expected_conn.close()
        assert actual == expected
        before = "\n".join(conn.iterdump())
        _handler(ns)(conn)
        conn.commit()
        assert "\n".join(conn.iterdump()) == before
    finally:
        conn.close()
