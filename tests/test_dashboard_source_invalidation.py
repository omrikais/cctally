"""Codex source invalidation contracts for #294 S4 Stage 1."""
from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import json
import math
import os
import pathlib
import re
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


def _seed_active_codex_weekly_cycle(ns, cache: sqlite3.Connection, *, now):
    """Add coherent root-qualified native 7-day evidence to a synced fixture."""
    import _cctally_quota as quota_module

    row = cache.execute(
        "SELECT source_root_key, canonical_root_path FROM codex_source_roots"
    ).fetchone()
    assert row is not None
    root_key, root_path = str(row[0]), str(row[1])
    resets_at = now + ns["dt"].timedelta(days=1)
    captured_at = now - ns["dt"].timedelta(minutes=1)
    cache.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        "observed_slot, logical_limit_key, limit_id, limit_name, window_minutes, "
        "used_percent, resets_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "codex", root_key, f"{root_path}/weekly-quota.jsonl", 10_080,
            captured_at.isoformat(), "fixture-weekly", "fixture-weekly-limit",
            "fixture-weekly-limit", "Fixture weekly quota", 10_080, 25.0,
            resets_at.isoformat(),
        ),
    )
    ns["_cctally_cache"]._bump_codex_physical_mutation_seq(cache)
    cache.commit()
    quota_module.reconcile_codex_quota_projection(
        source_root_keys=(root_key,), now=now,
    )
    return resets_at - ns["dt"].timedelta(minutes=10_080), resets_at


def _append_active_codex_weekly_snapshot(ns, rollout: pathlib.Path, *, now) -> None:
    """Persist a native 7-day observation for an ingest/recovery fixture."""
    resets_at = now + ns["dt"].timedelta(days=1)
    payload = {
        "type": "event_msg",
        "timestamp": (now - ns["dt"].timedelta(minutes=1)).isoformat(),
        "payload": {
            "type": "token_count",
            "info": {
                "rate_limits": {
                    "limit_id": "fixture-weekly-limit",
                    "limit_name": "Fixture weekly quota",
                    "primary": {
                        "resets_at": int(resets_at.timestamp()),
                        "used_percent": 25.0,
                        "window_minutes": 10_080,
                    },
                },
            },
        },
    }
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def test_codex_physical_mutation_sequence_tracks_metadata_and_refuses_unknown_reset(
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
        refused = ns["sync_codex_cache"](conn)
        assert refused.prune_refused is True
        assert _physical_seq(conn) == 2
    finally:
        conn.close()


def test_codex_physical_mutation_sequence_tracks_root_prune_and_rebuild(
    tmp_path, monkeypatch,
):
    ns, provider_root, _rollout, conn = _sync_setup(tmp_path, monkeypatch)
    try:
        ns["sync_codex_cache"](conn)
        assert _physical_seq(conn) == 1

        provider_b = tmp_path / "provider-b"
        rollout_b = (
            provider_b / "sessions" / "2026" / "07" / "16" / "rollout.jsonl"
        )
        rollout_b.parent.mkdir(parents=True)
        shutil.copyfile(CORPUS / "modern-full.jsonl", rollout_b)
        monkeypatch.setenv("CODEX_HOME", str(provider_b))
        ns["sync_codex_cache"](conn)
        # One mutation for pruning A and one for ingesting B.
        assert _physical_seq(conn) == 3

        monkeypatch.setenv("CODEX_HOME", str(provider_root))
        ns["sync_codex_cache"](conn)
        assert _physical_seq(conn) == 5

        ns["sync_codex_cache"](conn, rebuild=True)
        # Rebuild clears the old physical families and then commits the
        # reingested file batch in its own transaction.
        assert _physical_seq(conn) == 7
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


def test_dashboard_dispatch_signature_moves_on_a_real_armed_claude_alert(
    tmp_path, monkeypatch,
):
    """#556 S3 §2.9: pin the wiring, not only the helper's sensitivity.

    The leg exists to leave the idle short-circuit when a Claude alert fires.
    Passing a digest in by hand proves the signature reacts to one; only a real
    INSERT proves `_tui_compute_dispatch_signature` and `_snapshot_data_version`
    call it at all. Deleting the production argument leaves both digests empty
    and equal, which is what the first assertion catches.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    try:
        before = ns["_cctally_tui"]._tui_compute_dispatch_signature(stats)
        stats.execute(
            "INSERT INTO budget_milestones ("
            "vendor, period_start_at, period, threshold, budget_usd, spent_usd, "
            "consumption_pct, crossed_at_utc, alerted_at) VALUES ("
            "'claude', '2026-04-13T00:00:00Z', 'subscription-week', 50, "
            "100.0, 50.0, 50.0, '2026-04-16T13:40:00Z', '2026-04-16T13:59:00Z')"
        )
        stats.commit()
        after = ns["_cctally_tui"]._tui_compute_dispatch_signature(stats)

        assert before.claude_stats_digest != after.claude_stats_digest
        assert before != after
        assert ns["_snapshot_data_version"](before) != ns["_snapshot_data_version"](after)
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
    assert dict(first_codex.domain_freshness) == {
        "hero": "fresh",
        "quota": "fresh",
        "sessions": "fresh",
    }

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
    assert idle_codex.freshness == "fresh"
    assert dict(idle_codex.domain_freshness) == {
        "hero": "fresh",
        "quota": "stale",
        "sessions": "fresh",
    }


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
            projects_envelope={},
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
            projects_envelope={},
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


def test_source_bundle_publishes_pinned_snapshot_when_only_cache_moves(
    tmp_path, monkeypatch,
):
    """A busy cache may advance after the builder pins its coherent snapshot.

    Rejecting that completed snapshot starves the dashboard whenever active
    Claude/Codex sessions keep appending faster than a source build finishes.
    Stats movement remains covered by the preceding fail-closed regression.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    tui_module = ns["_cctally_tui"]
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    try:
        prior = tui_module._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
        from _lib_snapshot_cache import SnapshotSignature

        signatures = iter((
            SnapshotSignature(
                max_entry_id=1,
                max_wus_id=0,
                max_wcs_id=0,
                reset_sig=(0, 0),
                max_codex_id=1,
                generation=0,
                entry_mutation_seq=1,
                codex_physical_mutation_seq=1,
            ),
            SnapshotSignature(
                max_entry_id=2,
                max_wus_id=0,
                max_wcs_id=0,
                reset_sig=(0, 0),
                max_codex_id=2,
                generation=0,
                entry_mutation_seq=2,
                codex_physical_mutation_seq=2,
            ),
        ))
        monkeypatch.setitem(
            ns, "compute_signature", lambda *args, **kwargs: next(signatures),
        )

        rebuilt = tui_module._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats,
            now_utc=now + ns["dt"].timedelta(minutes=1),
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            prior_bundle=prior,
        )

        assert rebuilt is not prior
        assert rebuilt.sources["claude"].data_version.startswith("claude:1:1:")
        assert rebuilt.sources["codex"].data_version.startswith("codex:1:1:")
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
    conversations = ns["open_conversations_db"]()
    try:
        ns["_cctally_cache"].sync_claude_conversations(conversations)
    finally:
        conversations.close()
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
        # #556 S2 §3.6: this prune removes the ONLY Claude session, so the daily
        # panel empties and the shared aggregate range takes its fallback
        # branch. Both branches now floor to display-timezone midnight of the
        # SAME calendar day — the panel is either a full thirty-row calendar or
        # empty, and the fallback names `today - 29` exactly as the panel's
        # oldest row does — so the resolved start does NOT move here, and Codex
        # is reused by object identity.
        #
        # Assert the shared start POSITIVELY rather than inferring it from
        # unchanged data. The two providers carry the start at day granularity
        # in their version material by design, precisely so a provider-only
        # mutation cannot leave the composed aggregate describing a range one
        # half no longer covers; naming the fragment is what proves the
        # lockstep held rather than that nothing happened to notice.
        claude_scope = after_prune.source_bundle.sources["claude"].aggregate_scope
        assert (
            claude_scope["range"]["start_at"]
            == first.source_bundle.sources["claude"].aggregate_scope["range"]["start_at"]
        ), "the fallback resolves the same calendar day the panel floor did"
        assert after_prune.source_bundle.sources["codex"] is first.source_bundle.sources["codex"], (
            "the shared start did not move, so exact-version reuse holds"
        )
        assert after_prune.source_bundle.sources["codex"].data == first.source_bundle.sources["codex"].data

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

    def capture(context, *, data_version, **kwargs):
        # `**kwargs` rather than a fixed list: this double stands in for the
        # real builder only to observe the context it is handed, so a keyword
        # added to that builder must pass through rather than break the case.
        seen.append(context)
        return original_builder(context, data_version=data_version, **kwargs)

    try:
        (provider_root / "config.toml").write_text(
            'service_tier = "fast"\n', encoding="utf-8",
        )
        ns["sync_codex_cache"](cache)
        cycle_start, resets_at = _seed_active_codex_weekly_cycle(ns, cache, now=now)
        monkeypatch.setattr(tui_module, "build_codex_source_state", capture)

        bundle = tui_module._tui_build_source_bundle(
            projects_envelope={},
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
        assert bundle.sources["codex"].data["hero"]["cycle"] == {
            "window_minutes": 10_080,
            "start_at": cycle_start.isoformat(),
            "resets_at": resets_at.isoformat(),
        }
        entries = ns["iter_codex_entries"](
            cache,
            cycle_start,
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
        # #556 S3: every row in the legacy array is selected by
        # `alerted_at IS NOT NULL`, so a stub without one is not a shape the
        # projection can receive. Composition now orders on that field and
        # rejects a row that lacks it.
        "alerts": [
            {"axis": "weekly", "threshold": 90, "alerted_at": "2026-07-16T01:30:00Z"},
        ],
        "alerts_settings": {"enabled": True},
    }
    try:
        claude_data = ns["_cctally_tui"]._tui_project_claude_source_data(legacy_envelope)
        bundle = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
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


def test_an_unrecognized_projected_metric_fails_rather_than_picking_a_side():
    """#556 S3 §3.4. This row used to be dropped silently.

    Dropping it hid it from every surface, and the obvious replacement —
    treating an unrecognized metric as Claude's — would put a future Codex-side
    projected metric in the Claude tab. The classifier refuses both.
    """
    ns = load_script()
    legacy = {
        "alerts": [
            {"axis": "projected", "metric": "five_hour_pct", "threshold": 90,
             "alerted_at": "2026-07-16T07:00:00Z"},
        ],
    }

    with pytest.raises(ValueError, match="projected metric"):
        ns["_cctally_tui"]._tui_project_claude_source_data(legacy)


def test_source_bundle_retains_prior_whole_codex_state_on_ingest_contention(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    try:
        prior = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
        current = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
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
            projects_envelope={},
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
            projects_envelope={},
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
        current = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
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
            projects_envelope={},
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=12.5,
            claude_total_tokens=120,
        )
        current = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
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
            projects_envelope={},
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
            projects_envelope={},
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

    def signature_failure(_stats_conn, **_kwargs):
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


def test_source_bundle_hero_uses_native_cycle_while_periods_respect_visible_range(
    tmp_path, monkeypatch,
):
    ns, _root, _rollout, cache = _sync_setup(tmp_path, monkeypatch)
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 20, tzinfo=ns["dt"].timezone.utc)
    visible_start = ns["dt"].datetime(2026, 7, 17, tzinfo=ns["dt"].timezone.utc)
    try:
        ns["sync_codex_cache"](cache)
        _seed_active_codex_weekly_cycle(ns, cache, now=now)
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

        narrowed = build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=visible_start,
                now_utc=now,
                display_tz_name="UTC",
            ),
            data_version="narrowed",
        )
        assert narrowed.data["hero"] == historical.data["hero"]
        assert narrowed.data["periods"]["daily"]["rows"] == ()

        bundle = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=1.25,
            claude_total_tokens=25,
            common_range_start=visible_start,
        )

        assert bundle.sources["codex"].data["hero"] == historical.data["hero"]
        assert bundle.sources["codex"].data["periods"]["daily"]["rows"] == ()
        # #556 S1: Codex's provider `availability` is `empty` here — the
        # VISIBLE range holds nothing — but its hero is a separate
        # cycle-bounded read that holds real spend, so its leg is `current`
        # and contributes. An empty LEG means no accounting at all.
        assert bundle.sources["codex"].availability == "empty"
        combined = bundle.sources["all"].data["combined"]
        assert combined["legs"]["codex"]["state"] == "current"
        assert combined["cost_usd"] == pytest.approx(
            historical.data["hero"]["cost_usd"] + 1.25)
        assert combined["total_tokens"] == (
            historical.data["hero"]["total_tokens"] + 25)
        assert combined["legs"]["codex"]["cost_usd"] == pytest.approx(
            historical.data["hero"]["cost_usd"])
        assert combined["legs"]["claude"]["cost_usd"] == 1.25
    finally:
        cache.close()
        stats.close()


def test_dashboard_no_sync_skips_both_ingests_and_reads_cached_codex_source(
    tmp_path, monkeypatch,
):
    ns, _root, _rollout, cache = _sync_setup(tmp_path, monkeypatch)
    try:
        ns["sync_codex_cache"](cache)
        _seed_active_codex_weekly_cycle(
            ns, cache,
            now=ns["dt"].datetime(2026, 7, 20, tzinfo=ns["dt"].timezone.utc),
        )
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
            projects_envelope={},
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
            projects_envelope={},
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
            projects_envelope={},
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
            projects_envelope={},
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


