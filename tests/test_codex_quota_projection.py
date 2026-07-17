"""Durable Codex quota projection, recovery, and cost-correlation tests."""
from __future__ import annotations

import datetime as dt
import fcntl
import importlib
from pathlib import Path
import shutil
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
RESET = "2026-07-15T15:00:00+00:00"
CODEX_S1_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "codex-parity"
    / "v1"
    / "rollouts"
    / "modern-full.jsonl"
)


def _iso(hour: int, minute: int = 0) -> str:
    return dt.datetime(2026, 7, 15, hour, minute, tzinfo=UTC).isoformat()


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    quota = importlib.import_module("_cctally_quota")
    return ns, quota


def _seed_quota(
    ns,
    *,
    root_key: str,
    source_path: str,
    observations: list[tuple[str, int, float]],
    logical_limit_key: str = "limit-primary",
    observed_slot: str = "primary",
    window_minutes: int = 300,
):
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            """INSERT INTO codex_source_roots
               (source_root_key, canonical_root_path, first_seen_utc, last_seen_utc)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source_root_key) DO UPDATE SET
                 canonical_root_path=excluded.canonical_root_path,
                 last_seen_utc=excluded.last_seen_utc""",
            (root_key, f"/codex/{root_key}", _iso(10), _iso(10)),
        )
        conn.executemany(
            """INSERT INTO quota_window_snapshots
               (source, source_root_key, source_path, line_offset,
                captured_at_utc, observed_slot, logical_limit_key, limit_id,
                limit_name, window_minutes, used_percent, resets_at_utc,
                plan_type, individual_limit_json, reached_type)
               VALUES ('codex', ?, ?, ?, ?, ?, ?, 'native-primary', 'Primary',
                       ?, ?, ?, 'pro', NULL, NULL)""",
            [
                (
                    root_key, source_path, offset, captured_at, observed_slot,
                    logical_limit_key, window_minutes, used_percent, RESET,
                )
                for captured_at, offset, used_percent in observations
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _projection_rows(ns, table: str):
    conn = ns["open_db"]()
    try:
        order = "source_root_key" if table == "quota_projection_state" else "id"
        return conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
    finally:
        conn.close()


def _stage_real_s1_codex_root(tmp_path, monkeypatch):
    provider_root = tmp_path / "fake-codex-home"
    rollout = provider_root / "sessions" / "2026" / "07" / "15" / "rollout-s1.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CODEX_S1_FIXTURE, rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return provider_root


def test_reconciliation_materializes_root_qualified_blocks_milestones_and_state(
    tmp_path, monkeypatch
):
    ns, quota = _load(tmp_path, monkeypatch)
    _seed_quota(
        ns,
        root_key="root-a",
        source_path="/codex/root-a/rollout.jsonl",
        observations=[(_iso(10), 10, 10.2), (_iso(11), 20, 13.1)],
    )
    _seed_quota(
        ns,
        root_key="root-b",
        source_path="/codex/root-b/rollout.jsonl",
        observations=[(_iso(10), 10, 20.0), (_iso(11), 20, 21.0)],
    )

    result = quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a", "root-b"}, now=dt.datetime(2026, 7, 15, 12, tzinfo=UTC)
    )

    assert result.blocks_upserted == 2
    blocks = _projection_rows(ns, "quota_window_blocks")
    assert [(row["source_root_key"], row["first_percent"], row["current_percent"])
            for row in blocks] == [("root-a", 10.2, 13.1), ("root-b", 20.0, 21.0)]
    milestones = _projection_rows(ns, "quota_percent_milestones")
    assert [(row["source_root_key"], row["percent_threshold"])
            for row in milestones] == [
                ("root-a", 11), ("root-a", 12), ("root-a", 13), ("root-b", 21),
            ]
    states = _projection_rows(ns, "quota_projection_state")
    assert [row["source_root_key"] for row in states] == ["root-a", "root-b"]
    assert all(len(row["generation"]) >= 32 for row in states)
    assert all(len(row["physical_signature"]) == 64 for row in states)


def test_partial_physical_window_is_skipped_without_hiding_valid_root(
    tmp_path, monkeypatch
):
    ns, quota = _load(tmp_path, monkeypatch)
    _seed_quota(
        ns,
        root_key="root-valid",
        source_path="/codex/root-valid/rollout.jsonl",
        observations=[(_iso(10), 10, 10.0)],
    )
    _seed_quota(
        ns,
        root_key="root-partial",
        source_path="/codex/root-partial/rollout.jsonl",
        observations=[],
    )
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            """INSERT INTO quota_window_snapshots
               (source, source_root_key, source_path, line_offset,
                captured_at_utc, observed_slot, logical_limit_key, limit_id,
                limit_name, window_minutes, used_percent, resets_at_utc)
               VALUES ('codex', 'root-partial', '/codex/root-partial/rollout.jsonl', 10,
                       ?, NULL, 'limit-primary', 'native-primary', 'Primary', 300, 10, ?)""",
            (_iso(10), RESET),
        )
        cache.commit()
    finally:
        cache.close()

    observations = quota.load_codex_quota_observations()

    assert [(item.identity.source_root_key, item.identity.observed_slot) for item in observations] == [
        ("root-valid", "primary"),
    ]


