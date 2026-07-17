"""Codex source invalidation contracts for #294 S4 Stage 1."""
from __future__ import annotations

import dataclasses
import fcntl
import json
import math
import os
import pathlib
import shutil
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

import _cctally_db as cache_db
from _cctally_dashboard_sources import (
    DashboardReadContext,
    build_codex_source_state,
    source_detail_lookup,
)
from _lib_snapshot_cache import SnapshotSignature, compute_signature
from conftest import load_script, redirect_paths


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"


def _sync_setup(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "16" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return ns, provider_root, rollout, ns["open_cache_db"]()


def _physical_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key='codex_physical_mutation_seq'"
    ).fetchone()
    return 0 if row is None else int(row[0])


def test_codex_physical_mutation_sequence_tracks_metadata_only_and_reset_changes(
    tmp_path, monkeypatch,
):
    ns, _provider_root, rollout, conn = _sync_setup(tmp_path, monkeypatch)
    try:
        ns["sync_codex_cache"](conn)
        assert _physical_seq(conn) == 1
        max_id = conn.execute("SELECT MAX(id) FROM codex_session_entries").fetchone()[0]

        unchanged = ns["sync_codex_cache"](conn)
        assert unchanged.files_skipped_unchanged == 1
        assert _physical_seq(conn) == 1

        # A metadata-only tail changes the file commit while no accounting row
        # is added, so MAX(id) is deliberately flat but the sequence advances.
        rollout.write_bytes(rollout.read_bytes() + b"\n")
        ns["sync_codex_cache"](conn)
        assert conn.execute("SELECT MAX(id) FROM codex_session_entries").fetchone()[0] == max_id
        assert _physical_seq(conn) == 2

        rollout.write_text("{}\n", encoding="utf-8")
        ns["sync_codex_cache"](conn)
        assert _physical_seq(conn) == 3
    finally:
        conn.close()


def test_codex_physical_mutation_sequence_tracks_root_prune_and_rebuild(
    tmp_path, monkeypatch,
):
    ns, provider_root, _rollout, conn = _sync_setup(tmp_path, monkeypatch)
    try:
        ns["sync_codex_cache"](conn)
        assert _physical_seq(conn) == 1

        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent-root"))
        ns["sync_codex_cache"](conn)
        assert _physical_seq(conn) == 2

        monkeypatch.setenv("CODEX_HOME", str(provider_root))
        ns["sync_codex_cache"](conn)
        assert _physical_seq(conn) == 3

        ns["sync_codex_cache"](conn, rebuild=True)
        # Rebuild clears the old physical families and then commits the
        # reingested file batch in its own transaction.
        assert _physical_seq(conn) == 5
    finally:
        conn.close()