def test_dashboard_dispatch_retries_a_hero_scoped_codex_projection_after_certificate_recovery(
    tmp_path, monkeypatch,
):
    """Certificate-only recovery must not idle on the incoherent generation.

    Recovery moves no dispatch-signature leg, only the certificate. Since #857
    the certificate is a leg of the Codex quota-dependency identity, which
    rides the dispatch KEY, so the recovery tick takes the FULL path — legacy
    rows rebuilt once — and republishes a coherent Codex generation; the tick
    after it idles again.
    """
    ns, _root, rollout, cache = _sync_setup(tmp_path, monkeypatch)
    import _cctally_quota as quota_module
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    stats = ns["open_db"]()
    try:
        _append_active_codex_weekly_snapshot(ns, rollout, now=now)
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
        assert unavailable_codex.availability == "partial"
        assert unavailable_codex.freshness == "fresh"
        assert unavailable_codex.data is not None
        assert unavailable_codex.capabilities["hero"].status == "unavailable"
        assert unavailable_codex.data["hero"]["cycle"] is None
        assert unavailable_codex.warnings[0].code == "codex_projection_incoherent"
        assert unavailable_codex.warnings[0].domain == "hero"

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

        # #857: the certificate is a leg of the Codex quota-dependency
        # identity, which rides the dispatch KEY, so recovery now takes the
        # FULL path — legacy rows included — rather than the idle path's
        # bounded source adapter. Once per change: the next tick idles.
        tui = ns["_cctally_tui"]
        legacy_builds = []
        real_forecast = tui._tui_build_forecast_view

        def counted_forecast(*args, **kwargs):
            legacy_builds.append(1)
            return real_forecast(*args, **kwargs)

        monkeypatch.setattr(tui, "_tui_build_forecast_view", counted_forecast)
        recovered = ns["_tui_build_snapshot"](
            now_utc=now,
            skip_sync=True,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )
        assert recovered.source_bundle.sources["codex"].availability == "ok"
        assert recovered.source_bundle.sources["codex"] is not unavailable_codex
        assert recovered.source_bundle.sources["claude"] is unavailable.source_bundle.sources["claude"]
        assert len(legacy_builds) == 1, (
            "the certificate recovery must rebuild the legacy rows exactly once")
        again = ns["_tui_build_snapshot"](
            now_utc=now,
            skip_sync=True,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )
        assert len(legacy_builds) == 1, "the recovered snapshot must idle"
        assert (again.source_bundle.sources["codex"]
                is recovered.source_bundle.sources["codex"])
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
        assert first_codex.availability == "partial"
        assert first_codex.freshness == "fresh"
        assert first_codex.data is not None
        assert first_codex.capabilities["hero"].status == "unavailable"
        assert first_codex.data["hero"]["cycle"] is None
        assert first_codex.warnings[0].code == "codex_projection_incoherent"
        # #556 S1 §4.1: `hero` means current-cycle accounting resolvability.
        # An incoherent projection certificate leaves the cycle unresolvable
        # and the counters unpublishable, which is exactly what this axis now
        # reports. Provider `availability` and `freshness` are untouched, per
        # the standing prohibition on degrading them for a domain-local fact.
        assert dict(first_codex.domain_freshness) == {
            "hero": "stale",
            "quota": "stale",
            "sessions": "fresh",
        }
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
            assert codex.availability == "partial"
            assert codex.freshness == "fresh"
            assert codex.data is not None
            assert codex.capabilities["hero"].status == "unavailable"
            assert codex.data["hero"]["cycle"] is None
            assert codex.warnings[0].code == "codex_projection_incoherent"
            # #556 S1 §4.1, as above: an unresolvable cycle stales the
            # accounting axis while provider metadata stays coherent.
            assert dict(codex.domain_freshness) == {
                "hero": "stale",
                "quota": "stale",
                "sessions": "fresh",
            }
            assert snapshot.source_bundle.sources["claude"] is first.source_bundle.sources["claude"]
            wire = sys.modules["_cctally_dashboard_envelope"]._source_state_to_wire(codex)
            assert "clock_data" not in wire
            assert wire["data"]["periods"]["daily"] is not None
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
        assert degraded.source_bundle.sources["codex"].availability == "partial"
        assert degraded.source_bundle.sources["codex"].data is not None
        assert degraded.source_bundle.sources["codex"].capabilities["hero"].status == "unavailable"
        full_degraded_range = observed_ranges[-1]

        retried = ns["_tui_build_snapshot"](
            now_utc=now,
            skip_sync=True,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )
        idle_retry_range = observed_ranges[-1]
        assert retried.source_bundle.sources["codex"].availability == "partial"
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