def test_codex_sync_reconciles_only_after_releasing_its_cache_lock(
    tmp_path, monkeypatch
):
    ns, quota = _load(tmp_path, monkeypatch)
    observed: list[bool] = []

    def reconcile_after_unlock():
        with open(ns["_cctally_core"].CACHE_LOCK_CODEX_PATH, "w") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append(True)
            fcntl.flock(lock_fh, fcntl.LOCK_UN)

    monkeypatch.setattr(quota, "reconcile_codex_quota_projection", reconcile_after_unlock)
    cache = ns["open_cache_db"]()
    try:
        result = ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    assert result.lock_contended is False
    assert observed == [True]


def test_reconciliation_rolls_back_before_stats_commit_then_heals_orphans_and_reappearance(
    tmp_path, monkeypatch
):
    ns, quota = _load(tmp_path, monkeypatch)
    _seed_quota(
        ns,
        root_key="root-a",
        source_path="/codex/root-a/rollout.jsonl",
        observations=[(_iso(10), 10, 10.0), (_iso(11), 20, 12.0)],
    )
    quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, now=dt.datetime(2026, 7, 15, 12, tzinfo=UTC)
    )

    # Simulate a committed cache prune.  A projection failure must leave the
    # preceding complete stats generation visible; retry then marks rows orphaned.
    cache = ns["open_cache_db"]()
    try:
        cache.execute("DELETE FROM quota_window_snapshots WHERE source_root_key='root-a'")
        cache.commit()
    finally:
        cache.close()

    with pytest.raises(RuntimeError, match="before commit"):
        quota.reconcile_codex_quota_projection(
            source_root_keys={"root-a"},
            now=dt.datetime(2026, 7, 15, 12, tzinfo=UTC),
            _before_stats_commit=lambda: (_ for _ in ()).throw(RuntimeError("before commit")),
        )
    assert all(row["orphaned_at"] is None for row in _projection_rows(ns, "quota_window_blocks"))

    quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, now=dt.datetime(2026, 7, 15, 12, tzinfo=UTC)
    )
    assert all(row["orphaned_at"] is not None for row in _projection_rows(ns, "quota_window_blocks"))

    # Same stable physical block reappearing clears the orphan marker instead
    # of duplicating milestones or terminal claims.
    _seed_quota(
        ns,
        root_key="root-a",
        source_path="/codex/root-a/rollout.jsonl",
        observations=[(_iso(10), 10, 10.0), (_iso(11), 20, 12.0)],
    )
    quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, now=dt.datetime(2026, 7, 15, 12, tzinfo=UTC)
    )
    assert len(_projection_rows(ns, "quota_window_blocks")) == 1
    assert len(_projection_rows(ns, "quota_percent_milestones")) == 2
    assert all(row["orphaned_at"] is None for row in _projection_rows(ns, "quota_window_blocks"))