def test_codex_physical_sequence_rolls_back_with_its_surrounding_transaction(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    try:
        conn.execute("BEGIN")
        ns["_cctally_cache"]._bump_codex_physical_mutation_seq(conn)
        assert _physical_seq(conn) == 1
        conn.rollback()
        assert _physical_seq(conn) == 0
    finally:
        conn.close()


def test_snapshot_signature_trailing_source_legs_preserve_older_positional_callers():
    legacy = SnapshotSignature(1, 2, 3, (4, 5), 6, 7, 8)
    assert legacy.codex_physical_mutation_seq == 0
    assert legacy.codex_stats_digest == ""

    cache = sqlite3.connect(":memory:")
    stats = sqlite3.connect(":memory:")
    try:
        cache_db._apply_cache_schema(cache)
        before = compute_signature(cache, stats, generation=0)
        cache.execute(
            "INSERT INTO cache_meta(key, value) VALUES ('codex_physical_mutation_seq', '12')"
        )
        after = compute_signature(cache, stats, generation=0)

        assert before.codex_physical_mutation_seq == 0
        assert after.codex_physical_mutation_seq == 12
        assert after.codex_stats_digest == ""
    finally:
        cache.close()
        stats.close()


def test_snapshot_data_version_changes_when_only_codex_physical_sequence_changes(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        before = compute_signature(cache, stats, generation=0)
        cache.execute(
            "INSERT INTO cache_meta(key, value) VALUES ('codex_physical_mutation_seq', '1')"
        )
        after = compute_signature(cache, stats, generation=0)

        assert ns["_snapshot_data_version"](before) != ns["_snapshot_data_version"](after)
    finally:
        cache.close()
        stats.close()


def test_snapshot_signature_and_version_include_stable_codex_stats_digest(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        first = compute_signature(cache, stats, generation=0, codex_stats_digest="a" * 64)
        second = compute_signature(cache, stats, generation=0, codex_stats_digest="b" * 64)

        assert first.codex_stats_digest == "a" * 64
        assert ns["_snapshot_data_version"](first) != ns["_snapshot_data_version"](second)
    finally:
        cache.close()
        stats.close()


def test_dashboard_dispatch_signature_leaves_idle_path_on_stats_only_digest_change(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    try:
        monkeypatch.setattr(ns["_cctally_tui"], "codex_stats_digest", lambda _conn: "a" * 64)
        before = ns["_cctally_tui"]._tui_compute_dispatch_signature(stats)
        monkeypatch.setattr(ns["_cctally_tui"], "codex_stats_digest", lambda _conn: "b" * 64)
        after = ns["_cctally_tui"]._tui_compute_dispatch_signature(stats)

        assert before.codex_stats_digest == "a" * 64
        assert after.codex_stats_digest == "b" * 64
        assert before != after
    finally:
        stats.close()


def test_ordinary_tui_snapshot_does_no_codex_dashboard_work_and_has_no_source_bundle(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    calls = {"codex": 0}

    def unexpected_codex(*_args, **_kwargs):
        calls["codex"] += 1
        raise AssertionError("ordinary TUI must not ingest Codex")

    monkeypatch.setitem(ns, "sync_codex_cache", unexpected_codex)
    snap = ns["_tui_build_snapshot"](
        now_utc=ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc),
        skip_sync=True,
        precompute_envelope=False,
    )

    assert calls == {"codex": 0}
    assert snap.source_bundle is None


def test_dashboard_precompute_coordinates_both_ingests_once_and_publishes_complete_bundle(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    calls = {"claude": 0, "codex": 0}

    def claude_ingest(_conn):
        calls["claude"] += 1
        return SimpleNamespace(lock_contended=False)

    def codex_ingest(_conn):
        calls["codex"] += 1
        return SimpleNamespace(lock_contended=False)

    monkeypatch.setitem(ns, "sync_cache", claude_ingest)
    monkeypatch.setitem(ns, "sync_codex_cache", codex_ingest)
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)

    snap = ns["_tui_build_snapshot"](
        now_utc=now,
        skip_sync=False,
        precompute_envelope=True,
        runtime_bind="127.0.0.1",
    )

    assert calls == {"claude": 1, "codex": 1}
    assert snap.source_bundle is not None
    assert snap.source_bundle.source_order == ("claude", "codex", "all")
    assert set(snap.source_bundle.sources) == {"claude", "codex", "all"}
    assert snap.source_bundle.sources["codex"].availability == "empty"


def test_source_bundle_reuses_unchanged_provider_objects_on_real_dispatch(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    monkeypatch.setitem(
        ns, "sync_cache", lambda _conn: SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns, "sync_codex_cache", lambda _conn: SimpleNamespace(lock_contended=False),
    )
    first = ns["_tui_build_snapshot"](
        now_utc=now, precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) "
            "VALUES ('session_entries_mutation_seq', '1')"
        )
        cache.commit()
        claude_changed = ns["_tui_build_snapshot"](
            now_utc=now, precompute_envelope=True, runtime_bind="127.0.0.1",
        )
        assert claude_changed.source_bundle.sources["claude"] is not first.source_bundle.sources["claude"]
        assert claude_changed.source_bundle.sources["codex"] is first.source_bundle.sources["codex"]
        assert claude_changed.source_bundle.sources["all"] is not first.source_bundle.sources["all"]

        cache.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) "
            "VALUES ('codex_physical_mutation_seq', '1')"
        )
        cache.commit()
        codex_changed = ns["_tui_build_snapshot"](
            now_utc=now, precompute_envelope=True, runtime_bind="127.0.0.1",
        )
        assert codex_changed.source_bundle.sources["claude"] is claude_changed.source_bundle.sources["claude"]
        assert codex_changed.source_bundle.sources["codex"] is not claude_changed.source_bundle.sources["codex"]
        assert codex_changed.source_bundle.sources["all"] is not claude_changed.source_bundle.sources["all"]
    finally:
        cache.close()


def test_dashboard_idle_dispatch_refreshes_quota_freshness_without_provider_aggregation(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_quota as quota_module
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    now = ns["dt"].datetime(2026, 7, 16, 12, tzinfo=ns["dt"].timezone.utc)
    captured_at = now - ns["dt"].timedelta(minutes=5)
    resets_at = now + ns["dt"].timedelta(hours=5)
    root_key = "root-idle-freshness"
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        cache.execute(
            "INSERT INTO codex_source_roots "
            "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
            "VALUES (?, ?, ?, ?)",
            (root_key, "/private/root", captured_at.isoformat(), captured_at.isoformat()),
        )
        cache.execute(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, captured_at_utc, "
            "observed_slot, logical_limit_key, limit_name, window_minutes, "
            "used_percent, resets_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("codex", root_key, "/private/root/rollout.jsonl", 1,
             captured_at.isoformat(), "primary", "limit-primary", "Primary", 300,
             25.0, resets_at.isoformat()),
        )
        cache.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) "
            "VALUES ('codex_physical_mutation_seq', '1')"
        )
        cache.commit()
        observations = quota_module.load_codex_quota_observations(
            source_root_keys=(root_key,), cache_conn=cache,
        )
        stats.execute(
            "INSERT INTO quota_projection_state "
            "(source_root_key, generation, physical_signature, completed_at_utc) "
            "VALUES (?, ?, ?, ?)",
            (root_key, "idle", quota_module._signature(observations, root_key), now.isoformat()),
        )
        stats.commit()
        quota_module._store_codex_quota_projection_certificate(
            sequence=1,
            signatures={root_key: quota_module._signature(observations, root_key)},
        )
    finally:
        cache.close()
        stats.close()

    monkeypatch.setitem(
        ns, "sync_cache", lambda _conn: SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns, "sync_codex_cache", lambda _conn: SimpleNamespace(lock_contended=False),
    )
    first = ns["_tui_build_snapshot"](
        now_utc=now, precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    first_codex = first.source_bundle.sources["codex"]
    assert first_codex.data["quota"]["summary"]["freshness"] == "fresh"

    monkeypatch.setattr(
        ns["_cctally_tui"], "build_codex_source_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("idle dispatch must not aggregate the Codex provider")
        ),
    )
    idle = ns["_tui_build_snapshot"](
        now_utc=now + ns["dt"].timedelta(hours=2),
        precompute_envelope=True,
        runtime_bind="127.0.0.1",
    )
    idle_codex = idle.source_bundle.sources["codex"]
    assert idle_codex.data_version == first_codex.data_version
    assert idle_codex.last_success_at == first_codex.last_success_at
    assert idle_codex.data["quota"]["summary"]["freshness"] == "stale"
    assert idle_codex.data["quota"]["histories"][0]["forecast"]["status"] == "stale"


def test_source_bundle_retains_the_prior_complete_generation_when_postvalidation_moves(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    tui_module = ns["_cctally_tui"]
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    try:
        prior = tui_module._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
        calls = 0

        def moving_digest(_conn):
            nonlocal calls
            calls += 1
            return "a" * 64 if calls == 1 else "b" * 64

        monkeypatch.setattr(tui_module, "codex_stats_digest", moving_digest)
        rebuilt = tui_module._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now + ns["dt"].timedelta(minutes=1),
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            prior_bundle=prior,
        )

        assert calls >= 2
        assert rebuilt is prior
    finally:
        stats.close()