def test_codex_projection_and_source_build_failed_retain_prior_then_recover(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    try:
        prior = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
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
            source_module = sys.modules["_cctally_dashboard_sources"]
            m.setattr(
                source_module,
                "codex_projection_coherence",
                lambda *_args, **_kwargs: source_module.ProjectionCoherence(False, "mismatch"),
            )
            incoherent = ns["_cctally_tui"]._tui_build_source_bundle(
                projects_envelope={},
                stats_conn=stats,
                now_utc=now,
                display_tz_name="UTC",
                codex_ingest_contended=False,
                claude_cost_usd=1.0,
                claude_total_tokens=10,
                prior_bundle=prior,
            )
        assert incoherent.sources["codex"].data is not prior.sources["codex"].data
        assert incoherent.sources["codex"].availability == "partial"
        assert incoherent.sources["codex"].freshness == "fresh"
        assert incoherent.sources["codex"].capabilities["hero"].status == "unavailable"
        # #769 S6 / #753 (D4): the operands are no longer erased. The prior
        # generation was coherent, so its cohort is republished with a quiet
        # `updating` marker while reconciliation is in flight; the capability
        # and the warning are unchanged, and they are what disclose the state.
        # The retained cycle is None because the prior generation resolved no
        # cycle either — the cohort travels with the boundary that produced it.
        assert incoherent.sources["codex"].data["hero"]["update_state"] == "updating"
        assert incoherent.sources["codex"].data["hero"]["cycle"] is None
        assert (incoherent.sources["codex"].data["hero"]["total_tokens"]
                == prior.sources["codex"].data["hero"]["total_tokens"])
        assert (incoherent.sources["codex"].data["hero"]["cost_usd"]
                == prior.sources["codex"].data["hero"]["cost_usd"])
        assert incoherent.sources["codex"].warnings[0].code == "codex_projection_incoherent"
        assert incoherent.sources["codex"].warnings[0].domain == "hero"
        assert incoherent.sources["all"].data["combined"] is None

        recovered = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
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
        errors = []

        class PrivateDashboardLogger:
            def error(self, *args, **kwargs):
                errors.append((args, kwargs))

        with monkeypatch.context() as m:
            m.setattr(
                ns["_cctally_tui"],
                "_lib_log",
                SimpleNamespace(get_logger=lambda name: PrivateDashboardLogger()),
                raising=False,
            )
            m.setattr(
                ns["_cctally_tui"], "build_codex_source_state",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private /canary/root")),
            )
            failed_domain = ns["_cctally_tui"]._tui_build_source_bundle(
                projects_envelope={},
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
        assert errors == [(
            ("codex_read_model source build failed",),
            {"exc_info": True},
        )]
        assert "private /canary/root" not in repr(failed_domain.sources["codex"].warnings)

        recovered_domain = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
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
    claude_entry_count = 10_000
    claude_file_count = 200
    codex_entry_count = 20_000
    codex_file_count = 400
    project_count = 200
    quota_count = 24
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        root_key = "root-scale"
        timestamp = (now - ns["dt"].timedelta(minutes=1)).isoformat()
        cache.executemany(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ((f"/fixture/claude/{index % claude_file_count}.jsonl", index, timestamp,
              "claude-3-5-sonnet-20241022", 100, 10)
             for index in range(claude_entry_count)),
        )
        cache.executemany(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ((f"/fixture/claude/{index}.jsonl", 1, index, 1, timestamp)
             for index in range(claude_file_count)),
        )
        cache.executemany(
            "INSERT INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ((f"/fixture/codex/{index}.jsonl", 1, index, 1, timestamp)
             for index in range(codex_file_count)),
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
             for index in range(project_count)),
        )
        cache.executemany(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            "input_tokens, output_tokens, total_tokens, source_root_key, conversation_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ((f"/fixture/codex/{index % codex_file_count}.jsonl", index, timestamp,
              f"session-{index % project_count}", "gpt-5", 100, 10, 110,
              root_key, f"conversation-{index % project_count}")
             for index in range(codex_entry_count)),
        )
        quota_rows = []
        for index in range(quota_count):
            logical_limit = f"limit-{index}"
            slot = f"slot-{index}"
            resets_at = (now + ns["dt"].timedelta(hours=5 + index)).isoformat()
            quota_rows.append((
                "codex", root_key, f"/fixture/quota/{index}.jsonl", index,
                timestamp, slot, logical_limit, f"limit-id-{index}", "Scale quota",
                300, 25.0, resets_at,
            ))
        weekly_resets_at = (now + ns["dt"].timedelta(days=1)).isoformat()
        quota_rows.append((
            "codex", root_key, "/fixture/quota/weekly.jsonl", 24,
            timestamp, "weekly-slot", "weekly-limit", "weekly-limit-id",
            "Scale weekly quota", 10_080, 25.0, weekly_resets_at,
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
        quota_block_rows = [
            ("codex", root_key, f"limit-{index}", f"slot-{index}", 300,
             "Scale quota", (now + ns["dt"].timedelta(hours=5 + index)).isoformat(),
             timestamp, timestamp, timestamp, 25.0, 25.0,
             f"/fixture/quota/{index}.jsonl", index, "scale")
            for index in range(24)
        ]
        quota_block_rows.append((
            "codex", root_key, "weekly-limit", "weekly-slot", 10_080,
            "Scale weekly quota", weekly_resets_at,
            (now - ns["dt"].timedelta(minutes=10_080)).isoformat(),
            timestamp, timestamp, 25.0, 25.0,
            "/fixture/quota/weekly.jsonl", 24, "scale",
        ))
        stats.executemany(
            "INSERT INTO quota_window_blocks "
            "(source, source_root_key, logical_limit_key, observed_slot, window_minutes, "
            "limit_name, resets_at_utc, nominal_start_at_utc, first_observed_at_utc, "
            "last_observed_at_utc, first_percent, current_percent, last_source_path, "
            "last_line_offset, generation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            quota_block_rows,
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
            digest_statements = []
            stats.set_trace_callback(digest_statements.append)
            digest_started = time.perf_counter()
            try:
                tui.codex_stats_digest(stats)
            finally:
                stats.set_trace_callback(None)
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
        assert len(codex.data["projects"]["rows"]) == project_count
        blocks = codex.data["quota"]["blocks"]
        assert len(blocks) == quota_count
        assert all(block["window_minutes"] == 300 for block in blocks)
        assert all(block["model_breakdowns"] for block in blocks)
        assert not any(block["label"] == "Scale weekly quota" for block in blocks)
        assert calls == {"claude": 4, "codex": 4}
        assert all(idle.source_bundle.sources["claude"] is first.source_bundle.sources["claude"]
                   for idle in idles)
        assert all(idle.source_bundle.sources["codex"] is codex for idle in idles)
        # #556 S1 §3.7 "empty versus unresolved": this fixture seeds Claude
        # accounting (the provider is `ok`) but no `weekly_usage_snapshots`, so
        # no subscription week resolves. That is a FAILURE, not emptiness, and
        # the combined figure is withheld with Claude's own named reason rather
        # than published as if the missing leg were zero.
        assert first.source_bundle.sources["claude"].availability == "ok"
        assert combined is None
        unavailable = first.source_bundle.sources["all"].data["combined_unavailable"]
        assert unavailable["code"] == "claude_cycle_unresolved"
        assert codex.data["hero"]["cost_usd"] > 0
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

        # The structural claims, which fail identically on every machine.
        #
        # The digest reads bounded indexed aggregates: a fixed number of
        # statements over 20,000 Codex entries, 400 files, 200 conversations
        # and 25 quota windows. A regression to per-row or per-window queries
        # crosses this bound by two orders of magnitude.
        # Measured at 9 on this fixture: seven relation aggregates plus the
        # warm digest memo's two fixed PRAGMA reads (`database_list`, which
        # names the file it stats, and `journal_mode`).  The bound leaves room
        # for one more fixed query without leaving room for a per-window query.
        assert len(digest_statements) <= 10, (
            f"{len(digest_statements)} statements to digest a fixture of "
            f"{codex_entry_count:,} entries; the digest is no longer bounded"
        )
        # The idle path reuses each provider's state object rather than
        # rebuilding it, asserted above by identity, and `forbidden_rollout_scan`
        # forbids the rollout walk outright.
        #
        # The durations remain diagnostic output only. Absolute ceilings made
        # this production-shape fixture compete with the suite's own 120-second
        # per-test cap under xdist load; the query bound, object identities and
        # forbidden rollout walk are the deterministic regression claims.
        print(
            "source-scale "
            f"changed={changed_elapsed:.3f}s digest={digest_elapsed:.3f}s "
            f"digest_statements={len(digest_statements)} "
            f"idle3={idle_elapsed:.3f}s "
            f"rows={codex_entry_count}/{claude_entry_count} "
            f"files={codex_file_count} quota={quota_count}+1 "
            f"projects={project_count}"
        )
    finally:
        cache.close()
        stats.close()


# ==========================================================================
# #341 finding 9 — account registry / active-identity digest invalidation.
# An account SWITCH with zero new ingested rows must still surface: the digest
# folds into each source's data_version + the dispatch/idle signature so the
# next tick rebuilds the source state (flipping the `active` marker).
# ==========================================================================

def _seed_accounts(stats, rows):
    for r in rows:
        stats.execute(
            "INSERT INTO accounts (account_key, provider, natural_id, email, "
            "label, plan_type, label_source, first_seen_utc, last_seen_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (r["account_key"], r["provider"], r.get("natural_id"), r.get("email"),
             r.get("label"), r.get("plan_type"), r.get("label_source", "auto"),
             r.get("first_seen_utc", "2026-07-01T00:00:00Z"),
             r.get("last_seen_utc", "2026-07-01T00:00:00Z")),
        )
    stats.commit()


_ACCT_A = "a" * 32
_ACCT_B = "b" * 32


def _seed_two_claude_accounts(stats):
    _seed_accounts(stats, [
        dict(account_key=_ACCT_A, provider="claude", email="a@x.com", label="alice"),
        dict(account_key=_ACCT_B, provider="claude", email="b@x.com", label="bob"),
    ])


def test_accounts_identity_digest_empty_when_no_accounts(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    from _cctally_dashboard_sources import accounts_identity_digest
    stats = ns["open_db"]()
    try:
        # No account ever observed -> byte-neutral empty digest (R8: never folded).
        assert accounts_identity_digest(stats) == ""
    finally:
        stats.close()


def test_accounts_identity_digest_flips_on_switch_and_label(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_account
    from _cctally_dashboard_sources import accounts_identity_digest
    stats = ns["open_db"]()
    try:
        _seed_two_claude_accounts(stats)
        monkeypatch.setattr(_cctally_account, "resolve_active_account_keys",
                            lambda: {_ACCT_A})
        before = accounts_identity_digest(stats)
        assert before != ""  # registry present -> non-empty
        # Switch active alice -> bob with ZERO new rows: digest must change.
        monkeypatch.setattr(_cctally_account, "resolve_active_account_keys",
                            lambda: {_ACCT_B})
        after_switch = accounts_identity_digest(stats)
        assert after_switch != before
        # A label edit also changes it (new-account / label leg).
        stats.execute("UPDATE accounts SET label='robert' WHERE account_key=?",
                      (_ACCT_B,))
        stats.commit()
        assert accounts_identity_digest(stats) != after_switch
    finally:
        stats.close()


def test_dispatch_signature_and_data_version_flip_on_account_switch(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_account
    stats = ns["open_db"]()
    try:
        _seed_two_claude_accounts(stats)
        monkeypatch.setattr(_cctally_account, "resolve_active_account_keys",
                            lambda: {_ACCT_A})
        sig_a = ns["_cctally_tui"]._tui_compute_dispatch_signature(stats)
        monkeypatch.setattr(_cctally_account, "resolve_active_account_keys",
                            lambda: {_ACCT_B})
        sig_b = ns["_cctally_tui"]._tui_compute_dispatch_signature(stats)
        assert sig_a.accounts_digest != ""
        assert sig_a != sig_b
        assert ns["_snapshot_data_version"](sig_a) != ns["_snapshot_data_version"](sig_b)
    finally:
        stats.close()


def test_source_bundle_rebuilds_on_account_switch(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_account
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    try:
        _seed_two_claude_accounts(stats)
        monkeypatch.setattr(_cctally_account, "resolve_active_account_keys",
                            lambda: {_ACCT_A})
        first = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats, now_utc=now, display_tz_name="UTC",
            codex_ingest_contended=False, claude_cost_usd=1.0, claude_total_tokens=10,
        )
        monkeypatch.setattr(_cctally_account, "resolve_active_account_keys",
                            lambda: {_ACCT_B})
        second = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats, now_utc=now, display_tz_name="UTC",
            codex_ingest_contended=False, claude_cost_usd=1.0, claude_total_tokens=10,
            prior_bundle=first,
        )
        # Switch rebuilt the claude source (new object + new data_version).
        assert second.sources["claude"] is not first.sources["claude"]
        assert (second.sources["claude"].data_version
                != first.sources["claude"].data_version)
    finally:
        stats.close()


# =========================================================================
# public #5 — the Codex ingest backlog is part of the Codex data version.
#
# The `codex_ingest_backlog` cache_meta record feeds `codex.data.ingest_backlog`,
# which is what the hero's "totals will rise" note renders from. It was NOT a
# signature leg, so a backlog-only change reached no envelope: the dispatch
# signature stayed flat (idle short-circuit) and the memoised Codex source block
# was reused verbatim. Confirmed in a real browser — writing the record alone
# produced no envelope change across 90+ seconds of polling, and it surfaced only
# once an unrelated `codex_physical_mutation_seq` bump forced a rebuild.
#
# It usually works because `sync_codex_cache` writes the record alongside newly
# ingested rows, which does move that sequence. The exposure is a budgeted tick
# whose walk consumes only deduped or non-`token_count` bytes: the sequence does
# not move, and the note goes stale or missing until some unrelated Codex
# mutation happens along.
# =========================================================================

_BACKLOG_RECORD = '{"bytes": 8192, "files": 3, "since": "2026-07-16T09:00:00Z"}'


def _write_backlog(conn, value=_BACKLOG_RECORD):
    conn.execute(
        "INSERT OR REPLACE INTO cache_meta(key, value) VALUES "
        "('codex_ingest_backlog', ?)", (value,))
    conn.commit()


def _clear_backlog(conn):
    conn.execute("DELETE FROM cache_meta WHERE key='codex_ingest_backlog'")
    conn.commit()


def test_snapshot_signature_carries_the_codex_ingest_backlog(tmp_path, monkeypatch):
    """The leg itself: an O(1) `cache_meta` read, empty when nothing is owed."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        assert compute_signature(
            cache, stats, generation=0).codex_ingest_backlog_sig == ""
        _write_backlog(cache)
        owed = compute_signature(cache, stats, generation=0)
        assert owed.codex_ingest_backlog_sig != ""
        # Drained is byte-identical to never-had-one: the writer DELETEs the key
        # at zero, so every install that has finished ingesting keeps exactly
        # today's version string.
        _clear_backlog(cache)
        assert compute_signature(
            cache, stats, generation=0).codex_ingest_backlog_sig == ""
    finally:
        cache.close()
        stats.close()


def test_snapshot_data_version_changes_when_only_the_ingest_backlog_changes(
    tmp_path, monkeypatch,
):
    """The dispatch signal: a backlog-only tick must leave the idle path.

    Without this the whole snapshot short-circuits, the source bundle builder is
    never reached, and no version below it can matter.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        before = compute_signature(cache, stats, generation=0)
        _write_backlog(cache)
        after = compute_signature(cache, stats, generation=0)
        drained = None

        assert before != after, "the idle short-circuit compares the whole tuple"
        assert (ns["_snapshot_data_version"](before)
                != ns["_snapshot_data_version"](after))

        _clear_backlog(cache)
        drained = compute_signature(cache, stats, generation=0)
        assert ns["_snapshot_data_version"](drained) == \
            ns["_snapshot_data_version"](before)
    finally:
        cache.close()
        stats.close()


def test_codex_source_is_rebuilt_when_only_the_ingest_backlog_record_changes(
    tmp_path, monkeypatch,
):
    """End to end, in the shape the browser observed.

    `reuse_coherent_source_state` hands back the PRIOR Codex object whenever the
    version matches, so a backlog written between two builds has to move
    `codex_version` or the new field never reaches the envelope — even on a full
    rebuild forced by some other change.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    try:
        first = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats, now_utc=now, display_tz_name="UTC",
            codex_ingest_contended=False, claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
        assert "ingest_backlog" not in first.sources["codex"].data

        cache = ns["open_cache_db"]()
        try:
            _write_backlog(cache)
        finally:
            cache.close()

        second = ns["_cctally_tui"]._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats, now_utc=now + ns["dt"].timedelta(minutes=1),
            display_tz_name="UTC", codex_ingest_contended=False,
            claude_cost_usd=0.0, claude_total_tokens=0, prior_bundle=first,
        )

        codex = second.sources["codex"]
        assert codex.data_version != first.sources["codex"].data_version
        assert codex is not first.sources["codex"]
        assert dict(codex.data["ingest_backlog"]) == {
            "files": 3, "bytes": 8192, "since": "2026-07-16T09:00:00Z"}
        # The OTHER provider must not be dragged into a Codex-local change.
        assert (second.sources["claude"].data_version
                == first.sources["claude"].data_version)
    finally:
        stats.close()


# --- #556 S1 Task 4 — period identity in `claude_version` (spec §3.6) -------


_ROLLOVER_WEEK_START = "2026-07-13T14:00:00Z"
_ROLLOVER_WEEK_END = "2026-07-20T14:00:00Z"
_ROLLOVER_NEXT_END = "2026-07-27T14:00:00Z"


def _claude_data_for_week(tui_module, *, week_start: str, week_end: str):
    """Project a minimal legacy envelope whose current week is [start, end).

    The production caller builds this from ``snapshot_to_envelope``; the shape
    that matters here is the pair of effective bounds ``_tui_build_current_week``
    stores AFTER ``_apply_midweek_reset_override``.
    """
    return tui_module._tui_project_claude_source_data({
        "daily": {"total_cost_usd": 99.0, "total_tokens": 9_000},
        "current_week": {
            "week_start_at": week_start,
            "reset_at_utc": week_end,
            "spent_usd": 12.5,
            "total_tokens": 1_000,
            "used_pct": 40.0,
            "milestones": [],
            "five_hour_milestones": [],
        },
    })


def _frozen_signature_bundle_builder(ns, monkeypatch):
    """Pin every database signature so only the clock can move a version."""
    from _lib_snapshot_cache import SnapshotSignature

    frozen = SnapshotSignature(
        max_entry_id=7,
        max_wus_id=3,
        max_wcs_id=2,
        reset_sig=(1, 4),
        max_codex_id=5,
        generation=0,
        entry_mutation_seq=11,
        codex_physical_mutation_seq=6,
    )
    monkeypatch.setitem(ns, "compute_signature", lambda *a, **k: frozen)
    monkeypatch.setattr(
        ns["_cctally_tui"], "codex_stats_digest", lambda _conn: "c" * 64,
    )
    monkeypatch.setattr(
        ns["_cctally_tui"], "accounts_identity_digest", lambda _conn: "",
    )
    return ns["_cctally_tui"]


def test_claude_source_version_changes_on_a_clock_only_week_rollover(
    tmp_path, monkeypatch,
):
    """#556 S1 §3.6 — a nominal rollover must invalidate the generation.

    This is CLOCK-ONLY on purpose: every database signature
    (`max_entry_id`, `entry_mutation_seq`, `max_wus_id`, `max_wcs_id`,
    `reset_sig`, `generation`), the Codex stats digest and the accounts digest
    are all pinned, and the only differences between the two builds are
    `now_utc` crossing `week_end_at` and the newly-resolved week that crossing
    produces. A test that also changed a row would pass against the bug,
    because the row would move the signature on its own.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    tui_module = _frozen_signature_bundle_builder(ns, monkeypatch)
    stats = ns["open_db"]()
    before = ns["dt"].datetime(2026, 7, 19, 12, 0, tzinfo=ns["dt"].timezone.utc)
    after = ns["dt"].datetime(2026, 7, 20, 15, 0, tzinfo=ns["dt"].timezone.utc)
    try:
        # #556 S2: the shared aggregate range is pinned across both builds, for
        # the same reason every database signature is. Left unpinned it would
        # default to `now_utc - 30 days` and move with the clock, so this test
        # would witness a range change rather than the week rollover it is
        # about — and the Codex assertion below would then be measuring the
        # deliberate lockstep invalidation of §3.6 instead.
        pinned_range_start = before - ns["dt"].timedelta(days=30)
        first = tui_module._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats, now_utc=before, display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0, claude_total_tokens=0,
            common_range_start=pinned_range_start,
            claude_data=_claude_data_for_week(
                tui_module,
                week_start=_ROLLOVER_WEEK_START, week_end=_ROLLOVER_WEEK_END,
            ),
        )
        rolled = tui_module._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats, now_utc=after, display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0, claude_total_tokens=0,
            common_range_start=pinned_range_start,
            claude_data=_claude_data_for_week(
                tui_module,
                week_start=_ROLLOVER_WEEK_END, week_end=_ROLLOVER_NEXT_END,
            ),
            prior_bundle=first,
        )
    finally:
        stats.close()

    assert after >= ns["parse_iso_datetime"](_ROLLOVER_WEEK_END, "week_end_at")
    assert (rolled.sources["claude"].data_version
            != first.sources["claude"].data_version)
    assert rolled.sources["claude"] is not first.sources["claude"]
    # The rollover is Claude-local WITH THE RANGE HELD FIXED, which is what
    # `pinned_range_start` above does. Codex must not be dragged into a
    # subscription-week rollover by itself.
    #
    # With a real derived range this is not the whole story, and the difference
    # is deliberate rather than an oversight: a rollover that also crosses a
    # display day moves the shared start, and #556 S2 §3.6 puts that start in
    # BOTH providers' version material precisely so they rebuild together. So
    # a day-crossing rollover DOES invalidate Codex. This test isolates the
    # period identity from the range identity; the lockstep is asserted in
    # `test_real_dispatch_keeps_the_unchanged_provider_object_across_owned_changes`
    # and in `tests/test_556_s2_aggregate_ranges.py`.
    assert (rolled.sources["codex"].data_version
            == first.sources["codex"].data_version)


def test_an_unchanged_claude_period_does_not_churn_the_source_version(
    tmp_path, monkeypatch,
):
    """The identity must not defeat reuse on an ordinary tick.

    The second build spells the same instants with a UTC offset instead of a
    trailing `Z`, which is the spelling difference legacy rows actually carry
    (`_tui_build_current_week` collects both variants), so the identity has to
    normalize rather than hash the raw text.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    tui_module = _frozen_signature_bundle_builder(ns, monkeypatch)
    stats = ns["open_db"]()
    now = ns["dt"].datetime(2026, 7, 19, 12, 0, tzinfo=ns["dt"].timezone.utc)
    try:
        first = tui_module._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats, now_utc=now, display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0, claude_total_tokens=0,
            claude_data=_claude_data_for_week(
                tui_module,
                week_start=_ROLLOVER_WEEK_START, week_end=_ROLLOVER_WEEK_END,
            ),
        )
        again = tui_module._tui_build_source_bundle(
            projects_envelope={},
            stats_conn=stats, now_utc=now + ns["dt"].timedelta(minutes=5),
            display_tz_name="UTC", codex_ingest_contended=False,
            claude_cost_usd=0.0, claude_total_tokens=0,
            claude_data=_claude_data_for_week(
                tui_module,
                week_start="2026-07-13T16:00:00+02:00",
                week_end="2026-07-20T16:00:00+02:00",
            ),
            prior_bundle=first,
        )
    finally:
        stats.close()

    assert (again.sources["claude"].data_version
            == first.sources["claude"].data_version)
    assert again.sources["claude"] is first.sources["claude"]


# ==========================================================================
# #857 — a SETTLED cycle-unavailable Codex generation idles.
#
# Measured on a full-size store at the production 5-second interval: once the
# latest weekly window resets with no newer Codex observation, the build
# publishes `partial`/`fresh` with the one warning `codex_cycle_unavailable`,
# `_tui_source_bundle_can_idle` refuses it on availability, and every idle tick
# paid a full Codex capture and build (~30% CPU on an idle dashboard). Nothing
# about that generation can change until its evidence moves or a time
# transition its decision deadline records is reached, so these ticks must
# reclock it instead.
# ==========================================================================

_857_UTC = dt.timezone.utc
_857_T0 = dt.datetime(2026, 7, 16, 12, tzinfo=_857_UTC)
#: The window the fixture appends resets one day after T0 and is long past by
#: `_857_AFTER`, with no newer observation — the measured state.
_857_RESET = _857_T0 + dt.timedelta(days=1)
_857_AFTER = _857_T0 + dt.timedelta(days=2)
#: The production sync interval of the measurement.
_857_TICK = dt.timedelta(seconds=5)


def _857_append_weekly_snapshot(rollout, *, captured_at, resets_at):
    payload = {
        "type": "event_msg",
        "timestamp": captured_at.isoformat(),
        "payload": {
            "type": "token_count",
            "info": {
                "rate_limits": {
                    "limit_id": "fixture-weekly-limit",
                    "limit_name": "Fixture weekly quota",
                    "primary": {
                        "resets_at": int(resets_at.timestamp()),
                        "used_percent": 25.0,
                        "window_minutes": 10_080,
                    },
                },
            },
        },
    }
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _857_store(
    tmp_path, monkeypatch, *,
    captured_at=_857_T0 - dt.timedelta(minutes=1), resets_at=_857_RESET,
):
    """A real store: rollout accounting plus one coherent native weekly window."""
    import _lib_snapshot_cache as sc

    ns, _root, rollout, cache = _sync_setup(tmp_path, monkeypatch)
    _857_append_weekly_snapshot(
        rollout, captured_at=captured_at, resets_at=resets_at)
    ns["sync_codex_cache"](cache)
    sc.reset_dispatch_state()
    ns["_cctally_tui"]._tui_reset_partial_retry_state()
    return ns, cache


def _857_tick(ns, now_utc, **kwargs):
    kwargs.setdefault("skip_sync", True)
    return ns["_tui_build_snapshot"](
        now_utc=now_utc,
        precompute_envelope=True,
        runtime_bind="127.0.0.1",
        **kwargs,
    )


def _857_current_identity(ns):
    """The Codex quota-dependency identity a dispatch read sees now, read the
    way the tick reads it: on the dispatch signature's own cache handle."""
    stats = ns["open_db"]()
    try:
        return ns["_cctally_tui"]._tui_compute_dispatch_signature(
            stats, with_codex_dependency=True)[1]
    finally:
        stats.close()


def _857_identity_on(conn):
    """The identity read on an ALREADY-open ``conn``, bound to its own file.

    The kernel requires a pre-open observation; for a handle a test opened
    earlier, observing its file now is the same observation — nothing replaces
    the file in these tests unless they say so.
    """
    import _lib_snapshot_cache as sc

    opened = sc.codex_cache_file_identity(sc.codex_main_database_path(conn))
    return sc.codex_quota_dependency_identity(conn, opened_file=opened)


def _857_spy(monkeypatch, tui):
    """Count the source-level work each tick does, through the real seams."""
    calls = {
        "source_bundle": 0, "capture": 0, "build": 0, "clock": 0,
        "forecast": 0, "regimes": [],
    }

    def count(name, key):
        original = getattr(tui, name)

        def counted(*args, **kwargs):
            calls[key] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(tui, name, counted)

    count("_tui_build_source_bundle", "source_bundle")
    count("capture_codex_source_state", "capture")
    count("build_codex_source_state_from_capture", "build")
    count("refresh_codex_source_clock", "clock")
    # The legacy rows: zero means every tick took the idle dispatch.
    count("_tui_build_forecast_view", "forecast")
    original_regime = tui._tui_note_codex_regime

    def regime(value):
        calls["regimes"].append(value)
        return original_regime(value)

    monkeypatch.setattr(tui, "_tui_note_codex_regime", regime)
    return calls


def _857_assert_settled(codex):
    assert codex.availability == "partial"
    assert codex.freshness == "fresh"
    assert codex.data is not None
    assert [warning.code for warning in codex.warnings] == [
        "codex_cycle_unavailable"]
    assert codex.capabilities["hero"].status == "unavailable"
    assert codex.data["hero"]["cycle"] is None
    assert codex.data["hero"]["cost_usd"] is None
    assert not (codex.metadata_health or {}).get("retryable")


@pytest.fixture
def settled_store(tmp_path, monkeypatch):
    import _lib_snapshot_cache as sc

    ns, cache = _857_store(tmp_path, monkeypatch)
    handle = {"cache": cache}
    try:
        yield ns, handle
    finally:
        sc.reset_dispatch_state()
        ns["_cctally_tui"]._tui_reset_partial_retry_state()
        handle["cache"].close()


def test_857_a_settled_cycle_unavailable_codex_generation_idles_without_rebuilding(
    settled_store, monkeypatch,
):
    """The measured defect, through real dashboard ticks.

    Beyond the 120-second partial-retry interval, so neither the retry kernel
    nor any time-based rebuild can be what keeps the ticks quiet.
    """
    from _lib_source_retry import PARTIAL_RETRY_INTERVAL

    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    first = _857_tick(ns, _857_AFTER)
    settled = first.source_bundle.sources["codex"]
    _857_assert_settled(settled)
    # Nothing about this generation can change by time alone.
    assert settled.clock_data["codex_next_decision_at"] is None

    calls = _857_spy(monkeypatch, tui)
    steps = int(PARTIAL_RETRY_INTERVAL / _857_TICK) + 6
    snapshots = [
        _857_tick(ns, _857_AFTER + _857_TICK * step)
        for step in range(1, steps + 1)
    ]

    assert calls["forecast"] == 0, (
        "precondition: every tick took the idle dispatch, so any source work "
        "below was chosen by the idle gate")
    assert (calls["source_bundle"], calls["capture"], calls["build"]) == (
        0, 0, 0), (
        "an idle tick rebuilt the settled Codex generation: "
        f"{calls['source_bundle']} source bundles, {calls['capture']} captures, "
        f"{calls['build']} builds over {steps} ticks")
    assert calls["clock"] >= steps, "the idle clock must keep running"
    assert "active" not in calls["regimes"]
    for snapshot in snapshots:
        codex = snapshot.source_bundle.sources["codex"]
        _857_assert_settled(codex)
        assert codex.data_version == settled.data_version
        assert codex.data["periods"] is settled.data["periods"]
        assert snapshot.sessions is first.sessions


def test_857_a_claude_only_advance_rebuilds_claude_and_only_reclocks_the_settled_codex(
    settled_store, monkeypatch,
):
    """The active path: Claude evidence moved, Codex evidence did not."""
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    cache = handle["cache"]
    first = _857_tick(ns, _857_AFTER)
    settled = first.source_bundle.sources["codex"]
    _857_assert_settled(settled)

    calls = _857_spy(monkeypatch, tui)
    cache.execute(
        "INSERT OR REPLACE INTO cache_meta(key, value) "
        "VALUES ('session_entries_mutation_seq', '1')"
    )
    cache.commit()
    second = _857_tick(ns, _857_AFTER + _857_TICK)

    assert calls["source_bundle"] == 1, "precondition: the full path ran"
    claude = second.source_bundle.sources["claude"]
    assert claude is not first.source_bundle.sources["claude"]
    assert claude.data_version != first.source_bundle.sources["claude"].data_version
    assert (calls["capture"], calls["build"]) == (0, 0), (
        "a Claude-only advance rebuilt the unchanged settled Codex generation")
    assert calls["regimes"] == ["idle"]
    codex = second.source_bundle.sources["codex"]
    _857_assert_settled(codex)
    assert codex.data_version == settled.data_version
    assert codex.data["periods"] is settled.data["periods"]


@pytest.mark.parametrize("change", ("codex_evidence", "accounting_pending"))
def test_857_codex_evidence_or_pending_accounting_still_rebuilds_the_settled_codex(
    settled_store, monkeypatch, change,
):
    import _lib_snapshot_cache as sc

    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    cache = handle["cache"]
    _857_assert_settled(
        _857_tick(ns, _857_AFTER).source_bundle.sources["codex"])
    calls = _857_spy(monkeypatch, tui)
    if change == "codex_evidence":
        cache.execute(
            "UPDATE cache_meta SET value=CAST(value AS INTEGER) + 1 "
            "WHERE key='codex_physical_mutation_seq'"
        )
    else:
        # The process accounting memo no longer names the durable ledger head;
        # a Claude advance takes the tick onto the full path where it is read.
        sc.reset_codex_accounting_cache_state()
        cache.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) "
            "VALUES ('session_entries_mutation_seq', '1')"
        )
    cache.commit()
    _857_tick(ns, _857_AFTER + _857_TICK)

    assert (calls["capture"], calls["build"]) == (1, 1)
    assert calls["regimes"] == ["active"]