def test_terminal_threshold_rows_survive_rebuild_and_after_commit_interruption(
    tmp_path, monkeypatch
):
    ns, quota = _load(tmp_path, monkeypatch)
    _seed_quota(
        ns,
        root_key="root-a",
        source_path="/codex/root-a/rollout.jsonl",
        observations=[(_iso(10), 10, 95.0)],
    )
    quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, now=dt.datetime(2026, 7, 15, 12, tzinfo=UTC)
    )
    stats = ns["open_db"]()
    try:
        stats.execute(
            """INSERT INTO quota_threshold_events
               (source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, resets_at_utc, threshold, qualifying_kind,
                qualifying_percent, severity, created_at_utc, disposition, alerted_at)
               VALUES ('codex', 'root-a', 'limit-primary', 'primary', 300, ?, 90,
                       'actual', 95, 'warn', ?, 'alerted', ?)""",
            (RESET, _iso(10), _iso(10)),
        )
        stats.commit()
    finally:
        stats.close()

    cache = ns["open_cache_db"]()
    try:
        cache.execute("DELETE FROM quota_window_snapshots WHERE source_root_key='root-a'")
        cache.commit()
    finally:
        cache.close()

    with pytest.raises(RuntimeError, match="after commit"):
        quota.reconcile_codex_quota_projection(
            source_root_keys={"root-a"},
            now=dt.datetime(2026, 7, 15, 12, tzinfo=UTC),
            _after_stats_commit=lambda: (_ for _ in ()).throw(RuntimeError("after commit")),
        )
    orphaned = _projection_rows(ns, "quota_threshold_events")
    assert len(orphaned) == 1
    assert orphaned[0]["orphaned_at"] is not None

    _seed_quota(
        ns,
        root_key="root-a",
        source_path="/codex/root-a/rollout.jsonl",
        observations=[(_iso(10), 10, 95.0)],
    )
    quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, now=dt.datetime(2026, 7, 15, 12, tzinfo=UTC)
    )
    events = _projection_rows(ns, "quota_threshold_events")
    assert len(events) == 1
    assert events[0]["disposition"] == "alerted"
    assert events[0]["orphaned_at"] is None