def test_real_dispatch_keeps_the_unchanged_provider_object_across_owned_changes(
    tmp_path, monkeypatch,
):
    """Claude prune/config and Codex config changes reuse the other provider."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    config = {"alerts": {"notifier": "none"}}
    monkeypatch.setitem(ns, "load_config", lambda: config)
    orphan_dir = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-gone"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "orphan.jsonl").write_text(json.dumps({
        "type": "assistant", "uuid": "orphan-uuid", "parentUuid": None,
        "sessionId": "orphan-session", "requestId": "orphan-request",
        "timestamp": "2026-07-16T00:00:00Z", "cwd": "/Users/test/gone",
        "message": {
            "role": "assistant", "id": "orphan-message",
            "model": "claude-3-5-sonnet-20241022",
            "usage": {"input_tokens": 100, "output_tokens": 10,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n", encoding="utf-8")
    cache = ns["open_cache_db"]()
    try:
        ns["_cctally_cache"].sync_cache(cache)
    finally:
        cache.close()
    monkeypatch.setitem(
        ns, "sync_cache", lambda _conn: SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns, "sync_codex_cache", lambda _conn: SimpleNamespace(lock_contended=False),
    )
    try:
        first = ns["_tui_build_snapshot"](
            now_utc=now, precompute_envelope=True, runtime_bind="127.0.0.1",
        )
        assert not {
            "codex_budget_configured",
            "codex_budget_alerts_enabled",
            "codex_projected_enabled",
        } & set(first.source_bundle.sources["claude"].data["budget"]["settings"])
        # The real dashboard prune invalidates Claude through its generation
        # bump, while Codex physical cache state remains untouched.
        shutil.rmtree(orphan_dir)
        pruned = ns["_dashboard_self_heal_orphans"](skip_sync=False)
        assert pruned is not None and pruned.pruned_files == 1
        after_prune = ns["_tui_build_snapshot"](
            now_utc=now, precompute_envelope=True, runtime_bind="127.0.0.1",
        )
        assert after_prune.source_bundle.sources["claude"] is not first.source_bundle.sources["claude"]
        assert after_prune.source_bundle.sources["codex"] is first.source_bundle.sources["codex"]

        config = {"alerts": {"notifier": "osascript"}}
        after_claude_config = ns["_tui_build_snapshot"](
            now_utc=now, precompute_envelope=True, runtime_bind="127.0.0.1",
        )
        assert after_claude_config.source_bundle.sources["claude"] is not after_prune.source_bundle.sources["claude"]
        assert after_claude_config.source_bundle.sources["codex"] is after_prune.source_bundle.sources["codex"]

        config = {
            "alerts": {"notifier": "osascript"},
            "budget": {"codex": {
                "amount_usd": 10.0,
                "period": "calendar-month",
                "alert_thresholds": [80, 100],
            }},
        }
        after_codex_config = ns["_tui_build_snapshot"](
            now_utc=now, precompute_envelope=True, runtime_bind="127.0.0.1",
        )

        assert after_codex_config.source_bundle.sources["claude"] is after_claude_config.source_bundle.sources["claude"]
        assert after_codex_config.source_bundle.sources["codex"] is not after_claude_config.source_bundle.sources["codex"]
    finally:
        sc.reset_dispatch_state()


def test_source_bundle_threads_the_canonical_fast_tier_and_week_start(
    tmp_path, monkeypatch,
):
    ns, provider_root, _rollout, cache = _sync_setup(tmp_path, monkeypatch)
    stats = ns["open_db"]()
    tui_module = ns["_cctally_tui"]
    original_builder = tui_module.build_codex_source_state
    seen = []
    now = ns["dt"].datetime(2026, 7, 20, tzinfo=ns["dt"].timezone.utc)

    def capture(context, *, data_version):
        seen.append(context)
        return original_builder(context, data_version=data_version)

    try:
        (provider_root / "config.toml").write_text(
            'service_tier = "fast"\n', encoding="utf-8",
        )
        ns["sync_codex_cache"](cache)
        monkeypatch.setattr(tui_module, "build_codex_source_state", capture)

        bundle = tui_module._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            raw_config={"collector": {"week_start": "sunday"}},
        )

        assert bundle.sources["codex"].availability == "ok"
        assert len(seen) == 1
        assert seen[0].speed == "fast"
        assert seen[0].week_start_idx == 6
        entries = ns["iter_codex_entries"](
            cache,
            now - ns["dt"].timedelta(days=30),
            now,
        )
        expected = ns["build_codex_daily_view"](
            entries, now_utc=now, tz_name="UTC", speed="fast",
        )
        assert bundle.sources["codex"].data["hero"]["cost_usd"] == pytest.approx(
            expected.total_cost_usd,
        )
    finally:
        cache.close()
        stats.close()


def test_source_bundle_publishes_the_complete_legacy_derived_claude_projection(
    tmp_path, monkeypatch,
):
    """S4 must never collapse the default source to hero-only placeholders."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    legacy_envelope = {
        "header": {"used_pct": 42.0},
        "current_week": {
            "used_pct": 42.0,
            "five_hour_block": {"start_at": "2026-07-16T00:00:00Z"},
            "milestones": [{"percent": 42, "crossed_at_utc": "2026-07-16T01:00:00Z"}],
            "five_hour_milestones": [{"percent": 12, "crossed_at_utc": "2026-07-16T01:00:00Z"}],
        },
        "forecast": {"verdict": "ok"},
        "trend": {"weeks": []},
        "daily": {"rows": [{"date": "2026-07-16", "cost_usd": 1.25}], "total_cost_usd": 1.25, "total_tokens": 42},
        "monthly": {"rows": [{"label": "Jul 2026", "cost_usd": 1.25}], "total_cost_usd": 1.25, "total_tokens": 42},
        "weekly": {"rows": [{"label": "Jul 14", "cost_usd": 1.25}], "total_cost_usd": 1.25, "total_tokens": 42},
        "sessions": {
            "total": 1,
            "sort_key": "started_desc",
            "rows": [{"session_id": "native-session", "project_key": "legacy-project", "project": "cctally", "cost_usd": 1.25}],
        },
        "projects": {
            "current_week": {"rows": [{"key": "legacy-project", "bucket_path": "/private/cctally", "cost_usd": 1.25}]},
            "trend": {"projects": [{"key": "legacy-project", "bucket_path": "/private/cctally", "weekly_cost": [1.25]}]},
        },
        "alerts": [{"axis": "weekly", "threshold": 90}],
        "alerts_settings": {"enabled": True},
    }
    try:
        claude_data = ns["_cctally_tui"]._tui_project_claude_source_data(legacy_envelope)
        bundle = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=1.25,
            claude_total_tokens=42,
            claude_data=claude_data,
        )

        claude = bundle.sources["claude"]
        assert set(claude.data) >= {
            "hero", "periods", "sessions", "projects", "quota", "budget", "alerts",
        }
        assert list(claude.data["periods"]["daily"]["rows"]) == legacy_envelope["daily"]["rows"]
        assert list(claude.data["periods"]["monthly"]["rows"]) == legacy_envelope["monthly"]["rows"]
        assert claude.data["periods"]["monthly"]["total_cost_usd"] == legacy_envelope["monthly"]["total_cost_usd"]
        assert list(claude.data["periods"]["weekly"]["rows"]) == legacy_envelope["weekly"]["rows"]
        assert claude.data["periods"]["weekly"]["total_cost_usd"] == legacy_envelope["weekly"]["total_cost_usd"]
        assert dict(claude.data["hero"]["header"]) == legacy_envelope["header"]
        assert dict(claude.data["budget"]["forecast"]) == legacy_envelope["forecast"]
        assert claude.data["alerts"]["rows"][0]["source"] == "claude"

        session = claude.data["sessions"]["rows"][0]
        project = claude.data["projects"]["rows"][0]
        assert session["source"] == project["source"] == "claude"
        assert session["key"].startswith("session:")
        assert project["key"].startswith("project:")
        assert "native-session" not in repr(claude.data)
        assert "legacy-project" not in repr(claude.data)
        assert "/private/cctally" not in repr(claude.data)
        expected_domains = {
            "hero", "daily", "monthly", "weekly", "sessions", "forensics",
            "quota", "budget", "projects", "alerts",
        }
        assert expected_domains <= set(claude.capabilities)
        assert expected_domains <= set(bundle.sources["codex"].capabilities)
        assert claude.capabilities["daily"].semantics == "calendar-day"
        assert claude.capabilities["forensics"].semantics == "legacy-projection"
        assert bundle.sources["codex"].capabilities["alerts"].semantics == "provider-native"
    finally:
        stats.close()