def test_857_a_reset_crossing_rebuilds_codex_once_then_the_settled_generation_idles(
    settled_store, monkeypatch,
):
    """One authoritative rebuild AT the transition, then settled idling."""
    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    first = _857_tick(ns, _857_RESET - 2 * _857_TICK)
    live = first.source_bundle.sources["codex"]
    assert live.availability == "ok"
    assert live.data["hero"]["cycle"] is not None
    assert live.clock_data["codex_next_decision_at"] == _857_RESET

    calls = _857_spy(monkeypatch, tui)
    _857_tick(ns, _857_RESET - _857_TICK)
    assert (calls["capture"], calls["build"]) == (0, 0)
    crossing = _857_tick(ns, _857_RESET)
    assert (calls["capture"], calls["build"]) == (1, 1)
    _857_assert_settled(crossing.source_bundle.sources["codex"])
    for step in range(1, 31):
        _857_assert_settled(
            _857_tick(ns, _857_RESET + _857_TICK * step)
            .source_bundle.sources["codex"])

    assert (calls["capture"], calls["build"]) == (1, 1), (
        "the settled generation was rebuilt after the crossing")


def test_857_frozen_future_start_evidence_becomes_usable_at_its_deadline(
    tmp_path, monkeypatch,
):
    """A captured window whose nominal start lies ahead resolves strictly after
    that start with NO new evidence. The settled generation before it must
    record that instant, idle up to it, and rebuild exactly once there."""
    import _lib_snapshot_cache as sc

    start = _857_T0 + dt.timedelta(days=1)
    reset = start + dt.timedelta(minutes=10_080)
    ns, cache = _857_store(tmp_path, monkeypatch, resets_at=reset)
    tui = ns["_cctally_tui"]
    try:
        first = _857_tick(ns, start - 2 * _857_TICK)
        settled = first.source_bundle.sources["codex"]
        _857_assert_settled(settled)
        assert settled.clock_data["codex_next_decision_at"] == (
            start + dt.timedelta(microseconds=1))

        calls = _857_spy(monkeypatch, tui)
        _857_tick(ns, start - _857_TICK)
        _857_tick(ns, start)
        assert (calls["capture"], calls["build"]) == (0, 0)
        usable = _857_tick(ns, start + _857_TICK).source_bundle.sources["codex"]
        assert (calls["capture"], calls["build"]) == (1, 1)
        assert usable.availability == "ok"
        assert usable.data["hero"]["cycle"] is not None
        for step in range(2, 8):
            _857_tick(ns, start + _857_TICK * step)
        assert (calls["capture"], calls["build"]) == (1, 1)
    finally:
        sc.reset_dispatch_state()
        tui._tui_reset_partial_retry_state()
        cache.close()


def _857_insert_raw_quota_row(ns, cache, monkeypatch):
    """A quota row written with NO accounting or physical counter moving.

    The manual-repair / writer-omission shape the trigger ledger exists for.
    The window is a 5-hour one that expired before the tick, so the cycle
    verdict itself does not change.
    """
    root_key = cache.execute(
        "SELECT source_root_key FROM codex_source_roots").fetchone()[0]
    cache.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        "observed_slot, logical_limit_key, limit_id, limit_name, window_minutes, "
        "used_percent, resets_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("codex", root_key, "/private/857-raw.jsonl", 857,
         (_857_T0 - dt.timedelta(hours=2)).isoformat(), "fixture-five-hour",
         "fixture-five-hour-limit", "fixture-five-hour-limit",
         "Fixture five-hour quota", 300, 5.0,
         (_857_T0 + dt.timedelta(hours=3)).isoformat()),
    )
    cache.commit()
    return cache


def _857_bump_attribution_revision(ns, cache, monkeypatch):
    cache.execute(
        "INSERT INTO cache_meta(key, value) "
        "VALUES ('codex_window_attribution_revision', '1') "
        "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER) + 1"
    )
    cache.commit()
    return cache


def _857_rewrite_certificate(ns, cache, monkeypatch):
    """Same meaning, different bytes: only the CONTENTS leg can see it."""
    raw = cache.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='codex_quota_projection_certificate'").fetchone()[0]
    cache.execute(
        "UPDATE cache_meta SET value=? "
        "WHERE key='codex_quota_projection_certificate'",
        (json.dumps(json.loads(raw), indent=1, sort_keys=True),),
    )
    cache.commit()
    return cache


def _857_delete_certificate(ns, cache, monkeypatch):
    cache.execute(
        "DELETE FROM cache_meta WHERE key='codex_quota_projection_certificate'")
    cache.commit()
    return cache


def _857_replace_cache_file(ns, cache, monkeypatch):
    """A restored cache.db: identical bytes, a different file."""
    path = pathlib.Path(next(
        str(row[2]) for row in cache.execute("PRAGMA database_list")
        if str(row[1]) == "main"))
    cache.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    cache.close()
    replacement = path.with_name(path.name + ".857-replacement")
    shutil.copyfile(path, replacement)
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    before = path.stat().st_ino
    os.replace(replacement, path)
    assert path.stat().st_ino != before, "precondition: a different file"
    return ns["open_cache_db"]()


def _857_change_registry(ns, cache, monkeypatch):
    monkeypatch.setattr(
        ns["_cctally_tui"], "accounts_identity_digest",
        lambda _conn: "857registrychange")
    return cache


_857_INVALIDATIONS = (
    # (id, mutate, expected Codex builds on the next tick)
    ("raw_quota_row", _857_insert_raw_quota_row, 1),
    ("attribution_revision", _857_bump_attribution_revision, 1),
    ("certificate_rewritten", _857_rewrite_certificate, 1),
    ("certificate_deleted", _857_delete_certificate, 1),
    ("cache_file_replaced", _857_replace_cache_file, 1),
    ("registry_changed", _857_change_registry, 1),
)


@pytest.mark.parametrize(
    "mutate, expected_builds",
    [(mutate, builds) for _id, mutate, builds in _857_INVALIDATIONS],
    ids=[entry[0] for entry in _857_INVALIDATIONS],
)
def test_857_a_moved_codex_quota_input_leaves_the_settled_idle(
    settled_store, monkeypatch, mutate, expected_builds,
):
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    _857_assert_settled(
        _857_tick(ns, _857_AFTER).source_bundle.sources["codex"])
    calls = _857_spy(monkeypatch, tui)
    _857_tick(ns, _857_AFTER + _857_TICK)
    assert (calls["capture"], calls["build"]) == (0, 0), (
        "precondition: the unchanged settled generation idles")

    handle["cache"] = mutate(ns, handle["cache"], monkeypatch)
    moved = _857_tick(ns, _857_AFTER + 2 * _857_TICK)

    assert (calls["capture"], calls["build"]) == (
        expected_builds, expected_builds)
    # Whatever the rebuild published is the authoritative verdict over the
    # moved inputs; a verdict that is still the settled one idles again.
    codex = moved.source_bundle.sources["codex"]
    if [warning.code for warning in codex.warnings] == ["codex_cycle_unavailable"]:
        _857_tick(ns, _857_AFTER + 3 * _857_TICK)
        assert (calls["capture"], calls["build"]) == (
            expected_builds, expected_builds), "a re-settled generation idles"