def test_real_s1_rebuild_recovery_preserves_generation_and_terminal_claims(
    tmp_path, monkeypatch
):
    """Use the real S1 ingest/rebuild path, never a direct cache-row delete."""
    ns, quota = _load(tmp_path, monkeypatch)
    provider_root = _stage_real_s1_codex_root(tmp_path, monkeypatch)
    cache = ns["open_cache_db"]()
    original_reconcile = quota.reconcile_codex_quota_projection
    try:
        ns["sync_codex_cache"](cache, rebuild=True)
        initial_state = _projection_rows(ns, "quota_projection_state")
        assert len(initial_state) == 1
        prior_generation = initial_state[0]["generation"]
        prior_signature = initial_state[0]["physical_signature"]

        def fail_before_stats_commit():
            def raise_before_commit():
                raise RuntimeError("before stats commit")

            return original_reconcile(_before_stats_commit=raise_before_commit)

        monkeypatch.setattr(
            quota, "reconcile_codex_quota_projection", fail_before_stats_commit
        )
        with pytest.raises(RuntimeError, match="before stats commit"):
            ns["sync_codex_cache"](cache, rebuild=True)

        after_pre_failure = _projection_rows(ns, "quota_projection_state")
        assert [
            (row["generation"], row["physical_signature"])
            for row in after_pre_failure
        ] == [(prior_generation, prior_signature)]

        monkeypatch.setattr(
            quota, "reconcile_codex_quota_projection", original_reconcile
        )
        ns["sync_codex_cache"](cache, rebuild=True)
        healed_state = _projection_rows(ns, "quota_projection_state")
        assert len(healed_state) == 1
        assert healed_state[0]["physical_signature"] == prior_signature
        assert healed_state[0]["generation"] != prior_generation

        block = _projection_rows(ns, "quota_window_blocks")[0]
        stats = ns["open_db"]()
        try:
            stats.execute(
                """INSERT INTO quota_threshold_events
                   (source, source_root_key, logical_limit_key, observed_slot,
                    window_minutes, resets_at_utc, threshold, qualifying_kind,
                    qualifying_percent, severity, created_at_utc, disposition, alerted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    block["source"],
                    block["source_root_key"],
                    block["logical_limit_key"],
                    block["observed_slot"],
                    block["window_minutes"],
                    block["resets_at_utc"],
                    90,
                    "actual",
                    90.0,
                    "warn",
                    "2026-07-15T00:00:00Z",
                    "alerted",
                    "2026-07-15T00:01:00Z",
                ),
            )
            stats.commit()
        finally:
            stats.close()

        # A real rebuild from an empty configured root prunes physical rows
        # and leaves the terminal claim intact but historical.
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex-sessions"))
        ns["sync_codex_cache"](cache, rebuild=True)
        orphaned_events = _projection_rows(ns, "quota_threshold_events")
        assert len(orphaned_events) == 1
        assert orphaned_events[0]["disposition"] == "alerted"
        assert orphaned_events[0]["orphaned_at"] is not None

        def fail_after_stats_commit():
            def raise_after_commit():
                raise RuntimeError("after stats commit")

            return original_reconcile(_after_stats_commit=raise_after_commit)

        monkeypatch.setenv("CODEX_HOME", str(provider_root))
        monkeypatch.setattr(
            quota, "reconcile_codex_quota_projection", fail_after_stats_commit
        )
        with pytest.raises(RuntimeError, match="after stats commit"):
            ns["sync_codex_cache"](cache, rebuild=True)

        retained_events = _projection_rows(ns, "quota_threshold_events")
        assert len(retained_events) == 1
        assert retained_events[0]["disposition"] == "alerted"
        assert retained_events[0]["orphaned_at"] is None

        monkeypatch.setattr(
            quota, "reconcile_codex_quota_projection", original_reconcile
        )
        ns["sync_codex_cache"](cache, rebuild=True)
        final_events = _projection_rows(ns, "quota_threshold_events")
        assert len(final_events) == 1
        assert final_events[0]["disposition"] == "alerted"
        assert final_events[0]["orphaned_at"] is None
    finally:
        cache.close()


def test_breakdown_correlates_root_qualified_physical_tuples_and_reprices_at_read_time(
    tmp_path, monkeypatch
):
    ns, quota = _load(tmp_path, monkeypatch)
    _seed_quota(
        ns,
        root_key="root-a",
        source_path="/codex/root-a/rollout.jsonl",
        observations=[(_iso(10), 10, 10.0), (_iso(10), 20, 12.0)],
    )
    _seed_quota(
        ns,
        root_key="root-b",
        source_path="/codex/root-b/rollout.jsonl",
        observations=[(_iso(10), 10, 10.0), (_iso(10), 20, 12.0)],
    )
    cache = ns["open_cache_db"]()
    try:
        cache.executemany(
            """INSERT INTO codex_session_entries
               (source_path, line_offset, timestamp_utc, session_id, model,
                input_tokens, cached_input_tokens, output_tokens,
                reasoning_output_tokens, total_tokens, source_root_key)
               VALUES (?, ?, ?, 'session', 'gpt-5', 1000, 0, 100, 0, 1100, ?)""",
            [
                ("/codex/root-a/rollout.jsonl", 15, _iso(10), "root-a"),
                ("/codex/root-a/rollout.jsonl", 20, _iso(10), "root-a"),
                ("/codex/root-b/rollout.jsonl", 15, _iso(10), "root-b"),
            ],
        )
        cache.commit()
    finally:
        cache.close()
    quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a", "root-b"}, now=dt.datetime(2026, 7, 15, 12, tzinfo=UTC)
    )

    identity = quota.QuotaWindowIdentity(
        source="codex", source_root_key="root-a", logical_limit_key="limit-primary",
        observed_slot="primary", window_minutes=300,
    )
    standard = quota.codex_quota_breakdown(identity, RESET, speed="standard")
    fast = quota.codex_quota_breakdown(identity, RESET, speed="fast")
    monkeypatch.setattr(sys.modules["cctally"], "_resolve_codex_speed", lambda value: "fast")
    automatic = quota.codex_quota_breakdown(identity, RESET, speed="auto")

    assert [row.percent for row in standard] == [11, 12]
    assert standard[-1].total_tokens == 2200
    assert standard[-1].cost_usd > 0
    assert fast[-1].cost_usd != standard[-1].cost_usd
    assert automatic[-1].cost_usd == fast[-1].cost_usd