def test_claude_projection_filters_mixed_legacy_alert_ownership_without_duplicates():
    ns = load_script()
    legacy_alerts = [
        {"axis": "weekly", "threshold": 90, "alerted_at": "2026-07-16T01:00:00Z"},
        {"axis": "budget", "threshold": 75, "alerted_at": "2026-07-16T01:30:00Z"},
        {"axis": "budget", "vendor": "claude", "threshold": 80, "alerted_at": "2026-07-16T02:00:00Z"},
        {"axis": "budget", "vendor": "codex", "threshold": 80, "alerted_at": "2026-07-16T03:00:00Z"},
        {"axis": "codex_budget", "threshold": 90, "alerted_at": "2026-07-16T04:00:00Z"},
        {"axis": "projected", "metric": "budget_usd", "threshold": 90, "alerted_at": "2026-07-16T05:00:00Z"},
        {"axis": "projected", "metric": "codex_budget_usd", "threshold": 90, "alerted_at": "2026-07-16T06:00:00Z"},
        {"axis": "projected", "metric": "five_hour_pct", "threshold": 90, "alerted_at": "2026-07-16T07:00:00Z"},
    ]
    legacy = {"alerts": legacy_alerts}

    projected = ns["_cctally_tui"]._tui_project_claude_source_data(legacy)

    rows = projected["alerts"]["rows"]
    assert [(row["axis"], row.get("metric"), row.get("vendor")) for row in rows] == [
        ("weekly", None, None),
        ("budget", None, None),
        ("budget", None, "claude"),
        ("projected", "budget_usd", None),
    ]
    assert legacy["alerts"] == legacy_alerts


def test_source_bundle_retains_prior_whole_codex_state_on_ingest_contention(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    try:
        prior = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
        current = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=True,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            prior_bundle=prior,
        )

        codex = current.sources["codex"]
        assert codex.availability == "partial"
        assert codex.freshness == "stale"
        assert codex.data is prior.sources["codex"].data
        assert codex.data_version == prior.sources["codex"].data_version
        assert codex.warnings[0].code == "source_ingest_contended"
        assert current.sources["all"].data["combined"] is None
    finally:
        stats.close()


def test_source_bundle_reports_unavailable_codex_when_contention_has_no_prior(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    try:
        bundle = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc),
            display_tz_name="UTC",
            codex_ingest_contended=True,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            prior_bundle=None,
        )

        codex = bundle.sources["codex"]
        assert codex.availability == "unavailable"
        assert codex.data is None
        assert codex.warnings[0].code == "source_ingest_contended"
    finally:
        stats.close()


def test_source_bundle_retains_prior_whole_codex_state_on_ingest_failure(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    try:
        prior = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
        current = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            codex_ingest_failed=True,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            prior_bundle=prior,
        )

        codex = current.sources["codex"]
        assert codex.availability == "partial"
        assert codex.data is prior.sources["codex"].data
        assert codex.warnings[0].code == "source_ingest_failed"
    finally:
        stats.close()


@pytest.mark.parametrize(
    ("flag", "warning_code"),
    (
        ("claude_ingest_contended", "source_ingest_contended"),
        ("claude_ingest_failed", "source_ingest_failed"),
    ),
)
def test_source_bundle_retains_prior_whole_claude_state_on_ingest_degradation(
    tmp_path, monkeypatch, flag, warning_code,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    try:
        prior = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=12.5,
            claude_total_tokens=120,
        )
        current = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            prior_bundle=prior,
            **{flag: True},
        )

        claude = current.sources["claude"]
        assert claude.availability == "partial"
        assert claude.freshness == "stale"
        assert claude.data is prior.sources["claude"].data
        assert claude.data_version == prior.sources["claude"].data_version
        assert claude.last_success_at == prior.sources["claude"].last_success_at
        assert claude.warnings[0].code == warning_code
        assert current.sources["all"].data["combined"] is None
    finally:
        stats.close()


@pytest.mark.parametrize(
    ("flag", "warning_code"),
    (
        ("claude_ingest_contended", "source_ingest_contended"),
        ("claude_ingest_failed", "source_ingest_failed"),
    ),
)
def test_source_bundle_reports_unavailable_claude_without_prior_on_ingest_degradation(
    tmp_path, monkeypatch, flag, warning_code,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    try:
        bundle = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc),
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            prior_bundle=None,
            **{flag: True},
        )

        claude = bundle.sources["claude"]
        assert claude.availability == "unavailable"
        assert claude.freshness == "stale"
        assert claude.data is None
        assert claude.warnings[0].code == warning_code
        assert bundle.sources["all"].data["combined"] is None
    finally:
        stats.close()