def test_857_a_ledger_prune_does_not_leave_the_settled_idle(
    settled_store, monkeypatch,
):
    """The projector's prune deletes CONSUMED entries: evidence already
    reflected, not new evidence. The durable `sqlite_sequence` watermark does
    not regress, so the identity — and the idle — must hold across it."""
    import _lib_snapshot_cache as sc

    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    cache = handle["cache"]
    # An entry the settled build will already have seen, so pruning it later
    # removes nothing the build did not account for.
    _857_insert_raw_quota_row(ns, cache, monkeypatch)
    assert cache.execute(
        "SELECT COUNT(*) FROM quota_window_change_log").fetchone()[0] > 0, (
            "non-vacuity: there must be entries to prune")
    settled = _857_tick(ns, _857_AFTER).source_bundle.sources["codex"]
    _857_assert_settled(settled)
    before = _857_identity_on(cache)
    assert settled.clock_data["codex_quota_dependency"] == before
    calls = _857_spy(monkeypatch, tui)
    _857_tick(ns, _857_AFTER + _857_TICK)
    assert (calls["capture"], calls["build"]) == (0, 0)

    cache.execute("DELETE FROM quota_window_change_log")
    cache.commit()
    assert _857_identity_on(cache) == before
    _857_tick(ns, _857_AFTER + 2 * _857_TICK)

    assert (calls["capture"], calls["build"]) == (0, 0)


def test_857_an_unreadable_codex_quota_identity_rebuilds_every_tick(
    settled_store, monkeypatch,
):
    """No identity is not an unchanged identity."""
    import _lib_snapshot_cache as sc

    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    _857_assert_settled(
        _857_tick(ns, _857_AFTER).source_bundle.sources["codex"])
    calls = _857_spy(monkeypatch, tui)
    monkeypatch.setattr(
        sc, "codex_quota_dependency_identity", lambda _conn, **_kw: None)
    for step in (1, 2, 3):
        _857_assert_settled(
            _857_tick(ns, _857_AFTER + _857_TICK * step)
            .source_bundle.sources["codex"])

    assert (calls["capture"], calls["build"]) == (3, 3)


def test_857_ingest_contention_bypasses_the_settled_idle(settled_store, monkeypatch):
    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    _857_assert_settled(
        _857_tick(ns, _857_AFTER).source_bundle.sources["codex"])
    calls = _857_spy(monkeypatch, tui)
    monkeypatch.setitem(
        ns, "sync_cache", lambda _conn: SimpleNamespace(lock_contended=False))
    monkeypatch.setitem(
        ns, "sync_codex_cache", lambda _conn: SimpleNamespace(lock_contended=True))
    contended = _857_tick(ns, _857_AFTER + _857_TICK, skip_sync=False)

    assert calls["source_bundle"] == 1
    codex = contended.source_bundle.sources["codex"]
    assert [warning.code for warning in codex.warnings] == [
        "source_ingest_contended"]
    assert codex.freshness == "stale"


def test_857_a_projection_incoherent_composite_keeps_rebuilding(
    settled_store, monkeypatch,
):
    """Only the SINGLETON cycle warning settles: the composite with an
    incoherent projection is the certificate-recovery case, which must keep
    retrying every tick."""
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    _857_delete_certificate(ns, handle["cache"], monkeypatch)
    first = _857_tick(ns, _857_AFTER).source_bundle.sources["codex"]
    assert sorted(warning.code for warning in first.warnings) == [
        "codex_cycle_unavailable", "codex_projection_incoherent"]
    calls = _857_spy(monkeypatch, tui)
    for step in (1, 2, 3):
        _857_tick(ns, _857_AFTER + _857_TICK * step)

    assert (calls["capture"], calls["build"]) == (3, 3)


def test_857_the_codex_quota_identity_is_stable_cheap_and_agrees_across_readers(
    settled_store,
):
    """The idle decision compares an identity read on the dashboard's pinned
    cache connection with one read at dispatch; they must agree on an
    unchanged store, never move on their own, and read only O(1) rows."""
    import _cctally_quota
    import _lib_snapshot_cache as sc

    # The kernel may not import the quota glue, so it spells the key itself.
    assert sc._CODEX_PROJECTION_CERTIFICATE_KEY == (
        _cctally_quota._DASHBOARD_PROJECTION_CERTIFICATE_KEY)
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    cache = handle["cache"]
    statements = []
    cache.set_trace_callback(statements.append)
    try:
        pinned = _857_identity_on(cache)
    finally:
        cache.set_trace_callback(None)
    assert pinned is not None
    assert _857_identity_on(cache) == pinned
    assert _857_current_identity(ns) == pinned
    reader = ns["open_cache_db"]()
    try:
        assert _857_identity_on(reader) == pinned
    finally:
        reader.close()
    assert statements, "non-vacuity: the trace must see the identity's reads"
    scans = [
        statement for statement in statements
        if re.search(
            r"\bfrom\s+(quota_window_snapshots|codex_session_entries|"
            r"quota_window_change_log|codex_accounting_change_log)\b",
            statement, re.IGNORECASE,
        )
    ]
    assert scans == [], "the per-tick identity read a history relation"


def test_857_a_race_retained_bundle_is_never_restamped_and_loses_its_provenance(
    settled_store, monkeypatch,
):
    """A generation race returns the PRIOR bundle. It must never be restamped
    with the identity of inputs it never read, or the next tick would idle on
    it as though it were current — and (P1) it keeps no provenance at all:
    the raced build may already have consumed accounting that neither the
    Codex version nor the identity can see, so even its OWN older identity is
    withdrawn and no identity, older or newer, admits it."""
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    cache = handle["cache"]
    stats = ns["open_db"]()
    try:
        prior = tui._tui_build_source_bundle(
            stats_conn=stats, now_utc=_857_AFTER, display_tz_name="UTC",
            codex_ingest_contended=False, claude_cost_usd=0.0,
            claude_total_tokens=0, projects_envelope={}, raw_config={},
        )
        settled = prior.sources["codex"]
        _857_assert_settled(settled)
        older = settled.clock_data["codex_quota_dependency"]
        assert older == _857_current_identity(ns)

        _857_insert_raw_quota_row(ns, cache, monkeypatch)
        newer = _857_current_identity(ns)
        assert newer != older
        digests = iter(("857-pre", "857-post"))
        monkeypatch.setattr(
            tui, "codex_stats_digest", lambda _conn: next(digests, "857-post"))
        raced = tui._tui_build_source_bundle(
            stats_conn=stats, now_utc=_857_AFTER + _857_TICK,
            display_tz_name="UTC", codex_ingest_contended=False,
            claude_cost_usd=0.0, claude_total_tokens=0, projects_envelope={},
            raw_config={}, prior_bundle=prior,
        )
    finally:
        stats.close()

    # The prior generation, otherwise untouched.
    assert raced.sources["claude"] is prior.sources["claude"]
    assert raced.sources["codex"].data is settled.data
    assert raced.sources["codex"].data_version == settled.data_version
    assert raced.sources["codex"].clock_data["codex_quota_dependency"] is None
    assert settled.clock_data["codex_quota_dependency"] == older, (
        "withdrawal must not mutate the prior object")
    for identity in (newer, older):
        assert tui._tui_source_bundle_can_idle(
            raced, codex_dependency=identity,
            now_utc=_857_AFTER + 2 * _857_TICK,
        ) is False


def _857_variant(settled, name):
    """One neighbouring state, derived from the REAL settled generation."""
    import _lib_dashboard_sources as kernel

    warning = kernel.SourceDashboardWarning
    cycle = settled.warnings[0]
    extra = {
        "with_projection_incoherent": warning(
            "codex_projection_incoherent",
            "Codex quota projection is unavailable.", "hero"),
        "with_account_scope_unresolved": warning(
            "codex_account_scope_unresolved",
            "Codex account registry could not be read.", "accounts"),
        "with_metadata_incomplete": warning(
            "codex_metadata_incomplete",
            "1 Codex accounting row(s) lack project metadata.", "projects"),
        "with_unknown_warning": warning(
            "codex_some_future_cause", "Something new.", "hero"),
    }
    if name in extra:
        return dataclasses.replace(settled, warnings=(extra[name], cycle))
    if name == "no_stated_warning":
        return dataclasses.replace(settled, warnings=())
    if name == "stale":
        return dataclasses.replace(settled, freshness="stale")
    if name == "missing_data":
        return dataclasses.replace(settled, data=None)
    if name == "retryable_carrier":
        return dataclasses.replace(
            settled,
            metadata_health=kernel.build_metadata_health("transient_read_failure"),
        )
    if name == "aggregate_failed":
        scope = dict(settled.aggregate_scope)
        scope[kernel.AGGREGATE_NAMES[0]] = {"state": "failed"}
        return dataclasses.replace(settled, aggregate_scope=scope)
    raise AssertionError(name)


_857_REFUSED_VARIANTS = (
    "with_projection_incoherent", "with_account_scope_unresolved",
    "with_metadata_incomplete", "with_unknown_warning", "no_stated_warning",
    "stale", "missing_data", "retryable_carrier", "aggregate_failed",
)


def test_857_only_the_singleton_settled_cycle_state_idles(settled_store):
    """The safety matrix for the one Codex-only predicate and the idle gate."""
    import _lib_dashboard_sources as kernel

    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    bundle = _857_tick(ns, _857_AFTER).source_bundle
    settled = bundle.sources["codex"]
    claude = bundle.sources["claude"]
    identity = _857_current_identity(ns)
    assert identity is not None
    assert settled.clock_data["codex_quota_dependency"] == identity

    def with_codex(codex):
        return dataclasses.replace(bundle, sources={
            "claude": claude, "codex": codex,
            "all": kernel.compose_all_state(claude, codex),
        })

    def can_idle(codex, **overrides):
        kwargs = {"codex_dependency": identity, "now_utc": _857_AFTER}
        kwargs.update(overrides)
        return tui._tui_source_bundle_can_idle(with_codex(codex), **kwargs)

    assert kernel.settled_codex_cycle_unavailable(settled) is True
    assert can_idle(settled) is True
    # Without the current identity, the old refusal stands.
    assert tui._tui_source_bundle_can_idle(
        bundle, codex_dependency=None) is False
    assert can_idle(settled, codex_dependency=None) is False
    assert can_idle(settled, codex_dependency=identity + ("moved",)) is False
    assert can_idle(settled, codex_ingest_degraded=True) is False
    past = dataclasses.replace(settled, clock_data={
        **settled.clock_data, "codex_next_decision_at": _857_AFTER,
    })
    assert kernel.settled_codex_cycle_unavailable(past) is True
    assert can_idle(past) is False, "an elapsed decision deadline rebuilds"
    for name in _857_REFUSED_VARIANTS:
        variant = _857_variant(settled, name)
        assert kernel.settled_codex_cycle_unavailable(variant) is False, name
        assert can_idle(variant) is False, name
    assert kernel.settled_codex_cycle_unavailable(claude) is False


# --------------------------------------------------------------------------
# #857 fix round 2 — the settled idle's provenance, ordinary reuse and the
# file-identity race.
# --------------------------------------------------------------------------


def _857_add_accounting_row(cache):
    """Accounting moves while the quota dependency does not.

    A copy of the newest Codex accounting row at a fresh offset: `MAX(id)` and
    the accounting ledger advance, so the Codex version moves and the dispatch
    key with it, but no quota row, ledger entry, attribution revision,
    certificate or physical sequence changes — the certificate stays valid and
    a rebuild still publishes the settled shape, over the new totals.
    """
    columns = [
        str(row[1]) for row in cache.execute(
            "PRAGMA table_info(codex_session_entries)")
        if str(row[1]) not in ("id", "line_offset")
    ]
    names = ", ".join(columns)
    cache.execute(
        f"INSERT INTO codex_session_entries (line_offset, {names}) "
        f"SELECT line_offset + 857000000, {names} FROM codex_session_entries "
        "ORDER BY id DESC LIMIT 1"
    )
    cache.commit()


#: Where each Codex generation shape is built. A settled cycle-unavailable
#: generation after the reset; an `ok` one before it, with every tick these
#: tests take (at most seven more) still ahead of its decision deadline, the
#: reset itself.
_857_PHASE_START = {
    "settled": _857_AFTER,
    "ok": _857_RESET - 8 * _857_TICK,
}


def _857_assert_phase(codex, phase):
    if phase == "settled":
        _857_assert_settled(codex)
    else:
        assert codex.availability == "ok", [w.code for w in codex.warnings]
        assert codex.freshness == "fresh"


@pytest.mark.parametrize("phase", ("settled", "ok"))
def test_857_a_failed_rebuild_cannot_leave_the_generation_idling(
    settled_store, monkeypatch, phase,
):
    """C1: dispatch movement guarantees an ATTEMPTED rebuild, not a successful
    one. Codex accounting moves while the quota dependency does not; the full
    path's source build then raises and retains the prior bundle, and the
    snapshot is memoized under the NEW dispatch key. The next tick sees that
    key unchanged, so the idle gate must not treat the retained generation as
    validated — neither through the settled exception nor, for an `ok`
    generation, through ordinary idle admission (K1): it has to rebuild Codex
    over the moved evidence rather than republish the old generation for the
    life of the key."""
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    start = _857_PHASE_START[phase]
    settled = _857_tick(ns, start).source_bundle.sources["codex"]
    _857_assert_phase(settled, phase)
    _857_add_accounting_row(handle["cache"])

    original = tui._tui_build_source_bundle
    remaining = {"failures": 1}

    def fail_once(**kwargs):
        if remaining["failures"]:
            remaining["failures"] -= 1
            raise RuntimeError("857 transient source build failure")
        return original(**kwargs)

    monkeypatch.setattr(tui, "_tui_build_source_bundle", fail_once)
    failed = _857_tick(ns, start + _857_TICK)
    assert remaining["failures"] == 0, (
        "precondition: the moved accounting took the full path, which "
        "attempted the rebuild")
    assert (failed.source_bundle.sources["codex"].data_version
            == settled.data_version), (
        "precondition: the failed build retained the prior generation")

    calls = _857_spy(monkeypatch, tui)
    recovered = _857_tick(ns, start + 2 * _857_TICK)
    codex = recovered.source_bundle.sources["codex"]

    assert calls["forecast"] == 0, (
        "precondition: the key the failure memoized is unchanged, so the idle "
        "gate is what decided")
    assert (calls["capture"], calls["build"]) == (1, 1), (
        "the tick after a failed rebuild idled on the retained generation")
    assert codex.data_version != settled.data_version
    _857_assert_phase(codex, phase)
    # Recovered and re-validated: the idle resumes, and the retry was ONE
    # rebuild, not one per tick.
    _857_tick(ns, start + 3 * _857_TICK)
    _857_tick(ns, start + 4 * _857_TICK)
    assert (calls["capture"], calls["build"]) == (1, 1)