def test_snapshot_keeps_prior_complete_source_bundle_when_signature_and_builder_fail(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    stats = ns["open_db"]()
    try:
        prior_bundle = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=12.5,
            claude_total_tokens=120,
        )
    finally:
        stats.close()
    prior_snap = ns["_tui_empty_snapshot"](now)
    prior_snap = dataclasses.replace(prior_snap, source_bundle=prior_bundle)
    sc.store_dispatch_state(("prior",), prior_snap)

    def signature_failure(_stats_conn):
        raise RuntimeError("private digest /canary/root")

    def bundle_failure(**_kwargs):
        raise RuntimeError("private source build /canary/root")

    monkeypatch.setattr(ns["_cctally_tui"], "_tui_compute_dispatch_signature", signature_failure)
    monkeypatch.setattr(ns["_cctally_tui"], "_tui_build_source_bundle", bundle_failure)
    snap = ns["_tui_build_snapshot"](
        now_utc=now,
        skip_sync=True,
        precompute_envelope=True,
    )

    assert snap.source_bundle is prior_bundle
    assert "canary/root" not in repr(snap.source_bundle)


def test_source_bundle_combined_hero_uses_the_same_visible_interval_for_both_providers(
    tmp_path, monkeypatch,
):
    ns, _root, _rollout, cache = _sync_setup(tmp_path, monkeypatch)
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 20, tzinfo=ns["dt"].timezone.utc)
    visible_start = ns["dt"].datetime(2026, 7, 17, tzinfo=ns["dt"].timezone.utc)
    try:
        ns["sync_codex_cache"](cache)
        historical = build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=ns["dt"].datetime(2026, 7, 1, tzinfo=ns["dt"].timezone.utc),
                now_utc=now,
                display_tz_name="UTC",
            ),
            data_version="historical",
        )
        assert historical.data["hero"]["cost_usd"] > 0
        assert historical.data["periods"]["daily"]["rows"]

        bundle = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=1.25,
            claude_total_tokens=25,
            common_range_start=visible_start,
        )

        assert bundle.sources["codex"].data["hero"]["cost_usd"] == 0.0
        assert bundle.sources["codex"].data["periods"]["daily"]["rows"] == ()
        assert bundle.sources["all"].data["combined"] == {
            "cost_usd": 1.25,
            "total_tokens": 25,
        }
    finally:
        cache.close()
        stats.close()


def test_dashboard_no_sync_skips_both_ingests_and_reads_cached_codex_source(
    tmp_path, monkeypatch,
):
    ns, _root, _rollout, cache = _sync_setup(tmp_path, monkeypatch)
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    calls = {"claude": 0, "codex": 0}

    def unexpected_claude(*_args, **_kwargs):
        calls["claude"] += 1
        raise AssertionError("--no-sync must not ingest Claude")

    def unexpected_codex(*_args, **_kwargs):
        calls["codex"] += 1
        raise AssertionError("--no-sync must not ingest Codex")

    monkeypatch.setitem(ns, "sync_cache", unexpected_claude)
    monkeypatch.setitem(ns, "sync_codex_cache", unexpected_codex)
    snap = ns["_tui_build_snapshot"](
        now_utc=ns["dt"].datetime(2026, 7, 20, tzinfo=ns["dt"].timezone.utc),
        skip_sync=True,
        precompute_envelope=True,
    )

    assert calls == {"claude": 0, "codex": 0}
    assert snap.source_bundle is not None
    assert snap.source_bundle.sources["codex"].availability == "ok"
    assert snap.source_bundle.sources["codex"].data["sessions"]["rows"]


def test_dashboard_snapshot_retains_prior_claude_on_real_ingest_contention(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    stats = ns["open_db"]()
    try:
        prior_bundle = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=12.5,
            claude_total_tokens=120,
        )
    finally:
        stats.close()
    sc.store_dispatch_state(
        ("prior",),
        dataclasses.replace(ns["_tui_empty_snapshot"](now), source_bundle=prior_bundle),
    )

    monkeypatch.setitem(ns, "sync_cache", lambda _conn: SimpleNamespace(lock_contended=True))
    monkeypatch.setitem(ns, "sync_codex_cache", lambda _conn: SimpleNamespace(lock_contended=False))
    snap = ns["_tui_build_snapshot"](
        now_utc=now,
        skip_sync=False,
        precompute_envelope=True,
    )

    claude = snap.source_bundle.sources["claude"]
    assert claude.availability == "partial"
    assert claude.data is prior_bundle.sources["claude"].data
    assert claude.warnings[0].code == "source_ingest_contended"
    assert snap.source_bundle.sources["all"].data["combined"] is None


def test_source_bundle_reuses_exact_unchanged_provider_objects(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    try:
        first = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=12.5,
            claude_total_tokens=120,
        )
        cache.execute(
            "INSERT INTO cache_meta(key, value) VALUES ('session_entries_mutation_seq', '1')"
        )
        cache.commit()
        claude_changed = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=13.5,
            claude_total_tokens=130,
            prior_bundle=first,
        )
        assert claude_changed.sources["claude"] is not first.sources["claude"]
        assert claude_changed.sources["codex"] is first.sources["codex"]
        assert claude_changed.sources["all"] is not first.sources["all"]

        cache.execute(
            "INSERT INTO cache_meta(key, value) VALUES ('codex_physical_mutation_seq', '1')"
        )
        cache.commit()
        codex_changed = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=13.5,
            claude_total_tokens=130,
            prior_bundle=claude_changed,
        )
        assert codex_changed.sources["claude"] is claude_changed.sources["claude"]
        assert codex_changed.sources["codex"] is not claude_changed.sources["codex"]
        assert codex_changed.sources["all"] is not claude_changed.sources["all"]
    finally:
        cache.close()
        stats.close()


def test_dashboard_held_codex_flock_publishes_unavailable_source_without_prior(
    tmp_path, monkeypatch,
):
    ns, _root, _rollout, cache = _sync_setup(tmp_path, monkeypatch)
    cache.close()
    lock_path = ns["CACHE_LOCK_CODEX_PATH"]
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            snap = ns["_tui_build_snapshot"](
                now_utc=ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc),
                skip_sync=False,
                precompute_envelope=True,
            )
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    codex = snap.source_bundle.sources["codex"]
    assert codex.availability == "unavailable"
    assert codex.data is None
    assert codex.warnings[0].code == "source_ingest_contended"


def test_dashboard_dispatch_retries_an_unavailable_codex_projection_after_certificate_recovery(
    tmp_path, monkeypatch,
):
    """Certificate-only recovery must bypass idle and retry only Codex."""
    ns, _root, _rollout, cache = _sync_setup(tmp_path, monkeypatch)
    import _cctally_quota as quota_module
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    stats = ns["open_db"]()
    try:
        ns["sync_codex_cache"](cache)
        cache.execute(
            "DELETE FROM cache_meta WHERE key='codex_quota_projection_certificate'"
        )
        cache.commit()
        physical_seq = _physical_seq(cache)
        before_retry_signature = ns["_cctally_tui"]._tui_compute_dispatch_signature(stats)

        unavailable = ns["_tui_build_snapshot"](
            now_utc=now,
            skip_sync=True,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )
        unavailable_codex = unavailable.source_bundle.sources["codex"]
        assert unavailable_codex.availability == "unavailable"
        assert unavailable_codex.warnings[0].code == "codex_projection_incoherent"

        # A normal unchanged-file ingest re-runs the durable reconciler and
        # stamps the certificate.  Neither physical accounting rows nor the
        # dispatch signature changes, which is precisely the prior idle trap.
        retry = ns["sync_codex_cache"](cache)
        certificate = quota_module.load_codex_quota_projection_certificate(cache)
        assert retry.rows_changed == 0
        assert _physical_seq(cache) == physical_seq
        assert certificate is not None
        assert certificate[0] == physical_seq
        assert ns["_cctally_tui"]._tui_compute_dispatch_signature(stats) == before_retry_signature

        recovered = ns["_tui_build_snapshot"](
            now_utc=now,
            skip_sync=True,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )
        assert recovered.source_bundle.sources["codex"].availability == "ok"
        assert recovered.source_bundle.sources["codex"] is not unavailable_codex
        assert recovered.source_bundle.sources["claude"] is unavailable.source_bundle.sources["claude"]
    finally:
        sc.reset_dispatch_state()
        cache.close()
        stats.close()


def test_dashboard_idle_retries_persistently_unavailable_codex_without_rebuilding_legacy_rows(
    tmp_path, monkeypatch,
):
    """A missing projection certificate retries Codex without waking legacy builders."""
    ns, _root, _rollout, cache = _sync_setup(tmp_path, monkeypatch)
    import _cctally_quota as quota_module
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    tui = ns["_cctally_tui"]
    original_source_bundle = tui._tui_build_source_bundle
    original_forecast = tui._tui_build_forecast_view
    stats = ns["open_db"]()
    try:
        # Simulate a persistent post-projection certificate write failure. The
        # physical cache and durable stats projection are still complete, but
        # the source must fail closed because their coherence cannot be proved.
        monkeypatch.setattr(
            quota_module, "_store_codex_quota_projection_certificate",
            lambda **_kwargs: None,
        )
        first = ns["_tui_build_snapshot"](
            now_utc=now,
            skip_sync=False,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )
        first_codex = first.source_bundle.sources["codex"]
        assert first_codex.availability == "unavailable"
        assert first_codex.data is None
        assert first_codex.warnings[0].code == "codex_projection_incoherent"
        physical_seq = _physical_seq(cache)
        dispatch_signature = tui._tui_compute_dispatch_signature(stats)

        calls = {"forecast": 0, "source_bundle": 0}

        def counted_forecast(*args, **kwargs):
            calls["forecast"] += 1
            return original_forecast(*args, **kwargs)

        def counted_source_bundle(*args, **kwargs):
            calls["source_bundle"] += 1
            return original_source_bundle(*args, **kwargs)

        monkeypatch.setattr(tui, "_tui_build_forecast_view", counted_forecast)
        monkeypatch.setattr(tui, "_tui_build_source_bundle", counted_source_bundle)
        second = ns["_tui_build_snapshot"](
            now_utc=now,
            skip_sync=False,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )
        third = ns["_tui_build_snapshot"](
            now_utc=now,
            skip_sync=False,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )

        # Two unchanged-signature ticks retry the bounded provider path only;
        # the representative legacy aggregate and its heavy rows stay idle.
        assert _physical_seq(cache) == physical_seq
        assert tui._tui_compute_dispatch_signature(stats) == dispatch_signature
        assert calls == {"forecast": 0, "source_bundle": 2}
        assert second.forecast is first.forecast
        assert third.forecast is first.forecast
        assert second.trend is first.trend
        assert third.sessions is first.sessions
        for snapshot in (second, third):
            codex = snapshot.source_bundle.sources["codex"]
            assert codex.availability in ("unavailable", "partial")
            assert codex.data is None
            assert codex.warnings[0].code == "codex_projection_incoherent"
            assert snapshot.source_bundle.sources["claude"] is first.source_bundle.sources["claude"]
            wire = sys.modules["_cctally_dashboard_envelope"]._source_state_to_wire(codex)
            assert "clock_data" not in wire
            assert wire["data"] is None
    finally:
        sc.reset_dispatch_state()
        cache.close()
        stats.close()