def _857_rewrite_accounting_in_place(cache):
    """Accounting moves with no id, counter or quota input moving.

    An in-place UPDATE of an existing accounting row — the shape
    `apply_codex_window_spend_adoption` writes when it stamps `account_key`.
    The accounting-ledger trigger advances `codex_accounting_mutation_seq`, so
    the dispatch key moves and the process accounting cache is pending; but
    `MAX(id)`, `codex_physical_mutation_seq` and every quota-dependency leg
    stay put, so the Codex version and the identity are exactly those of the
    generation built before the write.
    """
    before = cache.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='codex_accounting_mutation_seq'").fetchone()
    cache.execute(
        "UPDATE codex_session_entries "
        "SET output_tokens = output_tokens + 857000 "
        "WHERE id = (SELECT MAX(id) FROM codex_session_entries)")
    cache.commit()
    after = cache.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='codex_accounting_mutation_seq'").fetchone()
    assert before != after, "precondition: the accounting ledger advanced"


@pytest.mark.parametrize("phase", ("settled", "ok"))
def test_857_a_generation_race_cannot_leave_the_prior_accounting_current(
    settled_store, monkeypatch, phase,
):
    """P1: a source build that loses the stats generation race returns the
    PRIOR bundle — but by then its Codex build has consumed the pending
    accounting (the process accounting cache is published during the build,
    before the race check). Accounting that moved in place (window-spend
    adoption) moves neither the Codex version nor the quota-dependency
    identity, so the next tick, which the stats movement puts on the full
    path, sees the same version, the same identity and nothing pending: it
    would reuse, or settle, the generation built BEFORE the accounting moved,
    for as long as nothing else changes. The race return must not leave that
    generation's provenance current."""
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    start = _857_PHASE_START[phase]
    stale = _857_tick(ns, start).source_bundle.sources["codex"]
    _857_assert_phase(stale, phase)
    _857_rewrite_accounting_in_place(handle["cache"])

    # Claude-side stats move DURING the Codex build: after the build has
    # consumed the accounting, before the post-build reconciliation reads.
    moved = {"stats": False}
    real_digest = tui.claude_stats_digest
    monkeypatch.setattr(
        tui, "claude_stats_digest",
        lambda conn: real_digest(conn) + (":857-moved" if moved["stats"] else ""))
    real_build = tui.build_codex_source_state_from_capture

    def build_then_move_stats(*args, **kwargs):
        result = real_build(*args, **kwargs)
        moved["stats"] = True
        return result

    monkeypatch.setattr(
        tui, "build_codex_source_state_from_capture", build_then_move_stats)
    raced = _857_tick(ns, start + _857_TICK)
    assert moved["stats"], (
        "precondition: the moved accounting took the full path and rebuilt Codex")
    codex = raced.source_bundle.sources["codex"]
    assert codex.data_version == stale.data_version
    assert codex.data["periods"] == stale.data["periods"], (
        "precondition: the race returned the prior generation")
    monkeypatch.setattr(tui, "build_codex_source_state_from_capture", real_build)

    calls = _857_spy(monkeypatch, tui)
    current = _857_tick(ns, start + 2 * _857_TICK)
    codex = current.source_bundle.sources["codex"]
    assert (calls["capture"], calls["build"]) == (1, 1), (
        "the tick after a lost race republished the generation built before "
        "the accounting moved")
    assert codex.data["periods"] != stale.data["periods"], (
        "the rebuild must publish the moved accounting")
    _857_assert_phase(codex, phase)
    # One rebuild per race, then the idle resumes.
    _857_tick(ns, start + 3 * _857_TICK)
    _857_tick(ns, start + 4 * _857_TICK)
    assert (calls["capture"], calls["build"]) == (1, 1)


@pytest.mark.parametrize("phase", ("settled", "ok"))
def test_857_a_race_over_a_reused_codex_generation_keeps_its_provenance(
    settled_store, monkeypatch, phase,
):
    """The race return withdraws the Codex stamp only when the raced build
    scheduled the Codex pass, the one way it can consume pending accounting.
    Here Claude evidence moves and Codex evidence does not, so the raced build
    reuses the Codex generation and consumes nothing; stats then move during
    the build and it loses the race. The returned prior generation is exactly
    as current as it was, so withdrawing its stamp would only buy a Codex
    rebuild on the next tick — one per race, and races are likeliest during
    active Claude use. A race whose build DID schedule the Codex pass still
    withdraws (`test_857_a_generation_race_cannot_leave_the_prior_accounting_current`,
    `test_857_a_race_retained_bundle_is_never_restamped_and_loses_its_provenance`)."""
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    cache = handle["cache"]
    start = _857_PHASE_START[phase]
    first = _857_tick(ns, start).source_bundle
    built = first.sources["codex"]
    _857_assert_phase(built, phase)
    stamped = built.clock_data["codex_quota_dependency"]
    assert stamped is not None and stamped == _857_current_identity(ns)

    # Claude evidence moves, so the next tick takes the full path.
    cache.execute(
        "INSERT OR REPLACE INTO cache_meta(key, value) "
        "VALUES ('session_entries_mutation_seq', '1')"
    )
    cache.commit()
    # Claude-side stats move DURING that build, right after its Codex reuse
    # decision and before the post-build reconciliation reads.
    moved = {"stats": False}
    real_digest = tui.claude_stats_digest
    monkeypatch.setattr(
        tui, "claude_stats_digest",
        lambda conn: real_digest(conn) + (":857-moved" if moved["stats"] else ""))
    real_regime = tui._tui_note_codex_regime

    def regime_then_move_stats(value):
        real_regime(value)
        moved["stats"] = True

    monkeypatch.setattr(tui, "_tui_note_codex_regime", regime_then_move_stats)
    calls = _857_spy(monkeypatch, tui)
    raced = _857_tick(ns, start + _857_TICK)
    assert moved["stats"] and calls["source_bundle"] == 1, (
        "precondition: the Claude advance took the full path")
    # A build that won would publish a REBUILT Claude generation, because its
    # `entry_mutation_seq` moved; only the race return publishes tick 1's.
    assert raced.source_bundle.sources["claude"] is first.sources["claude"], (
        "precondition: the build lost the generation race")
    assert calls["regimes"] == ["idle"] and (
        calls["capture"], calls["build"]) == (0, 0), (
        "precondition: the raced build reused Codex without running its pass")
    codex = raced.source_bundle.sources["codex"]
    assert codex.data_version == built.data_version
    assert codex.data["periods"] is built.data["periods"], (
        "precondition: the race returned the prior generation")
    assert codex.clock_data["codex_quota_dependency"] == stamped, (
        "a race over a reused Codex generation withdrew its stamp")

    # The moved stats move the next dispatch key, so the next tick takes the
    # full path again — and must reuse Codex rather than rebuild it.
    after = _857_tick(ns, start + 2 * _857_TICK)
    assert calls["source_bundle"] == 2, "precondition: the full path ran again"
    assert (calls["capture"], calls["build"]) == (0, 0), (
        "the tick after a race over a reused Codex generation rebuilt Codex")
    codex = after.source_bundle.sources["codex"]
    _857_assert_phase(codex, phase)
    assert codex.data_version == built.data_version
    assert codex.clock_data["codex_quota_dependency"] == stamped


def _857_fail_after_the_codex_decision(monkeypatch, tui, exc):
    """Fail the next source build once, on its first stats read after the
    Codex reuse-or-rebuild decision (the post-build reconciliation) — so a
    Codex pass it scheduled has completed, and published the process
    accounting cache, by then."""
    armed = {"decided": False, "failures": 1}
    real_regime = tui._tui_note_codex_regime
    real_digest = tui.codex_stats_digest

    def regime_then_arm(value):
        real_regime(value)
        armed["decided"] = True

    def fail_once_decided(conn):
        if armed["decided"] and armed["failures"]:
            armed["failures"] -= 1
            raise exc
        return real_digest(conn)

    monkeypatch.setattr(tui, "_tui_note_codex_regime", regime_then_arm)
    monkeypatch.setattr(tui, "codex_stats_digest", fail_once_decided)
    return armed


def _857_fail_the_next_idle_adapter(monkeypatch, tui, exc):
    """Send the next idle tick to the bounded source adapter, and fail that
    adapter once, on its first stats read after the Codex reuse-or-rebuild
    decision (the post-build reconciliation) — so a Codex pass it scheduled
    has completed by then. The idle gate's one refusal stands in for any
    refusal that leaves Codex reusable, a failed Claude aggregate fold among
    them."""
    armed = _857_fail_after_the_codex_decision(monkeypatch, tui, exc)
    armed["refuse"] = True
    real_can_idle = tui._tui_source_bundle_can_idle

    def refuse_once(*args, **kwargs):
        if armed["refuse"]:
            armed["refuse"] = False
            return False
        return real_can_idle(*args, **kwargs)

    monkeypatch.setattr(tui, "_tui_source_bundle_can_idle", refuse_once)
    return armed


def _857_adapter_failure(tui, failure):
    """One exception per idle-adapter handler: the quota-projection refusal
    has its own, ahead of the generic one a transient SQLite error reaches."""
    if failure == "quota_projection":
        return tui.QuotaProjectionIncomplete("857 quota projection incomplete")
    return sqlite3.OperationalError("database is locked")


@pytest.mark.parametrize("failure", ("sqlite", "quota_projection"))
@pytest.mark.parametrize("phase", ("settled", "ok"))
def test_857_a_failed_idle_adapter_cannot_leave_consumed_accounting_current(
    settled_store, monkeypatch, phase, failure,
):
    """The idle path's bounded source adapter reads the cache on its OWN
    snapshot, opened after the dispatch read that chose the idle path. An
    in-place accounting write committing between the two leaves this tick's
    key unchanged, but the adapter sees the accounting pending and runs the
    Codex pass, which publishes the process accounting cache. A later read in
    that adapter then fails, and the tick republishes the prior bundle. The
    next key does move (the accounting ledger is in it), but by then nothing
    reads as pending, and neither the Codex version nor the quota-dependency
    identity moved, so that full tick would reuse, or settle, the generation
    built BEFORE the write. A failed adapter that scheduled the Codex pass
    must not leave that generation's provenance current."""
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    start = _857_PHASE_START[phase]
    stale = _857_tick(ns, start).source_bundle.sources["codex"]
    _857_assert_phase(stale, phase)

    armed = _857_fail_the_next_idle_adapter(
        monkeypatch, tui, _857_adapter_failure(tui, failure))
    real_bundle = tui._tui_build_source_bundle
    committed = {"write": False}

    def commit_then_adapt(**kwargs):
        # Another writer commits after this tick's dispatch read and before
        # the adapter opens its own cache snapshot.
        if not committed["write"]:
            committed["write"] = True
            _857_rewrite_accounting_in_place(handle["cache"])
        return real_bundle(**kwargs)

    monkeypatch.setattr(tui, "_tui_build_source_bundle", commit_then_adapt)
    calls = _857_spy(monkeypatch, tui)
    failed = _857_tick(ns, start + _857_TICK)
    assert calls["forecast"] == 0 and calls["source_bundle"] == 1, (
        "precondition: the unchanged key took the idle path's bounded adapter")
    assert calls["regimes"] == ["active"] and (
        calls["capture"], calls["build"]) == (1, 1), (
        "precondition: the adapter saw the accounting pending and ran the "
        "Codex pass")
    assert armed["failures"] == 0, (
        "precondition: the adapter failed after its Codex pass")
    codex = failed.source_bundle.sources["codex"]
    assert codex.data_version == stale.data_version
    assert codex.data["periods"] == stale.data["periods"], (
        "precondition: the failed adapter retained the prior generation")

    calls = _857_spy(monkeypatch, tui)
    current = _857_tick(ns, start + 2 * _857_TICK)
    assert calls["forecast"], (
        "precondition: the committed accounting moved the next key")
    codex = current.source_bundle.sources["codex"]
    assert (calls["capture"], calls["build"]) == (1, 1), (
        "the tick after a failed idle adapter republished the generation "
        "built before the accounting moved")
    assert codex.data["periods"] != stale.data["periods"], (
        "the rebuild must publish the moved accounting")
    _857_assert_phase(codex, phase)
    # One rebuild per failure, then the idle resumes.
    _857_tick(ns, start + 3 * _857_TICK)
    _857_tick(ns, start + 4 * _857_TICK)
    assert (calls["capture"], calls["build"]) == (1, 1)


@pytest.mark.parametrize("failure", ("sqlite", "quota_projection"))
@pytest.mark.parametrize("phase", ("settled", "ok"))
def test_857_a_failed_idle_adapter_that_reused_codex_keeps_its_provenance(
    settled_store, monkeypatch, phase, failure,
):
    """The failed idle adapter withdraws the Codex stamp only when it
    scheduled the Codex pass. Here nothing moved, so it reused Codex and
    consumed nothing; the retained generation is exactly as current as
    before, and withdrawing its stamp would buy a Codex rebuild on the next
    tick — one per failed adapter tick for as long as a failure elsewhere in
    the adapter persists. Both handlers: the quota-projection refusal has its
    own, ahead of the generic one."""
    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    start = _857_PHASE_START[phase]
    built = _857_tick(ns, start).source_bundle.sources["codex"]
    _857_assert_phase(built, phase)
    stamped = built.clock_data["codex_quota_dependency"]
    assert stamped is not None and stamped == _857_current_identity(ns)

    armed = _857_fail_the_next_idle_adapter(
        monkeypatch, tui, _857_adapter_failure(tui, failure))
    calls = _857_spy(monkeypatch, tui)
    failed = _857_tick(ns, start + _857_TICK)
    assert calls["forecast"] == 0 and calls["source_bundle"] == 1, (
        "precondition: the unchanged key took the idle path's bounded adapter")
    assert calls["regimes"] == ["idle"] and (
        calls["capture"], calls["build"]) == (0, 0), (
        "precondition: the adapter reused Codex without running its pass")
    assert armed["failures"] == 0, "precondition: the adapter failed"
    legs = {entry.leg for entry in failed.sync_failures}
    assert ("quota-projection" in legs) == (failure == "quota_projection"), (
        "precondition: the failure reached the handler its case names")
    codex = failed.source_bundle.sources["codex"]
    assert codex.data_version == built.data_version
    assert codex.clock_data["codex_quota_dependency"] == stamped, (
        "a failed idle adapter that reused Codex withdrew its stamp")

    # Nothing moved, so the key is unchanged and the real idle gate decides.
    _857_tick(ns, start + 2 * _857_TICK)
    assert calls["source_bundle"] == 1 and (
        calls["capture"], calls["build"]) == (0, 0), (
        "the tick after a failed idle adapter that reused Codex rebuilt Codex")


# --------------------------------------------------------------------------
# #857 — the accounting provenance token closes UNPUBLISHED CONSUMPTION as a
# class. A build that consumed pending accounting (its Codex pass published the
# process accounting cache) and then failed to publish what it built leaves
# every retained generation older than the consumed population. The tests
# below drive the two failure sites the per-site withdrawals never reached;
# the token, not a withdrawal, is what refuses the retained generation there.
# --------------------------------------------------------------------------