@pytest.mark.parametrize("display_tz", ("utc", "local", "America/Los_Angeles"))
def test_dashboard_idle_source_retry_keeps_the_full_build_display_timezone_range(
    tmp_path, monkeypatch, display_tz,
):
    """The source-only retry uses the full build's resolved calendar zone."""
    ns, _root, _rollout, cache = _sync_setup(tmp_path, monkeypatch)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    now = ns["dt"].datetime(2026, 7, 16, 1, tzinfo=ns["dt"].timezone.utc)
    config = {"display": {"tz": display_tz}}
    monkeypatch.setitem(ns, "load_config", lambda: config)
    project_dir = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-tz-range"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "range.jsonl").write_text(json.dumps({
        "type": "assistant", "uuid": "tz-range-uuid", "parentUuid": None,
        "sessionId": "tz-range-session", "requestId": "tz-range-request",
        "timestamp": "2026-07-16T00:30:00Z", "cwd": "/Users/test/tz-range",
        "message": {
            "role": "assistant", "id": "tz-range-message",
            "model": "claude-3-5-sonnet-20241022",
            "usage": {"input_tokens": 100, "output_tokens": 10,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n", encoding="utf-8")
    tui = ns["_cctally_tui"]
    original_source_bundle = tui._tui_build_source_bundle
    observed_ranges = []

    def capture_source_range(*args, **kwargs):
        observed_ranges.append(kwargs["common_range_start"])
        return original_source_bundle(*args, **kwargs)

    monkeypatch.setattr(tui, "_tui_build_source_bundle", capture_source_range)
    try:
        # First normal build establishes the production calendar interval.
        first = ns["_tui_build_snapshot"](
            now_utc=now,
            skip_sync=False,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )
        assert first.daily_panel
        normal_range = observed_ranges[-1]

        # A certificate-only failure has no global signature leg. Resetting the
        # dispatcher seeds one explicit degraded generation, then the next
        # unchanged-signature tick exercises the source-only idle retry.
        cache.execute(
            "DELETE FROM cache_meta WHERE key='codex_quota_projection_certificate'"
        )
        cache.commit()
        sc.reset_dispatch_state()
        degraded = ns["_tui_build_snapshot"](
            now_utc=now,
            skip_sync=True,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )
        assert degraded.source_bundle.sources["codex"].availability == "unavailable"
        full_degraded_range = observed_ranges[-1]

        retried = ns["_tui_build_snapshot"](
            now_utc=now,
            skip_sync=True,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )
        idle_retry_range = observed_ranges[-1]
        assert retried.source_bundle.sources["codex"].availability in ("unavailable", "partial")
        assert len(observed_ranges) == 3
        assert normal_range == full_degraded_range == idle_retry_range

        if display_tz == "America/Los_Angeles":
            # The oldest visible LA day begins at 07:00Z. Host-local/UTC would
            # instead start the same calendar key at midnight Z.
            assert normal_range == ns["dt"].datetime(
                2026, 6, 16, 7, tzinfo=ns["dt"].timezone.utc,
            )
            assert normal_range != ns["dt"].datetime(
                2026, 6, 16, tzinfo=ns["dt"].timezone.utc,
            )
    finally:
        sc.reset_dispatch_state()
        cache.close()


def test_dashboard_held_codex_flock_retains_the_prior_source_state(
    tmp_path, monkeypatch,
):
    ns, _root, _rollout, cache = _sync_setup(tmp_path, monkeypatch)
    ns["sync_codex_cache"](cache)
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    prior = ns["_tui_build_snapshot"](
        now_utc=now,
        skip_sync=True,
        precompute_envelope=True,
    )
    assert prior.source_bundle.sources["codex"].data is not None
    cache.execute(
        "UPDATE cache_meta "
        "SET value=CAST(value AS INTEGER) + 1 "
        "WHERE key='codex_physical_mutation_seq'"
    )
    cache.commit()
    cache.close()

    lock_path = ns["CACHE_LOCK_CODEX_PATH"]
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            degraded = ns["_tui_build_snapshot"](
                now_utc=now,
                skip_sync=False,
                precompute_envelope=True,
            )
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    codex = degraded.source_bundle.sources["codex"]
    assert codex.availability == "partial"
    assert codex.data is prior.source_bundle.sources["codex"].data
    assert codex.warnings[0].code == "source_ingest_contended"


def test_codex_projection_and_domain_failure_retain_prior_then_recover(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    try:
        prior = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=1.0,
            claude_total_tokens=10,
        )
        cache.execute(
            "INSERT INTO cache_meta(key, value) VALUES ('codex_physical_mutation_seq', '1')"
        )
        cache.commit()

        with monkeypatch.context() as m:
            def projection_failure(*_args, **_kwargs):
                raise ns["_cctally_tui"].CodexProjectionIncoherent("mismatch")

            m.setattr(ns["_cctally_tui"], "build_codex_source_state", projection_failure)
            incoherent = ns["_cctally_tui"]._tui_build_source_bundle(
                stats_conn=stats,
                now_utc=now,
                display_tz_name="UTC",
                codex_ingest_contended=False,
                claude_cost_usd=1.0,
                claude_total_tokens=10,
                prior_bundle=prior,
            )
        assert incoherent.sources["codex"].data is prior.sources["codex"].data
        assert incoherent.sources["codex"].warnings[0].code == "codex_projection_incoherent"

        recovered = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=1.0,
            claude_total_tokens=10,
            prior_bundle=incoherent,
        )
        assert recovered.sources["codex"].availability == "empty"
        assert recovered.sources["codex"].freshness == "fresh"
        assert recovered.sources["codex"].warnings == ()

        cache.execute(
            "UPDATE cache_meta SET value='2' WHERE key='codex_physical_mutation_seq'"
        )
        cache.commit()
        with monkeypatch.context() as m:
            m.setattr(
                ns["_cctally_tui"], "build_codex_source_state",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private /canary/root")),
            )
            failed_domain = ns["_cctally_tui"]._tui_build_source_bundle(
                stats_conn=stats,
                now_utc=now,
                display_tz_name="UTC",
                codex_ingest_contended=False,
                claude_cost_usd=1.0,
                claude_total_tokens=10,
                prior_bundle=recovered,
            )
        assert failed_domain.sources["codex"].data is recovered.sources["codex"].data
        assert failed_domain.sources["codex"].warnings[0].code == "source_build_failed"
        assert failed_domain.sources["codex"].warnings[0].domain == "read_model"

        recovered_domain = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=1.0,
            claude_total_tokens=10,
            prior_bundle=failed_domain,
        )
        assert recovered_domain.sources["codex"].availability == "empty"
        assert recovered_domain.sources["codex"].freshness == "fresh"
        assert recovered_domain.sources["codex"].warnings == ()
    finally:
        cache.close()
        stats.close()