def _857_escalate_as_stats_corruption(monkeypatch, tui):
    """An exception the catch site attributes to a corrupt stats index, and a
    heal that succeeds. `_tui_capture_sync_failure` then raises
    `_StatsSnapshotCorruption` out of the handler — before the handler's own
    withdrawal runs — and the build boundary retries the whole tick once from
    the unchanged dispatch memo."""
    fault = sqlite3.DatabaseError("857 stats index corruption")
    real_attribute = tui._tui_attribute_corruption
    heals = []

    def attribute(conn, exc, *, database):
        if exc is fault:
            return "stats", True
        return real_attribute(conn, exc, database=database)

    def heal(exc):
        heals.append(exc)
        return True

    monkeypatch.setattr(tui, "_tui_attribute_corruption", attribute)
    monkeypatch.setattr(tui, "_tui_heal_post_query_stats", heal)
    return fault, heals


@pytest.mark.parametrize("site", ("idle_adapter", "full_path"))
@pytest.mark.parametrize("phase", ("settled", "ok"))
def test_857_a_corruption_escalation_cannot_leave_consumed_accounting_current(
    settled_store, monkeypatch, phase, site,
):
    """B1: a build consumes an in-place accounting write — on the idle path's
    bounded adapter, the write committing between the dispatch read and the
    adapter's own snapshot, or on the full path, whose key the write moved —
    and a later read then fails with an error attributed to stats corruption.
    `_tui_capture_sync_failure` escalates it before any withdrawal, and the
    heal's retry starts from the dispatch memo the failed attempt never
    touched. The retry takes the full path over a moved key, where nothing
    reads as pending any more and neither the Codex version nor the
    quota-dependency identity moved: it must rebuild Codex and publish the
    consumed accounting, not reuse, or settle, the generation built before."""
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    start = _857_PHASE_START[phase]
    stale = _857_tick(ns, start).source_bundle.sources["codex"]
    _857_assert_phase(stale, phase)

    fault, heals = _857_escalate_as_stats_corruption(monkeypatch, tui)
    if site == "idle_adapter":
        armed = _857_fail_the_next_idle_adapter(monkeypatch, tui, fault)
        real_bundle = tui._tui_build_source_bundle
        committed = {"write": False}

        def commit_then_adapt(**kwargs):
            if not committed["write"]:
                committed["write"] = True
                _857_rewrite_accounting_in_place(handle["cache"])
            return real_bundle(**kwargs)

        monkeypatch.setattr(tui, "_tui_build_source_bundle", commit_then_adapt)
    else:
        _857_rewrite_accounting_in_place(handle["cache"])
        armed = _857_fail_after_the_codex_decision(monkeypatch, tui, fault)
    calls = _857_spy(monkeypatch, tui)
    healed = _857_tick(ns, start + _857_TICK)

    assert heals == [fault] and armed["failures"] == 0, (
        "precondition: the failure after the Codex pass escalated to the heal")
    assert calls["regimes"][:1] == ["active"], (
        "precondition: the failed attempt ran the Codex pass")
    assert calls["source_bundle"] == 2, (
        "precondition: the heal retried the source build once")
    codex = healed.source_bundle.sources["codex"]
    assert (calls["capture"], calls["build"]) == (2, 2), (
        "the heal's retry republished the generation built before the "
        "consumed accounting")
    assert codex.data["periods"] != stale.data["periods"], (
        "the retry must publish the consumed accounting")
    _857_assert_phase(codex, phase)
    # One rebuild per failure, then the idle resumes.
    _857_tick(ns, start + 2 * _857_TICK)
    _857_tick(ns, start + 3 * _857_TICK)
    assert (calls["capture"], calls["build"]) == (2, 2)


@pytest.mark.parametrize("phase", ("settled", "ok"))
def test_857_a_keyless_failed_build_cannot_leave_consumed_accounting_current(
    settled_store, monkeypatch, phase,
):
    """B2: a transient dispatch-signature failure leaves the tick with no
    dispatch key. Its source build still runs, consumes the in-place
    accounting write, and fails in reconciliation, so the tick retains the
    prior bundle untouched and memoizes nothing — the dispatch memo keeps the
    older stamped snapshot. When the signature reads recover, the moved
    accounting ledger sends the next tick down the full path, where nothing
    reads as pending and the version and identity are unchanged: it must
    rebuild Codex rather than reuse, or settle, the older generation."""
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    start = _857_PHASE_START[phase]
    stale = _857_tick(ns, start).source_bundle.sources["codex"]
    _857_assert_phase(stale, phase)
    _857_rewrite_accounting_in_place(handle["cache"])

    real_signature = tui._tui_compute_dispatch_signature
    signature = {"failures": 1}

    def fail_signature_once(*args, **kwargs):
        if signature["failures"]:
            signature["failures"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real_signature(*args, **kwargs)

    monkeypatch.setattr(
        tui, "_tui_compute_dispatch_signature", fail_signature_once)
    armed = _857_fail_after_the_codex_decision(
        monkeypatch, tui, sqlite3.OperationalError("database is locked"))
    calls = _857_spy(monkeypatch, tui)
    failed = _857_tick(ns, start + _857_TICK)
    assert signature["failures"] == 0 and armed["failures"] == 0, (
        "precondition: the signature and then the build failed")
    assert calls["regimes"] == ["active"] and (
        calls["capture"], calls["build"]) == (1, 1), (
        "precondition: the keyless build ran the Codex pass")
    assert (failed.source_bundle.sources["codex"].data["periods"]
            == stale.data["periods"]), (
        "precondition: the failed build retained the prior generation")

    current = _857_tick(ns, start + 2 * _857_TICK)
    codex = current.source_bundle.sources["codex"]
    assert (calls["capture"], calls["build"]) == (2, 2), (
        "the tick after a keyless failed build republished the generation "
        "built before the consumed accounting")
    assert codex.data["periods"] != stale.data["periods"], (
        "the rebuild must publish the consumed accounting")
    _857_assert_phase(codex, phase)
    _857_tick(ns, start + 3 * _857_TICK)
    _857_tick(ns, start + 4 * _857_TICK)
    assert (calls["capture"], calls["build"]) == (2, 2)


@pytest.mark.parametrize("phase", ("settled", "ok"))
def test_857_memory_eviction_of_the_accounting_cache_never_forces_a_codex_rebuild(
    settled_store, monkeypatch, phase,
):
    """Snapshot memory enforcement discards the whole accelerator estate —
    the process accounting cache with it — after every publisher build on a
    store over the cap. The consumed provenance a retained Codex generation is
    compared against lives OUTSIDE that estate, so an eviction alone must
    never refuse the generation: comparing against the evictable state would
    rebuild Codex on every tick of an unchanged, oversized store, which is the
    idle CPU #857 exists to remove."""
    import _lib_snapshot_cache as sc

    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    start = _857_PHASE_START[phase]
    built = _857_tick(ns, start).source_bundle.sources["codex"]
    _857_assert_phase(built, phase)
    stamped = built.clock_data[sc.CODEX_ACCOUNTING_PROVENANCE_KEY]
    assert stamped is not None
    assert stamped == sc.codex_accounting_consumed_provenance()

    # Every build is over the cap: the publisher's own admission call evicts.
    monkeypatch.setattr(sc, "_SNAPSHOT_ACCELERATOR_MAX_ENTRIES", 0)
    sc.enforce_snapshot_accelerator_bounds(data_version="dashboard-publisher")
    assert sc.checkpoint_codex_accounting_cache_state() == {}, (
        "precondition: enforcement evicted the accounting cache")

    calls = _857_spy(monkeypatch, tui)
    for step in range(1, 6):
        snapshot = _857_tick(ns, start + _857_TICK * step)
        sc.enforce_snapshot_accelerator_bounds(
            data_version="dashboard-publisher")
        codex = snapshot.source_bundle.sources["codex"]
        _857_assert_phase(codex, phase)
        assert codex.data_version == built.data_version
    assert calls["forecast"] == 0, "precondition: every tick took the idle dispatch"
    assert (calls["source_bundle"], calls["capture"], calls["build"]) == (
        0, 0, 0), "an eviction alone forced a Codex rebuild"
    assert sc.codex_accounting_consumed_provenance() == stamped


_857_OK_INVALIDATIONS = tuple(
    entry for entry in _857_INVALIDATIONS if entry[0] != "registry_changed"
)


@pytest.mark.parametrize(
    "mutate",
    [mutate for _id, mutate, _builds in _857_OK_INVALIDATIONS],
    ids=[entry[0] for entry in _857_OK_INVALIDATIONS],
)
def test_857_a_moved_codex_quota_input_refuses_ordinary_reuse(
    settled_store, monkeypatch, mutate,
):
    """C2: the quota-dependency identity moves the dispatch key, but the
    ordinary exact-version reuse of an ``ok`` generation must not answer for
    it: the version string carries none of these inputs, so reuse would hand
    back the obsolete verdict and every later idle tick would retain it."""
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    live = _857_tick(ns, _857_RESET - 4 * _857_TICK).source_bundle.sources["codex"]
    assert live.availability == "ok"
    assert live.clock_data["codex_next_decision_at"] == _857_RESET
    calls = _857_spy(monkeypatch, tui)
    _857_tick(ns, _857_RESET - 3 * _857_TICK)
    assert (calls["capture"], calls["build"]) == (0, 0), (
        "precondition: the unchanged ok generation idles")

    handle["cache"] = mutate(ns, handle["cache"], monkeypatch)
    moved = _857_tick(ns, _857_RESET - 2 * _857_TICK)

    assert calls["source_bundle"] >= 1, "precondition: the dispatch key moved"
    assert (calls["capture"], calls["build"]) == (1, 1), (
        "exact-version reuse republished a generation whose quota inputs moved")
    codex = moved.source_bundle.sources["codex"]
    stamped = codex.clock_data["codex_quota_dependency"]
    assert stamped is not None
    assert stamped == _857_current_identity(ns)
    codes = [w.code for w in codex.warnings]
    if mutate in (_857_delete_certificate, _857_bump_attribution_revision):
        # A lost or revision-stale certificate: the obsolete coherent verdict
        # the reused generation would have kept publishing.
        assert "codex_projection_incoherent" in codes
        assert codex.availability == "partial"
    else:
        assert codex.availability == "ok", codes
    # The rebuilt generation names the moved inputs. A coherent one idles
    # again; the incoherent-projection composite is the certificate-recovery
    # state, which retries every tick by design.
    _857_tick(ns, _857_RESET - _857_TICK)
    expected = 1 if codex.availability == "ok" else 2
    assert (calls["capture"], calls["build"]) == (expected, expected)


def test_857_an_unreadable_codex_quota_identity_refuses_ordinary_reuse(
    settled_store, monkeypatch,
):
    """C2, unavailability: no identity is not an unchanged identity, on the
    active path as much as on the idle one. The first unreadable tick moves
    the key (the full path); every later one leaves it unchanged, so it is the
    IDLE gate that must refuse the `ok` generation stamped with no identity
    (K1) — admitting it would retain that generation for as long as the
    identity stays unreadable."""
    import _lib_snapshot_cache as sc

    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    live = _857_tick(ns, _857_RESET - 4 * _857_TICK).source_bundle.sources["codex"]
    assert live.availability == "ok"
    calls = _857_spy(monkeypatch, tui)
    monkeypatch.setattr(
        sc, "codex_quota_dependency_identity", lambda _conn, **_kw: None)
    unread = _857_tick(ns, _857_RESET - 3 * _857_TICK)

    assert calls["source_bundle"] >= 1, "precondition: the dispatch key moved"
    assert (calls["capture"], calls["build"]) == (1, 1)
    assert unread.source_bundle.sources["codex"].clock_data[
        "codex_quota_dependency"] is None

    forecasts = calls["forecast"]
    for step in (2, 1):
        again = _857_tick(ns, _857_RESET - step * _857_TICK)
        codex = again.source_bundle.sources["codex"]
        assert codex.availability == "ok"
        assert codex.clock_data["codex_quota_dependency"] is None
    assert calls["forecast"] == forecasts, (
        "precondition: an unchanged key took the idle dispatch")
    assert (calls["capture"], calls["build"]) == (3, 3), (
        "an ok generation stamped with no identity idled")


def test_857_a_cache_file_replaced_under_a_pinned_build_stamps_no_identity(
    settled_store, monkeypatch,
):
    """C3: `PRAGMA database_list` names a PATH, and a stat of that path need
    not describe the file the connection opened. Replace the cache file after
    the build has pinned its read of the old one: an identity stamped from the
    NEW inode and the OLD rows would compare equal to every later read of a
    replacement whose counters match, validating a generation built from a
    file that is gone. It must fail closed instead."""
    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    cache = handle["cache"]
    _857_assert_settled(
        _857_tick(ns, _857_AFTER).source_bundle.sources["codex"])
    # Accounting moves, so the next tick rebuilds Codex on a pinned read.
    _857_add_accounting_row(cache)
    path = pathlib.Path(next(
        str(row[2]) for row in cache.execute("PRAGMA database_list")
        if str(row[1]) == "main"))
    cache.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replacement = path.with_name(path.name + ".857-race")
    shutil.copyfile(path, replacement)

    original_compute = ns["compute_signature"]
    raced = {"done": False}

    def compute_then_replace(cache_conn, *args, **kwargs):
        result = original_compute(cache_conn, *args, **kwargs)
        if not raced["done"] and cache_conn.in_transaction:
            # The builder's first read has pinned the OLD file's snapshot.
            raced["done"] = True
            os.replace(replacement, path)
            for suffix in ("-wal", "-shm"):
                sidecar = path.with_name(path.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()
        return result

    monkeypatch.setitem(ns, "compute_signature", compute_then_replace)
    rebuilt = _857_tick(ns, _857_AFTER + _857_TICK)
    monkeypatch.setitem(ns, "compute_signature", original_compute)
    assert raced["done"], "precondition: the replacement raced a pinned build"
    codex = rebuilt.source_bundle.sources["codex"]
    _857_assert_settled(codex)
    assert codex.clock_data["codex_quota_dependency"] is None, (
        "the identity described the replacement, not the file the build read")

    # The next tick reads the replacement, so it must rebuild rather than
    # validate the generation built from the replaced file, and then settle.
    calls = _857_spy(monkeypatch, tui)
    current = _857_tick(ns, _857_AFTER + 2 * _857_TICK).source_bundle
    assert (calls["capture"], calls["build"]) == (1, 1)
    assert (current.sources["codex"].clock_data["codex_quota_dependency"]
            == _857_current_identity(ns))
    _857_tick(ns, _857_AFTER + 3 * _857_TICK)
    assert (calls["capture"], calls["build"]) == (1, 1)


def test_857_an_unobserved_pre_open_file_stamps_no_identity(
    settled_store, monkeypatch,
):
    """K2: the stamp is bound to the file observed BEFORE the build's handle
    opened, or it is nothing. A failed pre-open stat — a pathname absent for a
    moment during a restore, or any stat error — does not show that the open
    created the file, so a late bracket (two stats taken after the open) can
    stat a replacement twice around reads from the replaced file and mint an
    identity that the replacement then compares equal to on the next tick.
    Here the build loses its pre-open observation and the file is replaced
    after its read is pinned."""
    import _lib_snapshot_cache as sc

    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    cache = handle["cache"]
    _857_assert_settled(
        _857_tick(ns, _857_AFTER).source_bundle.sources["codex"])
    # Accounting moves, so the next tick rebuilds Codex on a pinned read.
    _857_add_accounting_row(cache)
    path = pathlib.Path(sc.codex_main_database_path(cache))
    cache.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replacement = path.with_name(path.name + ".857-unobserved")
    shutil.copyfile(path, replacement)

    real_file_identity = sc.codex_cache_file_identity

    def unobserved_by_the_build(file_path):
        observer = sys._getframe(1)
        if (
            observer.f_code.co_name == "_tui_cache_file_before_open"
            and observer.f_back is not None
            and observer.f_back.f_code.co_name == "_tui_build_source_bundle"
        ):
            return None
        return real_file_identity(file_path)

    original_compute = ns["compute_signature"]
    raced = {"done": False}

    def compute_then_replace(cache_conn, *args, **kwargs):
        result = original_compute(cache_conn, *args, **kwargs)
        if not raced["done"] and cache_conn.in_transaction:
            # The builder's first read has pinned the OLD file's snapshot.
            raced["done"] = True
            os.replace(replacement, path)
            for suffix in ("-wal", "-shm"):
                sidecar = path.with_name(path.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()
        return result

    monkeypatch.setattr(sc, "codex_cache_file_identity", unobserved_by_the_build)
    monkeypatch.setitem(ns, "compute_signature", compute_then_replace)
    rebuilt = _857_tick(ns, _857_AFTER + _857_TICK)
    monkeypatch.setitem(ns, "compute_signature", original_compute)
    monkeypatch.setattr(sc, "codex_cache_file_identity", real_file_identity)
    assert raced["done"], "precondition: the replacement raced a pinned build"
    codex = rebuilt.source_bundle.sources["codex"]
    _857_assert_settled(codex)
    assert codex.clock_data["codex_quota_dependency"] is None, (
        "an unobserved pre-open file was bound by a late bracket")

    calls = _857_spy(monkeypatch, tui)
    current = _857_tick(ns, _857_AFTER + 2 * _857_TICK).source_bundle
    assert (calls["capture"], calls["build"]) == (1, 1), (
        "the replacement validated a generation built from the replaced file")
    assert (current.sources["codex"].clock_data["codex_quota_dependency"]
            == _857_current_identity(ns))
    _857_tick(ns, _857_AFTER + 3 * _857_TICK)
    assert (calls["capture"], calls["build"]) == (1, 1)


def test_857_a_dispatch_read_with_no_pre_open_observation_has_no_identity(
    settled_store, monkeypatch,
):
    """K2 at dispatch: the dispatch-time identity is compared against the
    stamp and rides the dispatch key, so it is bound on the same terms. A
    pre-open stat that fails yields NO identity — never a late bracket over
    whatever file holds the name afterwards — while the signature is still
    read, so the tick keeps its ordinary dispatch."""
    import _lib_snapshot_cache as sc

    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    real_file_identity = sc.codex_cache_file_identity

    def unobservable(file_path):
        if sys._getframe(1).f_code.co_name == "_tui_cache_file_before_open":
            return None
        return real_file_identity(file_path)

    monkeypatch.setattr(sc, "codex_cache_file_identity", unobservable)
    stats = ns["open_db"]()
    try:
        signature, identity = tui._tui_compute_dispatch_signature(
            stats, with_codex_dependency=True)
    finally:
        stats.close()
    assert signature is not None
    assert identity is None


def test_857_a_fresh_install_idles_from_its_second_tick(tmp_path, monkeypatch):
    """The one legitimate absent-file case: on a fresh install the dispatch
    read's own open CREATES cache.db, so no pre-open observation of it can
    exist. The dispatch re-observes the file it created and binds the identity
    on a second handle; without that, its first key would carry no identity,
    the second tick's key would carry one, and every fresh install would pay a
    second full rebuild."""
    import _lib_snapshot_cache as sc

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    sc.reset_dispatch_state()
    ns["_cctally_tui"]._tui_reset_partial_retry_state()
    cache_path = pathlib.Path(sys.modules["_cctally_core"].CACHE_DB_PATH)
    assert not cache_path.exists(), "precondition: a fresh install"
    try:
        first = _857_tick(ns, _857_AFTER)
        codex = first.source_bundle.sources["codex"]
        stamped = codex.clock_data["codex_quota_dependency"]
        assert stamped is not None
        assert stamped == _857_current_identity(ns)
        calls = _857_spy(monkeypatch, ns["_cctally_tui"])
        _857_tick(ns, _857_AFTER + _857_TICK)
        assert (calls["source_bundle"], calls["forecast"]) == (0, 0), (
            "a fresh install's second tick did not idle")
    finally:
        sc.reset_dispatch_state()
        ns["_cctally_tui"]._tui_reset_partial_retry_state()


def test_857_the_settled_generation_idles_with_the_per_tick_ingest_on(
    settled_store, monkeypatch,
):
    """O1: production ingests on EVERY tick. Both provider ingests run, find
    nothing new, and must leave the settled generation idling: no source
    bundle, capture or build, the clock still running, the identity stable."""
    from _lib_source_retry import PARTIAL_RETRY_INTERVAL

    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    first = _857_tick(ns, _857_AFTER, skip_sync=False)
    settled = first.source_bundle.sources["codex"]
    _857_assert_settled(settled)
    identity = settled.clock_data["codex_quota_dependency"]
    assert identity is not None

    ingests = {"claude": 0, "codex": 0}
    for name, key in (("sync_cache", "claude"), ("sync_codex_cache", "codex")):
        real = ns[name]

        def counted(*args, _real=real, _key=key, **kwargs):
            ingests[_key] += 1
            return _real(*args, **kwargs)

        monkeypatch.setitem(ns, name, counted)
    calls = _857_spy(monkeypatch, tui)
    steps = int(PARTIAL_RETRY_INTERVAL / _857_TICK) + 4
    for step in range(1, steps + 1):
        snapshot = _857_tick(ns, _857_AFTER + _857_TICK * step, skip_sync=False)
        codex = snapshot.source_bundle.sources["codex"]
        _857_assert_settled(codex)
        assert codex.data_version == settled.data_version
        assert snapshot.last_sync_error is None

    assert ingests == {"claude": steps, "codex": steps}, (
        "non-vacuity: both ingests must have run on every tick")
    assert calls["forecast"] == 0, "every tick took the idle dispatch"
    assert (calls["source_bundle"], calls["capture"], calls["build"]) == (
        0, 0, 0), (
        "the per-tick ingest took the settled generation off the idle path")
    assert calls["clock"] >= steps
    assert _857_current_identity(ns) == identity


def test_857_a_stats_rebuild_never_publishes_a_generation_a_cold_build_disagrees_with(
    settled_store, monkeypatch,
):
    """O2: `db rebuild --db stats` republishes the disposable stats index from
    the journal. Whatever the ticks after it do — rebuild, refuse, or keep
    idling because the relevant rebuilt contents are identical — the Codex
    generation they publish once a tick completes cleanly must be the one a
    COLD build over the rebuilt store publishes at the same instant."""
    import importlib

    import _cctally_core
    import _lib_snapshot_cache as sc

    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    jr = importlib.import_module("_cctally_journal")
    settled = _857_tick(ns, _857_AFTER, skip_sync=False).source_bundle.sources[
        "codex"]
    _857_assert_settled(settled)
    stats_path = pathlib.Path(_cctally_core.DB_PATH)
    before = stats_path.stat()

    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))
    after = stats_path.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_size) != (
        before.st_ino, before.st_mtime_ns, before.st_size), (
            "non-vacuity: the rebuild must have written the stats file")

    calls = _857_spy(monkeypatch, tui)
    warm = None
    at = _857_AFTER
    for step in range(1, 7):
        at = _857_AFTER + _857_TICK * step
        snapshot = _857_tick(ns, at, skip_sync=False)
        if snapshot.last_sync_error is None:
            warm = snapshot.source_bundle.sources["codex"]
            break
    assert warm is not None, "the dashboard never recovered from the rebuild"
    # This rebuild republishes identical RELEVANT contents: every digest the
    # dispatch key carries re-derives to the same value over the new file, so
    # the settled generation keeps idling — which is only correct because the
    # cold build below agrees with it.
    assert (calls["source_bundle"], calls["capture"], calls["build"]) == (
        0, 0, 0)

    sc.reset_dispatch_state()
    tui._tui_reset_partial_retry_state()
    cold = _857_tick(ns, at).source_bundle.sources["codex"]
    assert warm.data_version == cold.data_version
    assert [w.code for w in warm.warnings] == [w.code for w in cold.warnings]
    assert warm.availability == cold.availability
    assert (warm.clock_data["codex_quota_dependency"]
            == cold.clock_data["codex_quota_dependency"])