def test_dashboard_source_scale_gate_reuses_idle_provider_state_without_rollout_scan(
    tmp_path, monkeypatch,
):
    """Remote production-shape gate: relational reads stay bounded at scale."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    now = ns["dt"].datetime(2026, 7, 16, 12, tzinfo=ns["dt"].timezone.utc)
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        root_key = "root-scale"
        timestamp = (now - ns["dt"].timedelta(minutes=1)).isoformat()
        cache.executemany(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ((f"/fixture/claude/{index % 1000}.jsonl", index, timestamp,
              "claude-3-5-sonnet-20241022", 100, 10)
             for index in range(50_000)),
        )
        cache.executemany(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ((f"/fixture/claude/{index}.jsonl", 1, index, 1, timestamp)
             for index in range(1_000)),
        )
        cache.executemany(
            "INSERT INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ((f"/fixture/codex/{index}.jsonl", 1, index, 1, timestamp)
             for index in range(2_000)),
        )
        cache.execute(
            "INSERT INTO codex_source_roots "
            "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
            "VALUES (?, ?, ?, ?)",
            (root_key, "/fixture/codex-root", timestamp, timestamp),
        )
        cache.executemany(
            "INSERT INTO codex_conversation_threads "
            "(conversation_key, source_root_key, native_thread_id, root_thread_id, "
            "source_path, git_json, first_seen_utc, last_seen_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ((f"conversation-{index}", root_key, f"native-{index}", f"root-{index}",
              f"/fixture/codex/{index}.jsonl", f'{{"project": {index}}}', timestamp, timestamp)
             for index in range(200)),
        )
        cache.executemany(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            "input_tokens, output_tokens, total_tokens, source_root_key, conversation_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ((f"/fixture/codex/{index % 2000}.jsonl", index, timestamp,
              f"session-{index % 200}", "gpt-5", 100, 10, 110,
              root_key, f"conversation-{index % 200}")
             for index in range(100_000)),
        )
        quota_rows = []
        for index in range(24):
            logical_limit = f"limit-{index}"
            slot = f"slot-{index}"
            resets_at = (now + ns["dt"].timedelta(hours=5 + index)).isoformat()
            quota_rows.append((
                "codex", root_key, f"/fixture/quota/{index}.jsonl", index,
                timestamp, slot, logical_limit, f"limit-id-{index}", "Scale quota",
                300, 25.0, resets_at,
            ))
        cache.executemany(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, captured_at_utc, "
            "observed_slot, logical_limit_key, limit_id, limit_name, window_minutes, "
            "used_percent, resets_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            quota_rows,
        )
        cache.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES "
            "('codex_physical_mutation_seq', '1')"
        )
        cache.commit()

        import _cctally_quota as quota
        observations = quota.load_codex_quota_observations(source_root_keys=(root_key,))
        physical_signature = quota._signature(observations, root_key)
        stats.executemany(
            "INSERT INTO quota_window_blocks "
            "(source, source_root_key, logical_limit_key, observed_slot, window_minutes, "
            "limit_name, resets_at_utc, nominal_start_at_utc, first_observed_at_utc, "
            "last_observed_at_utc, first_percent, current_percent, last_source_path, "
            "last_line_offset, generation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (("codex", root_key, f"limit-{index}", f"slot-{index}", 300,
              "Scale quota", (now + ns["dt"].timedelta(hours=5 + index)).isoformat(),
              timestamp, timestamp, timestamp, 25.0, 25.0,
              f"/fixture/quota/{index}.jsonl", index, "scale")
             for index in range(24)),
        )
        stats.execute(
            "INSERT INTO quota_projection_state "
            "(source_root_key, generation, physical_signature, completed_at_utc) "
            "VALUES (?, ?, ?, ?)",
            (root_key, "scale", physical_signature, timestamp),
        )
        stats.commit()
        quota._store_codex_quota_projection_certificate(
            sequence=1,
            signatures={root_key: physical_signature},
        )

        tui = ns["_cctally_tui"]
        perf = sys.modules["_lib_perf"]
        calls = {"claude": 0, "codex": 0}

        def claude_ingest(_conn):
            calls["claude"] += 1
            return SimpleNamespace(lock_contended=False)

        def codex_ingest(_conn):
            calls["codex"] += 1
            return SimpleNamespace(lock_contended=False)

        def forbidden_rollout_scan(*_args, **_kwargs):
            raise AssertionError("dashboard read-model must not scan rollout JSONL")

        monkeypatch.setattr(tui, "sync_cache", claude_ingest)
        monkeypatch.setattr(tui, "sync_codex_cache", codex_ingest)
        monkeypatch.setattr(pathlib.Path, "rglob", forbidden_rollout_scan)
        monkeypatch.setattr(tui, "_tui_precompute_doctor_payload", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(tui, "_tui_build_sessions", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(
            sys.modules["_cctally_dashboard"], "build_cache_report_snapshot",
            lambda **_kwargs: None,
        )
        perf.set_enabled(True)
        try:
            started = time.perf_counter()
            first = tui._tui_build_snapshot(
                now_utc=now, precompute_envelope=True, runtime_bind="127.0.0.1",
            )
            changed_elapsed = time.perf_counter() - started
            digest_started = time.perf_counter()
            tui.codex_stats_digest(stats)
            digest_elapsed = time.perf_counter() - digest_started
            idle_started = time.perf_counter()
            idles = [
                tui._tui_build_snapshot(
                    now_utc=now, precompute_envelope=True, runtime_bind="127.0.0.1",
                )
                for _ in range(3)
            ]
            idle_elapsed = time.perf_counter() - idle_started
        finally:
            perf.set_enabled(False)

        codex = first.source_bundle.sources["codex"]
        combined = first.source_bundle.sources["all"].data["combined"]
        assert first.last_sync_error is None
        assert codex.availability == "ok"
        assert len(codex.data["projects"]["rows"]) == 200
        assert len(codex.data["quota"]["blocks"]) == 24
        assert calls == {"claude": 4, "codex": 4}
        assert all(idle.source_bundle.sources["claude"] is first.source_bundle.sources["claude"]
                   for idle in idles)
        assert all(idle.source_bundle.sources["codex"] is codex for idle in idles)
        assert math.isclose(
            combined["cost_usd"],
            first.source_bundle.sources["claude"].data["hero"]["cost_usd"]
            + codex.data["hero"]["cost_usd"],
            rel_tol=0.0, abs_tol=1e-9,
        )
        assert combined["total_tokens"] == (
            first.source_bundle.sources["claude"].data["hero"]["total_tokens"]
            + codex.data["hero"]["total_tokens"]
        )
        source_detail_lookup(
            first.source_bundle, "codex", "project", codex.data["projects"]["rows"][0]["key"],
        )
        source_detail_lookup(
            first.source_bundle, "codex", "block", codex.data["quota"]["blocks"][0]["key"],
        )
        share = ns["_load_sibling"]("_cctally_dashboard_share")
        native_share = share._build_codex_source_share_snapshot(
            ns["_share_load_lib"](), state=codex, panel="projects",
            template_id="projects-recap", options={},
        )
        assert native_share.rows and native_share.rows[0].cells["project"].label

        # Explicit remote-fixture budgets: query-plan evidence is primary;
        # these bounds merely catch an accidental full scan/regression.
        assert digest_elapsed < 2.0
        assert idle_elapsed < 12.0
        assert changed_elapsed < 45.0
        print(
            "source-scale "
            f"changed={changed_elapsed:.3f}s digest={digest_elapsed:.3f}s "
            f"idle3={idle_elapsed:.3f}s rows=100000/50000 files=2000 quota=24 projects=200"
        )
    finally:
        cache.close()
        stats.close()