def test_857_the_dispatch_reads_the_identity_on_its_own_handle_in_one_snapshot(
    settled_store, monkeypatch,
):
    """O3: the dispatch-time identity rides the signature's own cache handle —
    no second connection per tick — and its reads share ONE read transaction,
    so a commit landing between two legs cannot yield an identity no state
    ever had (one spurious full-path tick)."""
    ns, _handle = settled_store
    tui = ns["_cctally_tui"]
    real_open = ns["open_cache_db"]
    traces = []

    def traced_open():
        conn = real_open()
        statements = []
        conn.set_trace_callback(statements.append)
        traces.append(statements)
        return conn

    monkeypatch.setitem(ns, "open_cache_db", traced_open)
    stats = ns["open_db"]()
    try:
        _signature, identity = tui._tui_compute_dispatch_signature(
            stats, with_codex_dependency=True)
    finally:
        stats.close()

    assert identity is not None
    assert len(traces) == 1, "the identity must ride the signature's handle"
    statements = [statement.strip().upper() for statement in traces[0]]
    legs = [
        index for index, statement in enumerate(statements)
        if "QUOTA_WINDOW_CHANGE_LOG" in statement
        or "CODEX_WINDOW_ATTRIBUTION_REVISION" in statement
    ]
    assert len(legs) >= 3, statements
    signature_reads = [
        index for index, statement in enumerate(statements)
        if "CODEX_SESSION_ENTRIES" in statement
    ]
    assert signature_reads, "non-vacuity: the traced handle is the signature's own"
    # K3: the WHOLE dispatch identity — the signature's cache legs and the
    # quota-dependency legs — is read on one snapshot, so a commit between the
    # signature and the identity cannot mint a key no state ever had.
    first = min(signature_reads[0], legs[0])
    last = max(signature_reads[-1], legs[-1])
    begins = [
        i for i, st in enumerate(statements) if st == "BEGIN" and i < first]
    rollbacks = [
        i for i, st in enumerate(statements) if st == "ROLLBACK" and i > last]
    assert begins and rollbacks, (
        "the dispatch identity was read outside one read transaction")
    begin, rollback = begins[-1], rollbacks[0]
    assert not any(
        st in ("BEGIN", "ROLLBACK", "COMMIT")
        for st in statements[begin + 1:rollback]
    ), "every signature and identity leg must be read inside the SAME transaction"
    # P6: the connection's file path is resolved ONCE per identity read.
    assert sum("DATABASE_LIST" in st for st in statements) == 1, statements


def test_857_the_dispatch_signature_and_identity_share_one_snapshot(
    settled_store, monkeypatch,
):
    """K3, behaviourally: a quota commit landing between the dispatch
    signature's read and the identity's must be invisible to the identity,
    exactly as it is to the signature. Otherwise the key pairs an older
    signature with a newer identity — a key no database state ever had — and
    the next tick pays one unnecessary full rebuild."""
    import _lib_snapshot_cache as sc

    ns, handle = settled_store
    tui = ns["_cctally_tui"]
    before = _857_current_identity(ns)
    assert before is not None
    real_compute = sc.compute_signature
    committed = {"done": False}

    def compute_then_commit(cache_conn, *args, **kwargs):
        result = real_compute(cache_conn, *args, **kwargs)
        if not committed["done"]:
            committed["done"] = True
            # Another writer, on its own connection, commits a quota row.
            _857_insert_raw_quota_row(ns, handle["cache"], monkeypatch)
        return result

    monkeypatch.setattr(sc, "compute_signature", compute_then_commit)
    stats = ns["open_db"]()
    try:
        signature, identity = tui._tui_compute_dispatch_signature(
            stats, with_codex_dependency=True)
    finally:
        stats.close()
    monkeypatch.setattr(sc, "compute_signature", real_compute)

    assert committed["done"], "precondition: the commit raced the dispatch read"
    assert signature is not None
    assert identity == before, (
        "the identity saw a commit that landed after the signature's read")
    assert _857_current_identity(ns) != before, (
        "non-vacuity: the commit moved the identity")


def test_857_the_identity_is_bound_to_the_opened_file_and_joins_a_pinned_read(
    settled_store,
):
    """C3/O3/K2 at the kernel: the pre-open observation is REQUIRED, a
    pre-open observation of any OTHER file — or a failed one — yields no
    identity (there is no late-bracket fallback), and inside a caller's pinned
    read the identity joins that transaction rather than ending it (the
    build's capture must keep reading the snapshot the identity described)."""
    import _lib_snapshot_cache as sc

    _ns, handle = settled_store
    cache = handle["cache"]
    path = sc.codex_main_database_path(cache)
    opened = sc.codex_cache_file_identity(path)
    assert opened is not None
    identity = sc.codex_quota_dependency_identity(cache, opened_file=opened)
    assert identity is not None
    assert identity[1:3] == opened
    with pytest.raises(TypeError):
        sc.codex_quota_dependency_identity(cache)  # no unbound form exists
    assert sc.codex_quota_dependency_identity(
        cache, opened_file=(opened[0], opened[1] + 1)) is None
    assert sc.codex_quota_dependency_identity(cache, opened_file=None) is None
    assert not cache.in_transaction, "the identity must leave no transaction"

    cache.execute("BEGIN")
    try:
        cache.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
        assert sc.codex_quota_dependency_identity(
            cache, opened_file=opened) == identity
        assert cache.in_transaction, "the identity ended the caller's pinned read"
    finally:
        cache.rollback()
