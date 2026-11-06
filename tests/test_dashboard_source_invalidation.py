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


@pytest.fixture(autouse=True)
def _no_detached_drains(monkeypatch):
    """Patch each fresh sibling, including loads through imported aliases."""
    import conftest
    import subprocess
    import sys

    real_load = conftest.load_script_module
    real_popen = subprocess.Popen
    escaped = []

    def patch_current():
        update = sys.modules.get("_cctally_update")
        if update is not None:
            monkeypatch.setattr(update, "_spawn_detached", lambda command: True)

    def load_without_detaching(*args, **kwargs):
        module = real_load(*args, **kwargs)
        patch_current()
        return module

    def guard_detached(*popenargs, **kwargs):
        argv = popenargs[0] if popenargs else kwargs.get("args")
        if (kwargs.get("start_new_session") and isinstance(argv, (list, tuple))
                and argv and str(argv[-1]).startswith("_codex-")):
            escaped.append(tuple(argv))
            raise AssertionError("a Codex detached worker escaped test isolation")
        return real_popen(*popenargs, **kwargs)

    # Every conftest.load_script alias resolves this primitive at call time;
    # wrapping only this module's load_script name would miss helper-owned loads.
    monkeypatch.setattr(conftest, "load_script_module", load_without_detaching)
    monkeypatch.setattr(subprocess, "Popen", guard_detached)
    patch_current()
    yield
    assert not escaped, f"detached Codex worker attempts: {escaped!r}"


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
        foreground = ns["sync_codex_cache"](conn)
        assert foreground.files_pruned == 0
        assert sys.modules["_cctally_cache"].run_codex_prune_drain(
            wait_for_requests=False).identities_deleted == 1
        # One mutation for pruning A and one for ingesting B.
        assert _physical_seq(conn) == 3

        monkeypatch.setenv("CODEX_HOME", str(provider_root))
        foreground = ns["sync_codex_cache"](conn)
        assert foreground.files_pruned == 0
        assert sys.modules["_cctally_cache"].run_codex_prune_drain(
            wait_for_requests=False).identities_deleted == 1
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
        # #1050: no input identity for the prior build. Reading one first
        # would open (and so create) cache.db before this build observes it,
        # stamping the prior Codex generation with a quota-dependency identity
        # that the raced build below must then withdraw (#857) — a copy, not
        # the prior object this test retains.
        prior = tui_module._tui_build_source_bundle(
            claude_input_identity=None,
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

        # #1050: the input identity is the dispatch read, taken before the
        # inputs are assembled — before the digest starts moving.
        identity = tui_module._tui_read_claude_input_identity(stats)
        monkeypatch.setattr(tui_module, "codex_stats_digest", moving_digest)
        rebuilt = tui_module._tui_build_source_bundle(
            claude_input_identity=identity,
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
            claude_input_identity=tui_module._tui_read_claude_input_identity(stats),
            projects_envelope={},
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
        # #1050: the stats legs are the store's own; only the cache legs are
        # scripted. The pinned start read equals the input identity (the
        # dispatch read the supplied inputs were assembled under), and the
        # post-build read sees the cache move after the pin.
        real, revision = tui_module._tui_read_claude_input_identity(stats)
        pinned = real._replace(
            max_entry_id=1, max_codex_id=1, generation=0,
            entry_mutation_seq=1, codex_physical_mutation_seq=1,
        )
        signatures = iter((
            pinned,
            pinned._replace(
                max_entry_id=2, max_codex_id=2,
                entry_mutation_seq=2, codex_physical_mutation_seq=2,
            ),
        ))
        monkeypatch.setitem(
            ns, "compute_signature", lambda *args, **kwargs: next(signatures),
        )

        rebuilt = tui_module._tui_build_source_bundle(
            claude_input_identity=(pinned, revision),
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
            claude_input_identity=tui_module._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
            projects_envelope={},
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
        current = ns["_cctally_tui"]._tui_build_source_bundle(
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
            projects_envelope={},
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
        current = ns["_cctally_tui"]._tui_build_source_bundle(
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
            projects_envelope={},
            stats_conn=stats,
            now_utc=now,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=12.5,
            claude_total_tokens=120,
        )
        current = ns["_cctally_tui"]._tui_build_source_bundle(
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
                claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
                claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
        # names the file it stats, and `journal_mode`). #1050 spec 4.7 appends
        # two Codex relations (`quota_percent_milestones`,
        # `quota_window_blocks`), so 11. The bound leaves room for one more
        # fixed query without leaving room for a per-window query.
        assert len(digest_statements) <= 12, (
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
            projects_envelope={},
            stats_conn=stats, now_utc=now, display_tz_name="UTC",
            codex_ingest_contended=False, claude_cost_usd=1.0, claude_total_tokens=10,
        )
        monkeypatch.setattr(_cctally_account, "resolve_active_account_keys",
                            lambda: {_ACCT_B})
        second = ns["_cctally_tui"]._tui_build_source_bundle(
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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
            claude_input_identity=ns["_cctally_tui"]._tui_read_claude_input_identity(stats),
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


def _frozen_input_identity(ns, tui_module, stats):
    """#1050: the Claude input identity under the same pin. The dispatch read
    computes its signature through the snapshot-cache kernel, not the patched
    namespace entry, so the pinned database legs are applied to the store's own
    read here (its digests and content revision stay real, as the builder's
    are) — otherwise the builder's frozen start read differs from the identity
    and every build is rejected."""
    real, revision = tui_module._tui_read_claude_input_identity(stats)
    frozen = ns["compute_signature"]()
    pinned = real._replace(**{
        leg: getattr(frozen, leg) for leg in (
            "max_entry_id", "max_wus_id", "max_wcs_id", "reset_sig",
            "max_codex_id", "generation", "entry_mutation_seq",
            "codex_physical_mutation_seq",
        )
    })
    return pinned, revision


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
            claude_input_identity=_frozen_input_identity(
                ns, tui_module, stats),
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
            claude_input_identity=_frozen_input_identity(
                ns, tui_module, stats),
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
            claude_input_identity=_frozen_input_identity(
                ns, tui_module, stats),
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
            claude_input_identity=_frozen_input_identity(
                ns, tui_module, stats),
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
            claude_input_identity=tui._tui_read_claude_input_identity(stats),
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
        # #1050: the input identity is the dispatch read, taken before the
        # digest starts moving.
        identity = tui._tui_read_claude_input_identity(stats)
        digests = iter(("857-pre", "857-post"))
        monkeypatch.setattr(
            tui, "codex_stats_digest", lambda _conn: next(digests, "857-post"))
        raced = tui._tui_build_source_bundle(
            claude_input_identity=identity,
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
    # This rebuild republishes identical RELEVANT Codex contents: every Codex
    # digest re-derives to the same value over the new file, so the settled
    # Codex generation is reused — which is only correct because the cold
    # build below agrees with it. #1050: the rebuild rewrites the publication
    # stamp, which the Claude content revision names (spec 4.4), so the
    # source bundle rebuilds for Claude while Codex captures and builds
    # nothing.
    assert calls["source_bundle"] >= 1
    assert (calls["capture"], calls["build"]) == (0, 0)

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


# --------------------------------------------------------------------------
# #1015 spec 5.4: the calibration dependency rides the dispatch key
# --------------------------------------------------------------------------
# The forecast's calibrated fields and the rate-change array's supersession
# label depend on the calibration sidecar and on the correction lineage
# relations, none of which a dispatch-signature leg reads. Without the
# `calibration_dependency` component a refit, a recorded attempt, a completed
# correction batch or an explicit supersession left an open dashboard idling on
# the forecast it had before (A12).
_1015_EFFECTIVE = "2026-07-01T00:00:00+00:00"


def _1015_store(tmp_path, monkeypatch):
    import _lib_snapshot_cache as sc

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    sc.reset_dispatch_state()
    ns["_cctally_tui"]._tui_reset_partial_retry_state()
    stats = ns["open_db"]()
    try:
        stats.execute(
            "INSERT INTO meter_rate_change_events "
            "(provider, account_key, effective_from, previous_units_per_point,"
            " new_units_per_point, severity, detected_at_utc, created_at_utc) "
            "VALUES ('claude', 'unattributed', ?, 2.0, 1.0, 'alarm', "
            "        '2026-07-15T00:00:00+00:00', '2026-07-15T00:00:00+00:00')",
            (_1015_EFFECTIVE,))
        stats.commit()
    finally:
        stats.close()
    _1015_write_sidecar(units=1.0)
    return ns


def _1015_write_sidecar(*, units, attempt=None, in_place=False):
    """The sidecar as `cctally quota` leaves it: an atomic replacement, or (an
    attempt record) an in-place rewrite keeping the inode."""
    import _cctally_quota_model as qmg

    bucket = {"regimes": [{"unitsPerPoint": units,
                           "effectiveFrom": "2026-06-01T00:00:00+00:00"}]}
    if attempt is not None:
        bucket["recalibrationAttempt"] = {
            "status": attempt, "basis": ["batch-1"],
            "asOf": "2026-07-16T00:00:00+00:00"}
    payload = json.dumps({
        "schemaVersion": qmg.CALIBRATION_STATE_SCHEMA_VERSION,
        "accounts": {qmg.MERGED_STATE_KEY: bucket},
    }, sort_keys=True)
    path = pathlib.Path(qmg.calibration_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    if in_place and path.exists():
        path.write_text(payload, encoding="utf-8")
        return
    staged = path.with_name(path.name + ".tmp")
    staged.write_text(payload, encoding="utf-8")
    os.replace(staged, path)


def _1015_seed_correction(ns):
    stats = ns["open_db"]()
    try:
        stats.execute(
            "INSERT INTO claude_weekly_observation_index (observation_id, account_key, captured_at_utc) VALUES (?,?,?)",
            ("obs-1", "acct-a", "2026-07-10T10:00:00+00:00"))
        stats.execute(
            "INSERT INTO claude_weekly_correction_history VALUES (?,?,?)",
            ("op-1", "obs-1", "hold"))
        stats.execute(
            "INSERT INTO claude_weekly_correction_ops VALUES (?,?,?)",
            ("op-1", "observations-2026-07.jsonl", 100))
        stats.execute("INSERT INTO correction_batch_lineage VALUES (?,?,?)",
                      ("batch-1", "observations-2026-07.jsonl", 200))
        stats.execute(
            "INSERT INTO correction_batch_footprint VALUES (?,?,?)",
            ("batch-1", "claude", "acct-a"))
        stats.commit()
    finally:
        stats.close()


def _1015_seed_supersession(ns):
    stats = ns["open_db"]()
    try:
        stats.execute(
            "INSERT INTO meter_rate_change_supersessions VALUES "
            "(?,?,?,?,?,?,?)",
            ("claude", "unattributed", _1015_EFFECTIVE, "reviewed-correction",
             '[{"observations":1,"opId":"op-9"}]',
             "2026-07-16T00:00:00+00:00", "2026-07-16T00:00:00+00:00"))
        stats.commit()
    finally:
        stats.close()


def _1015_signature(ns):
    stats = ns["open_db"]()
    try:
        return ns["_cctally_tui"]._tui_compute_dispatch_signature(stats)
    finally:
        stats.close()


_1015_INVALIDATIONS = (
    # (id, mutate, every dispatch-signature leg unchanged, expected label)
    ("sidecar-refit",
     lambda ns: _1015_write_sidecar(units=1.4), True, None),
    ("attempt-status",
     lambda ns: _1015_write_sidecar(
         units=1.0, attempt="insufficient-history", in_place=True), True, None),
    ("completed-correction-batch", _1015_seed_correction, True, None),
    ("supersession-row", _1015_seed_supersession, False,
     "2026-07-16T00:00:00+00:00"),
)


@pytest.mark.parametrize(
    "case_id,mutate,signature_unchanged,superseded_at",
    _1015_INVALIDATIONS, ids=[case[0] for case in _1015_INVALIDATIONS])
def test_1015_a_calibration_dependency_change_leaves_the_idle_path(
    tmp_path, monkeypatch, case_id, mutate, signature_unchanged, superseded_at,
):
    """Detects an open dashboard that keeps a forecast calibrated from a fit a
    correction tainted (or keeps refusing after the refit), and a rate-change
    row that never gains its "superseded by a correction" label."""
    import _lib_snapshot_cache as sc

    ns = _1015_store(tmp_path, monkeypatch)
    tui = ns["_cctally_tui"]
    try:
        first = _857_tick(ns, _857_AFTER)
        assert [row.get("supersession") for row in first.meter_rate_changes] == [
            None], "precondition: the published row is not superseded"
        calls = _857_spy(monkeypatch, tui)
        _857_tick(ns, _857_AFTER + _857_TICK)
        assert calls["forecast"] == 0, "precondition: an unchanged tick idles"

        before = _1015_signature(ns)
        mutate(ns)
        if signature_unchanged:
            assert _1015_signature(ns) == before, (
                "non-vacuity: no dispatch-signature leg may see this change")
        rebuilt = _857_tick(ns, _857_AFTER + _857_TICK * 2)
        assert calls["forecast"] == 1, (
            f"{case_id}: the dashboard kept the idle forecast")
        if case_id == "completed-correction-batch":
            assert rebuilt.meter_rate_changes[0].get("supersession") == {
                "reason": "reviewed-correction", "superseded_at": None,
                "corrections": ["op-1"]}
        elif superseded_at is not None:
            assert (rebuilt.meter_rate_changes[0].get("supersession")
                    or {}).get("superseded_at") == superseded_at
        _857_tick(ns, _857_AFTER + _857_TICK * 3)
        assert calls["forecast"] == 1, "the new key must settle and idle"
    finally:
        sc.reset_dispatch_state()
        tui._tui_reset_partial_retry_state()


_1015_LINEAGE_TABLES = (
    "CORRECTION_BATCH_LINEAGE",
    "CORRECTION_BATCH_FOOTPRINT",
    "CLAUDE_WEEKLY_CORRECTION_HISTORY",
    "CLAUDE_WEEKLY_CORRECTION_OPS",
    "CLAUDE_WEEKLY_OBSERVATION_INDEX",
    "METER_RATE_CHANGE_SUPERSESSIONS",
)


def test_1015_an_unchanged_settled_tick_scans_no_lineage_relation(
    tmp_path, monkeypatch,
):
    """Detects an idle dashboard that pays a correction or supersession scan
    (or a second stats connection) on every tick: an unchanged, settled stats
    file serves both digests from the memo, and the sidecar leg is a stat."""
    import importlib
    import _lib_snapshot_cache as sc

    kernel = importlib.import_module("_lib_dashboard_sources")
    monkeypatch.setattr(kernel, "_STATS_RELATIONS_DIGEST_MEMO", {})
    monkeypatch.setattr(
        kernel, "_stats_memo_now", lambda: time.time() + 3600.0)
    ns = _1015_store(tmp_path, monkeypatch)
    _1015_seed_correction(ns)
    _1015_seed_supersession(ns)
    tui = ns["_cctally_tui"]
    store = sys.modules["_cctally_store"]
    real_open = tui.open_db
    real_failfast = store.stats_open_readonly_failfast
    opened = {"stats": 0, "failfast": 0}
    statements: list = []

    def traced_open(*args, **kwargs):
        opened["stats"] += 1
        conn = real_open(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    def counted_failfast(*args, **kwargs):
        opened["failfast"] += 1
        return real_failfast(*args, **kwargs)

    def lineage_reads():
        return [s for s in statements
                if any(table in s.upper() for table in _1015_LINEAGE_TABLES)]

    try:
        _857_tick(ns, _857_AFTER)
        _857_tick(ns, _857_AFTER + _857_TICK)
        calls = _857_spy(monkeypatch, tui)
        monkeypatch.setattr(tui, "open_db", traced_open)
        monkeypatch.setattr(store, "stats_open_readonly_failfast",
                            counted_failfast)
        _857_tick(ns, _857_AFTER + _857_TICK * 2)
        assert calls["forecast"] == 0, "precondition: the tick idled"
        assert statements, "non-vacuity: the trace saw the tick's reads"
        assert lineage_reads() == [], lineage_reads()
        assert opened == {"stats": 1, "failfast": 0}, opened

        # Non-vacuity: a cold memo reads the relations on the same handle.
        monkeypatch.setattr(kernel, "_STATS_RELATIONS_DIGEST_MEMO", {})
        statements.clear()
        _857_tick(ns, _857_AFTER + _857_TICK * 3)
        assert calls["forecast"] == 0, "a cold memo is not a dispatch change"
        assert lineage_reads(), "the trace must see the cold digest reads"
    finally:
        sc.reset_dispatch_state()
        tui._tui_reset_partial_retry_state()


# ── #836 spec 5.7: the two dependencies the idle path could not see ───────
#
# Every case runs real `_tui_build_snapshot` ticks with the dispatch memo (the
# prior snapshot and its source bundle) threaded through, and asserts that the
# second tick is NOT idle, that the Claude source object is a new one, and
# that the published value changed. Detects: an idle tick or an exact-version
# Claude source reuse republishing a value the rejection state or a matured
# sample had already superseded.

_836_T = dt.datetime(2026, 4, 30, 11, 30, tzinfo=dt.timezone.utc)
_836_KEY = 1777595400


@pytest.fixture
def ns836(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _lib_snapshot_cache as sc
    sc.reset_dispatch_state()
    tui = ns["_cctally_tui"]
    calls = {"rebuild": 0}
    original = tui._tui_build_forecast_view

    def counted(*args, **kwargs):
        calls["rebuild"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tui, "_tui_build_forecast_view", counted)
    ns["_836_calls"] = calls
    yield ns
    sc.reset_dispatch_state()
    tui._tui_reset_partial_retry_state()


def _836_tick(ns, now):
    """One real dashboard tick; returns (snapshot, envelope, was_rebuild)."""
    before = ns["_836_calls"]["rebuild"]
    snap = ns["_tui_build_snapshot"](
        now_utc=now, skip_sync=True, precompute_envelope=True,
        runtime_bind="127.0.0.1", display_tz_pref_override="utc")
    envelope = ns["snapshot_to_envelope"](snap, now_utc=now)
    return snap, envelope, ns["_836_calls"]["rebuild"] > before


def _836_claude(snap):
    return snap.source_bundle.sources["claude"]


def _836_accounts(conn):
    conn.executemany(
        "INSERT INTO accounts (account_key, provider, natural_id) "
        "VALUES (?, 'claude', ?)", [("alice", "alice"), ("zbob", "zbob")])
    conn.commit()


def _836_snapshot(conn, account, pct, captured, *, held=0,
                  week=("2026-04-27", "2026-05-04"),
                  five_hour_percent=12.0, payload="{}"):
    start, end = week
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(week_start_date, week_end_date, week_start_at, week_end_at, "
        " captured_at_utc, weekly_percent, five_hour_percent, "
        " five_hour_window_key, five_hour_resets_at, weekly_observation_held, "
        " account_key, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-04-30T15:30:00+00:00', ?, ?, ?)",
        (start, end, f"{start}T00:00:00+00:00", f"{end}T00:00:00+00:00",
         captured, pct, five_hour_percent, _836_KEY, held, account, payload))
    conn.commit()
    return int(cur.lastrowid)


def _836_weekly_milestone(conn, account, threshold, captured, *,
                          reset_event_id=0):
    conn.execute(
        "INSERT INTO percent_milestones "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, percent_threshold, cumulative_cost_usd, "
        " marginal_cost_usd, usage_snapshot_id, cost_snapshot_id, "
        " reset_event_id, account_key) "
        "VALUES (?, '2026-04-27', '2026-05-04', '2026-04-27T00:00:00+00:00', "
        " '2026-05-04T00:00:00+00:00', ?, ?, 0.5, 0, 0, ?, ?)",
        (captured, threshold, float(threshold), reset_event_id, account))
    conn.commit()


def _836_assert_rebuilt_and_republished(first, second, rebuilt):
    assert rebuilt, "the second tick idled on the prior snapshot"
    assert _836_claude(second) is not _836_claude(first), (
        "the Claude source object was reused from the prior bundle")


def test_836_a_newer_sample_of_another_account_flips_the_displayed_account(
        ns836):
    """(a) The displayed-account flip from a newer sample."""
    conn = ns836["open_db"]()
    try:
        _836_accounts(conn)
        _836_snapshot(conn, "alice", 3.0, "2026-04-30T11:00:00Z")
        first, env1, _ = _836_tick(ns836, _836_T - dt.timedelta(minutes=15))
        assert env1["current_week"]["account_key"] == "alice"
        _836_snapshot(conn, "zbob", 40.0, "2026-04-30T11:20:00Z")
    finally:
        conn.close()
    second, env2, rebuilt = _836_tick(ns836, _836_T)
    _836_assert_rebuilt_and_republished(first, second, rebuilt)
    assert env2["current_week"]["account_key"] == "zbob"
    assert _836_claude(second).data["hero"]["current_week"]["used_pct"] == 40.0


def _836_seed_live_crossing(conn):
    """An undecorated store with a live block and a five-hour crossing whose
    snapshot (10:20, weekly 43) is the card's newest weekly sample."""
    conn.execute(
        "INSERT INTO five_hour_blocks "
        "(five_hour_window_key, five_hour_resets_at, block_start_at, "
        " first_observed_at_utc, last_observed_at_utc, final_five_hour_percent, "
        " seven_day_pct_at_block_start, crossed_seven_day_reset, is_closed, "
        " created_at_utc, last_updated_at_utc, account_key) "
        "VALUES (?, '2026-04-30T15:30:00+00:00', '2026-04-30T10:30:00+00:00', "
        " '2026-04-30T10:30:00+00:00', '2026-04-30T11:00:00+00:00', 30.0, "
        " 40.0, 0, 0, '2026-04-30T10:30:00+00:00', "
        " '2026-04-30T11:00:00+00:00', 'unattributed')", (_836_KEY,))
    block_id = int(conn.execute("SELECT MAX(id) FROM five_hour_blocks").fetchone()[0])
    _836_snapshot(conn, "unattributed", 40.0, "2026-04-30T10:40:00Z",
                  five_hour_percent=20.0)
    crossing = _836_snapshot(conn, "unattributed", 43.0, "2026-04-30T11:00:00Z",
                             five_hour_percent=30.0)
    conn.execute(
        "INSERT INTO five_hour_milestones "
        "(block_id, five_hour_window_key, percent_threshold, captured_at_utc, "
        " usage_snapshot_id, account_key, seven_day_pct_at_crossing) "
        "VALUES (?, ?, 30, '2026-04-30T11:00:00Z', ?, 'unattributed', 43.0)",
        (block_id, _836_KEY, crossing))
    conn.commit()
    return crossing


def _836_crossing_values(envelope, captured_prefix=""):
    return [m["effective_seven_day_pct_at_crossing"]
            for m in envelope["current_week"]["five_hour_milestones"]
            if str(m["captured_at_utc"]).startswith(captured_prefix)]


def test_836_a_detection_tick_suppression_publishes_no_effective_value(ns836):
    """(b) A credit's detection tick suppresses the crossing's snapshot (the
    reset row and the in-place retirement commit together)."""
    conn = ns836["open_db"]()
    try:
        crossing = _836_seed_live_crossing(conn)
        first, env1, _ = _836_tick(ns836, _836_T)
        assert _836_crossing_values(env1) == [43.0]
        assert env1["current_week"]["used_pct"] == 43.0
        conn.execute(
            "INSERT INTO week_reset_events (detected_at_utc, old_week_end_at, "
            " new_week_end_at, effective_reset_at_utc, account_key) "
            "VALUES ('2026-04-30T11:30:05Z', '2026-05-04T00:00:00+00:00', "
            " '2026-05-04T00:00:00+00:00', '2026-04-30T10:40:00Z', "
            " 'unattributed')")
        conn.execute("UPDATE weekly_usage_snapshots "
                     "SET weekly_observation_held = 1 WHERE id = ?", (crossing,))
        conn.commit()
    finally:
        conn.close()
    second, env2, rebuilt = _836_tick(ns836, _836_T + dt.timedelta(seconds=15))
    _836_assert_rebuilt_and_republished(first, second, rebuilt)
    assert _836_crossing_values(env2) == [None]
    assert env2["current_week"]["used_pct"] == 40.0


def _836_record(ns, monkeypatch, at, percent, five):
    import argparse
    monkeypatch.setenv("CCTALLY_AS_OF", at.isoformat().replace("+00:00", "Z"))
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    return ns["cmd_record_usage"](argparse.Namespace(
        percent=percent, resets_at=1788879600, five_hour_percent=five,
        five_hour_resets_at=1788613200, week_start_name=None))


def test_836_effects_only_retirement_and_restoration_leave_the_idle_path(
        ns836, monkeypatch):
    """(c) The credit-retry schedule (`tests/test_exact_origin_credit_dedup.py`
    a5 recovery-delete case): a 63% replica carrying its own five-hour reading
    lands after the 0% credit and is published once; the identical 0%
    observation is then retried, the reset insert deduplicates, and the
    replica is RETIRED IN PLACE with no new snapshot or reset row. (d) The
    reverse: a restoration (suppression replacement or convergence) makes it
    unheld again."""
    def at(hour, minute, second=0):
        return dt.datetime(2026, 9, 5, hour, minute, second,
                           tzinfo=dt.timezone.utc)

    assert _836_record(ns836, monkeypatch, at(9, 0), 63.0, 30.0) == 0
    assert _836_record(ns836, monkeypatch, at(10, 0), 0.0, 31.0) == 0
    assert _836_record(ns836, monkeypatch, at(10, 30), 63.0, 40.0) == 0
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-09-05T10:31:00Z")
    first, env1, _ = _836_tick(ns836, at(10, 31))
    assert env1["current_week"]["used_pct"] == 63.0, (
        "non-vacuity: the card reads the replica")
    replica_at = "2026-09-05T10:30"
    published = _836_crossing_values(env1, replica_at)
    assert published and set(published) == {63.0}, (
        "non-vacuity: the replica's crossings are published", published)

    conn = ns836["open_db"]()
    try:
        counts = conn.execute(
            "SELECT (SELECT MAX(id) FROM weekly_usage_snapshots), "
            "       (SELECT COUNT(*) FROM week_reset_events)").fetchone()
        assert _836_record(ns836, monkeypatch, at(10, 0), 0.0, 31.0) == 0
        assert conn.execute(
            "SELECT (SELECT MAX(id) FROM weekly_usage_snapshots), "
            "       (SELECT COUNT(*) FROM week_reset_events)").fetchone() == counts, (
            "precondition: the retry adds no snapshot and no reset")
        replica = conn.execute(
            "SELECT id, weekly_observation_held FROM weekly_usage_snapshots "
            "WHERE captured_at_utc LIKE '2026-09-05T10:30%'").fetchall()
        assert [r[1] for r in replica] == [1], (
            "precondition: the retry retired the replica in place", replica)
    finally:
        conn.close()
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-09-05T10:31:15Z")
    second, env2, rebuilt = _836_tick(ns836, at(10, 31, 15))
    _836_assert_rebuilt_and_republished(first, second, rebuilt)
    assert _836_crossing_values(env2, replica_at) == [None] * len(published)
    assert env2["current_week"]["used_pct"] == 0.0, (
        "the card still reads the retired replica")
    assert env2["data_version"] != env1["data_version"]

    conn = ns836["open_db"]()
    try:
        conn.execute("UPDATE weekly_usage_snapshots "
                     "SET weekly_observation_held = 0 WHERE id = ?",
                     (replica[0][0],))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-09-05T10:31:30Z")
    third, env3, rebuilt = _836_tick(ns836, at(10, 31, 30))
    _836_assert_rebuilt_and_republished(second, third, rebuilt)
    assert _836_crossing_values(env3, replica_at) == published
    assert env3["current_week"]["used_pct"] == 63.0


_836_EARLIER_WEEK = ("2026-04-25", "2026-05-02")


@pytest.mark.parametrize("bob_week", [None, _836_EARLIER_WEEK],
                         ids=["same-window", "earlier-overlapping-window"])
def test_836_a_sample_captured_after_the_frozen_instant_matures(
        ns836, bob_week):
    """(e) Bob's sample is captured at T+1 s and committed before the
    signature read, so tick one counts it in the signature but excludes it by
    its as-of. At T+15 s nothing else moved; the tick must still rebuild and
    switch the displayed account — in Alice's window, and in Bob's own window
    starting two days earlier (overlapping), which a current-window-only
    eligibility read would miss."""
    conn = ns836["open_db"]()
    try:
        _836_accounts(conn)
        _836_snapshot(conn, "alice", 3.0, "2026-04-30T11:00:00Z")
        kwargs = {} if bob_week is None else {"week": bob_week}
        _836_snapshot(conn, "zbob", 40.0, "2026-04-30T11:30:01Z", **kwargs)
    finally:
        conn.close()
    first, env1, _ = _836_tick(ns836, _836_T)
    assert env1["current_week"]["account_key"] == "alice"
    second, env2, rebuilt = _836_tick(ns836, _836_T + dt.timedelta(seconds=15))
    _836_assert_rebuilt_and_republished(first, second, rebuilt)
    assert env2["current_week"]["account_key"] == "zbob"
    assert env2["current_week"]["used_pct"] == 40.0


def test_836_nothing_maturing_stays_idle(ns836):
    """(e) control: with no capture after the as-of instant the second tick
    idles and reuses the Claude source."""
    conn = ns836["open_db"]()
    try:
        _836_accounts(conn)
        _836_snapshot(conn, "alice", 3.0, "2026-04-30T11:00:00Z")
        _836_snapshot(conn, "zbob", 40.0, "2026-04-30T10:59:00Z")
    finally:
        conn.close()
    first, _env1, _ = _836_tick(ns836, _836_T)
    assert first.evidence_eligible_at is None
    second, _env2, rebuilt = _836_tick(ns836, _836_T + dt.timedelta(seconds=15))
    assert not rebuilt, "nothing matured, yet the tick rebuilt"
    assert _836_claude(second) is _836_claude(first)


def test_836_a_retained_reset_effective_after_the_instant_cuts_on_the_next_tick(
        ns836):
    """(f) Alice's reset takes effect at T+5 s. Nothing else is captured or
    detected after T (review F3: a post-reset sample captured after T would
    mature the eligibility instant by itself), so only the reset's effective
    instant can refuse the idle tick at T+15 s. Her cycle then starts at the
    reset and holds no reading yet: the card publishes no merged fallback
    (Constraint 6), its weekly milestones go with it, and the scoped index's
    current entry moves to the post-reset segment."""
    conn = ns836["open_db"]()
    try:
        _836_accounts(conn)
        _836_snapshot(conn, "zbob", 10.0, "2026-04-30T09:00:00Z")
        _836_snapshot(conn, "alice", 40.0, "2026-04-30T10:00:00Z")
        conn.execute(
            "INSERT INTO week_reset_events (detected_at_utc, old_week_end_at, "
            " new_week_end_at, effective_reset_at_utc, account_key) "
            "VALUES ('2026-04-30T11:29:00Z', '2026-05-04T00:00:00+00:00', "
            " '2026-05-04T00:00:00+00:00', '2026-04-30T11:30:05Z', 'alice')")
        conn.commit()
        for t in (39, 40):
            _836_weekly_milestone(conn, "alice", t, f"2026-04-30T09:{t}:00Z")
    finally:
        conn.close()
    first, env1, _ = _836_tick(ns836, _836_T)
    cw1 = env1["current_week"]
    assert cw1["week_start_at"] == "2026-04-27T00:00:00Z"
    assert [m["percent"] for m in cw1["milestones"]] == [39, 40]
    assert [(e["start_at_utc"], e["end_at_utc"])
            for e in cw1["week_index"] if e["is_current"]] == [
        ("2026-04-27T00:00:00Z", "2026-05-04T00:00:00Z")]
    assert first.evidence_eligible_at == dt.datetime(
        2026, 4, 30, 11, 30, 5, tzinfo=dt.timezone.utc), (
        "non-vacuity: only the reset's effective instant is pending")
    later = _836_T + dt.timedelta(seconds=15)
    second, env2, rebuilt = _836_tick(ns836, later)
    _836_assert_rebuilt_and_republished(first, second, rebuilt)
    assert env2["current_week"] is None, (
        "the card still reads the pre-reset segment after the reset")
    assert second.percent_milestones == []
    assert _836_claude(first).data["hero"]["current_week"]["used_pct"] == 40.0
    assert not _836_claude(second).data["hero"]["current_week"]
    conn = ns836["open_db"]()
    try:
        scope = ns836["_claude_current_cycle_scope"](
            conn, later, account_key="alice")
        index = ns836["build_claude_week_index"](
            conn, account_key="alice", cycle_scope=scope)
    finally:
        conn.close()
    assert [(e["start_at_utc"], e["end_at_utc"])
            for e in index if e["is_current"]] == [
        ("2026-04-30T11:30:05Z", "2026-05-04T00:00:00Z")]


def test_836_a_matured_sample_of_the_same_owner_refuses_source_reuse(ns836):
    """(g) Same owner, no threshold: 25.1% with its 25% milestone, then 25.2%
    captured at T+1 s. Nothing but the published percentage moves, so only
    the Claude source version's eligibility fragment refuses exact-version
    reuse of the prior source (the account-flip case cannot detect it)."""
    conn = ns836["open_db"]()
    try:
        _836_accounts(conn)
        _836_snapshot(conn, "zbob", 10.0, "2026-04-30T09:00:00Z")
        _836_snapshot(conn, "alice", 25.1, "2026-04-30T10:00:00Z")
        _836_weekly_milestone(conn, "alice", 25, "2026-04-30T10:00:00Z")
        _836_snapshot(conn, "alice", 25.2, "2026-04-30T11:30:01Z")
    finally:
        conn.close()
    first, env1, _ = _836_tick(ns836, _836_T)
    assert env1["current_week"]["used_pct"] == 25.1
    second, env2, rebuilt = _836_tick(ns836, _836_T + dt.timedelta(seconds=15))
    _836_assert_rebuilt_and_republished(first, second, rebuilt)
    assert env2["current_week"]["used_pct"] == 25.2
    data = _836_claude(second).data
    assert data["hero"]["current_week"]["used_pct"] == 25.2
    assert data["quota"]["current_week"]["used_pct"] == 25.2


def test_836_versions_carry_no_new_fragment_without_retired_or_pending_rows(
        ns836):
    """A10: with no retired snapshot, no unavailable join and nothing captured
    after the as-of instant, `data_version` and the Claude source version
    carry neither new fragment, so their bytes are what they were."""
    conn = ns836["open_db"]()
    try:
        _836_accounts(conn)
        _836_snapshot(conn, "alice", 3.0, "2026-04-30T11:00:00Z")
        _836_snapshot(conn, "zbob", 40.0, "2026-04-30T10:59:00Z")
        stats_sig = ns836["_cctally_tui"]._tui_compute_dispatch_signature(conn)
    finally:
        conn.close()
    snap, envelope, _ = _836_tick(ns836, _836_T)
    assert stats_sig.claude_weekly_retired_digest == ""
    assert envelope["data_version"] == ns836["_snapshot_data_version"](stats_sig)
    assert not re.search(r"\.r[0-9a-f]{64}$", envelope["data_version"])
    version = _836_claude(snap).data_version
    assert not re.search(r":r[0-9a-f]{64}", version)
    assert not re.search(r":e\d{4}-\d\d-\d\d", version)


def test_836_the_decorated_claude_version_names_its_owner_and_index(ns836):
    """Spec 5.7: a decorated Claude source publishes ONE account's card and
    that account's history index, so the owner, its label and the published
    index identity enter the exact-reuse version, while an undecorated build
    gains no fragment. Detects: a reused Claude source still carrying the
    previous owner's label or index stamps."""
    tui = ns836["_cctally_tui"]
    frag = tui._tui_claude_owner_version_fragment
    index = [{"key": "k1", "detail_stamp": "s1", "is_current": True},
             {"key": "k0", "detail_stamp": "s0", "is_current": False}]

    def data(**cw):
        return {"hero": {"current_week": {"week_index": index, **cw}}}

    base = frag(data(account_key="alice", account_label="Alice"))
    assert re.fullmatch(r"[0-9a-f]{16}", base)
    assert frag(data()) == "", "an undecorated envelope carries no account"
    assert frag(data(account_key="zbob", account_label="Alice")) != base
    assert frag(data(account_key="alice", account_label="Al")) != base
    restamped = [dict(index[0], detail_stamp="s2"), index[1]]
    assert frag({"hero": {"current_week": {
        "week_index": restamped, "account_key": "alice",
        "account_label": "Alice"}}}) != base

    conn = ns836["open_db"]()
    try:
        _836_accounts(conn)
        _836_snapshot(conn, "alice", 3.0, "2026-04-30T11:00:00Z")
        _836_snapshot(conn, "zbob", 40.0, "2026-04-30T10:59:00Z")
    finally:
        conn.close()
    snap, envelope, _ = _836_tick(ns836, _836_T)
    claude = _836_claude(snap)
    assert envelope["current_week"]["account_key"] == "alice"
    assert (":o" + frag(claude.data)) in claude.data_version

    conn = ns836["open_db"]()
    try:
        conn.execute("DELETE FROM accounts WHERE account_key = 'zbob'")
        conn.commit()
    finally:
        conn.close()
    snap, envelope, _ = _836_tick(ns836, _836_T + dt.timedelta(minutes=1))
    assert "account_key" not in envelope["current_week"]
    assert not re.search(r":o[0-9a-f]{16}(:|$)", _836_claude(snap).data_version)


# --- #1018 twin spec 5.5: idle and reuse ticks refresh the confirmation -----

def _1018_at(minute, second=0):
    return dt.datetime(2026, 9, 5, 9, minute, second, tzinfo=dt.timezone.utc)


def _1018_record(ns, monkeypatch, at):
    """One real flat reading (51 %, five-hour 10 %) captured at ``at``."""
    assert _836_record(ns, monkeypatch, at, 51.0, 10.0) == 0


def _1018_freshness(snap):
    """The card's freshness tuple and both Claude source copies."""
    claude = _836_claude(snap)
    return (
        (snap.current_week.latest_weekly_reading_at,
         snap.current_week.freshness_label, snap.current_week.freshness_age),
        claude.data["hero"]["current_week"]["freshness"],
        claude.data["quota"]["current_week"]["freshness"],
        claude.domain_freshness["quota"],
    )


def _1018_copy(label, captured, age):
    return {"label": label, "age_seconds": age,
            "captured_at": captured.strftime("%Y-%m-%dT%H:%M:%SZ")}


@pytest.mark.parametrize("path", ["pure-idle", "source-adapter-fallback"])
def test_idle_flat_reading_refreshes_weekly_confirmation_without_rebuild(
        ns836, monkeypatch, path):
    """#1018 twin (failure: a flat reading adds no stored row and leaves the
    dispatch signature unchanged, so the idle tick — pure, or through its
    bounded source-adapter fallback — republishes the prior confirmation and
    a healthy stream reads stale)."""
    _1018_record(ns836, monkeypatch, _1018_at(0))
    first, _env1, _ = _836_tick(ns836, _1018_at(0, 10))
    before = _1018_freshness(first)
    if path == "source-adapter-fallback":
        monkeypatch.setattr(ns836["_cctally_tui"],
                            "_tui_source_bundle_can_idle",
                            lambda *_a, **_k: False)
    _1018_record(ns836, monkeypatch, _1018_at(5))
    second, env2, rebuilt = _836_tick(ns836, _1018_at(5, 10))
    assert not rebuilt, "the flat reading forced a heavy rebuild"
    fresh = _1018_copy("fresh", _1018_at(5), 10)
    assert _1018_freshness(second) == (
        (_1018_at(5), "fresh", 10), fresh, fresh, "fresh")
    assert env2["current_week"]["freshness"] == fresh
    assert env2["current_week"]["last_snapshot_age_sec"] == 310
    # The previously published snapshot is replaced, never mutated.
    assert _1018_freshness(first) == before
    assert before[0] == (_1018_at(0), "fresh", 10)


def test_idle_ticks_without_a_reading_cross_the_freshness_thresholds(
        ns836, monkeypatch):
    """#1018 twin (failure: an idle tick re-runs the lookup but keeps the
    build-time label and age, so silence never ages the card or its source
    copies, or advances the quota clock from the stored row instead)."""
    _1018_record(ns836, monkeypatch, _1018_at(0))
    snaps = []
    for second, label, quota in ((10, "fresh", "fresh"),
                                 (45, "aging", "fresh"),
                                 (120, "stale", "stale")):
        snap, env, rebuilt = _836_tick(ns836, _1018_at(0, 0)
                                       + dt.timedelta(seconds=second))
        assert rebuilt is (not snaps)
        copy = _1018_copy(label, _1018_at(0), second)
        assert _1018_freshness(snap) == (
            (_1018_at(0), label, second), copy, copy, quota)
        assert env["current_week"]["freshness"] == copy
        snaps.append((snap, _1018_freshness(snap)))
    for snap, observed in snaps:
        assert _1018_freshness(snap) == observed


def _1018_retire_confirmations(ns):
    """Remove every eligible confirmation (the index flag only, as a reviewed
    hold leaves it). #1050: that UPDATE is a tracked in-place correction, so
    it moves the Claude content revision and the next tick rebuilds."""
    conn = ns["open_db"]()
    try:
        conn.execute("UPDATE claude_weekly_observation_index"
                     " SET weekly_freshness_eligible = 0")
        conn.commit()
    finally:
        conn.close()


def test_idle_stale_confirmation_that_disappears_publishes_none(
        ns836, monkeypatch):
    """#1018 twin (failure: the idle clock keeps the retained `stale` quota
    value, or the card keeps its last confirmation, when the final eligible
    confirmation is removed)."""
    _1018_record(ns836, monkeypatch, _1018_at(0))
    first, _env, _ = _836_tick(ns836, _1018_at(2))
    stale = _1018_copy("stale", _1018_at(0), 120)
    assert _1018_freshness(first) == (
        (_1018_at(0), "stale", 120), stale, stale, "stale")
    _1018_retire_confirmations(ns836)
    second, env2, rebuilt = _836_tick(ns836, _1018_at(2, 5))
    # #1050: the retirement is a stored correction the content revision
    # names, so the tick rebuilds rather than idling on the prior source.
    assert rebuilt
    assert _1018_freshness(second) == ((None, None, None), None, None, "fresh")
    assert env2["current_week"]["freshness"] is None
    assert _836_claude(second).domain_freshness["hero"] == "fresh"
    assert _1018_freshness(first) == (
        (_1018_at(0), "stale", 120), stale, stale, "stale")


def test_idle_stats_error_keeps_the_prior_confirmation(ns836, monkeypatch):
    """#1018 twin (failure: an idle lookup error publishes `now`, drops the
    label, or crashes the tick)."""
    import sqlite3 as _sqlite3
    import _cctally_oauth_samples
    _1018_record(ns836, monkeypatch, _1018_at(0))
    first, _env, _ = _836_tick(ns836, _1018_at(0, 10))

    def boom(*_a, **_k):
        raise _sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(_cctally_oauth_samples, "latest_weekly_reading_at",
                        boom)
    second, _env2, rebuilt = _836_tick(ns836, _1018_at(0, 20))
    assert not rebuilt
    assert second.current_week is first.current_week
    assert (_836_claude(second).data["hero"]["current_week"]["freshness"]
            == _836_claude(first).data["hero"]["current_week"]["freshness"])
    assert second.last_sync_error and "disk I/O error" in second.last_sync_error


def _1018_claude_data(freshness):
    return {"hero": {"current_week": {"freshness": freshness}},
            "quota": {"current_week": {"freshness": freshness}}}


@pytest.mark.parametrize("next_freshness, quota", [
    ({"label": "fresh", "age_seconds": 5,
      "captured_at": "2026-07-16T00:00:55Z"}, "fresh"),
    (None, "fresh"),
], ids=["advanced", "to-none"])
def test_claude_reuse_republishes_the_current_week_freshness(
        tmp_path, monkeypatch, next_freshness, quota):
    """#1018 twin (failure: a build that reuses the unchanged Claude source
    while Codex rebuilds republishes the prior source copies and quota clock,
    freezing a stale label, or keeps a retained `stale` once the confirmation
    disappears)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    tui_module = ns["_cctally_tui"]
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    stale = {"label": "stale", "age_seconds": 900,
             "captured_at": "2026-07-15T23:45:00Z"}
    common = dict(projects_envelope={}, stats_conn=stats,
                  display_tz_name="UTC", claude_cost_usd=0.0,
                  claude_total_tokens=0)
    try:
        prior = tui_module._tui_build_source_bundle(
            claude_input_identity=tui_module._tui_read_claude_input_identity(
                stats),
            now_utc=now, codex_ingest_contended=False,
            claude_data=_1018_claude_data(stale), **common)
        prior_claude = prior.sources["claude"]
        assert prior_claude.domain_freshness["quota"] == "stale"
        rebuilt = tui_module._tui_build_source_bundle(
            claude_input_identity=tui_module._tui_read_claude_input_identity(
                stats),
            now_utc=now + ns["dt"].timedelta(minutes=1),
            codex_ingest_contended=True, prior_bundle=prior,
            claude_data=_1018_claude_data(next_freshness), **common)
    finally:
        stats.close()
    claude = rebuilt.sources["claude"]
    # Non-vacuity: the Claude source was REUSED, not rebuilt. A rebuild
    # stamps its own build instant (one minute later) as `last_success_at`;
    # the reuse path keeps the prior object's (#1018 twin review F7: an equal
    # `data_version` alone is what a rebuild over unchanged evidence carries
    # too).
    assert prior_claude.last_success_at == now
    assert claude.last_success_at == prior_claude.last_success_at, (
        "non-vacuity: the Claude source was rebuilt, not reused")
    assert claude.data_version == prior_claude.data_version
    assert claude.data["hero"]["current_week"]["freshness"] == next_freshness
    assert claude.data["quota"]["current_week"]["freshness"] == next_freshness
    assert claude.domain_freshness["quota"] == quota
    assert prior_claude.data["hero"]["current_week"]["freshness"] == stale
    assert prior_claude.domain_freshness["quota"] == "stale"


# --- #1018 twin review F1: a full-build lookup error recovers and is seen ---

@pytest.mark.parametrize("failing", ["selector", "scope-lookup"])
def test_full_build_reading_lookup_error_recovers_on_the_next_idle_tick(
        ns836, monkeypatch, failing):
    """#1018 twin review F1 (failure: one transient stats error in the full
    build's confirming-reading lookup leaves the card without freshness for
    as long as the idle path serves it — every tick of flat usage — with no
    recorded failure, or costs the whole current-week card)."""
    import sqlite3 as _sqlite3
    import _cctally_oauth_samples
    tui = ns836["_cctally_tui"]
    _1018_record(ns836, monkeypatch, _1018_at(0))
    if failing == "selector":
        target, name = _cctally_oauth_samples, "latest_weekly_reading_at"
    else:
        target, name = tui, "_tui_weekly_reading_scope"
    original = getattr(target, name)
    calls = {"n": 0}

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sqlite3.OperationalError("disk I/O error")
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, fail_once)
    first, _env1, rebuilt = _836_tick(ns836, _1018_at(0, 10))
    assert rebuilt and calls["n"] == 1
    # The card survives the failed lookup, without freshness.
    assert first.current_week is not None
    assert first.current_week.used_pct == 51.0
    assert _1018_freshness(first) == ((None, None, None), None, None, "fresh")
    # The next tick changes no data, so it is idle, and the lookup recovers.
    second, env2, rebuilt = _836_tick(ns836, _1018_at(0, 20))
    assert not rebuilt
    fresh = _1018_copy("fresh", _1018_at(0), 20)
    assert _1018_freshness(second) == (
        (_1018_at(0), "fresh", 20), fresh, fresh, "fresh")
    assert env2["current_week"]["freshness"] == fresh
    # The failure was recorded like every other failed card read.
    legs = [(f.leg, f.database, f.corruption) for f in first.sync_failures]
    assert ("current-week-reading", "stats_or_cache", False) in legs, legs
    assert "disk I/O error" in (first.last_sync_error or "")
    assert not second.sync_failures


def test_full_build_reading_lookup_corruption_reaches_the_stats_heal(
        ns836, monkeypatch):
    """#1018 twin review F1 (failure: corruption met only by the
    confirming-reading lookup — e.g. in its partial index — is swallowed, so
    it never reaches the stats corruption classification and heal)."""
    import sqlite3 as _sqlite3
    import _cctally_oauth_samples
    import _cctally_store
    tui = ns836["_cctally_tui"]
    _1018_record(ns836, monkeypatch, _1018_at(0))

    def corrupt(*_args, **_kwargs):
        raise _sqlite3.DatabaseError("database disk image is malformed")

    heals = []
    monkeypatch.setattr(_cctally_oauth_samples, "latest_weekly_reading_at",
                        corrupt)
    monkeypatch.setattr(tui, "_tui_attribute_corruption",
                        lambda *_a, **_k: ("stats", True))
    monkeypatch.setattr(_cctally_store, "HEAL_HOOK",
                        lambda *_a, **_k: heals.append(1) or True)
    snap, _env, _ = _836_tick(ns836, _1018_at(0, 10))
    assert heals == [1], "the corruption never reached the stats heal"
    assert snap.current_week is not None and snap.current_week.used_pct == 51.0
    assert ("current-week-reading", "stats", True) in [
        (f.leg, f.database, f.corruption) for f in snap.sync_failures]


# --- #1018 twin review F4/F9: one threshold set on every path ---------------

def test_configured_freshness_thresholds_agree_on_full_and_idle_ticks(
        ns836, monkeypatch):
    """#1018 twin review F4 (failure: the full build derives the Claude source
    copies and quota clock with the default oauth_usage thresholds while idle
    ticks use the configured ones, so with custom thresholds the published
    label flips between a full build and the next idle tick)."""
    ns836["save_config"]({"oauth_usage": {
        "fresh_threshold_seconds": 300, "stale_after_seconds": 600}})
    _1018_record(ns836, monkeypatch, _1018_at(0))
    # 120 s old: `stale` under the defaults (30 s / 90 s), `fresh` as
    # configured.
    first, _env1, rebuilt = _836_tick(ns836, _1018_at(2))
    assert rebuilt
    second, _env2, rebuilt = _836_tick(ns836, _1018_at(2, 10))
    assert not rebuilt
    full = _1018_copy("fresh", _1018_at(0), 120)
    idle = _1018_copy("fresh", _1018_at(0), 130)
    assert _1018_freshness(first) == (
        (_1018_at(0), "fresh", 120), full, full, "fresh")
    assert _1018_freshness(second) == (
        (_1018_at(0), "fresh", 130), idle, idle, "fresh")


def test_idle_initial_absence_then_a_reading_publishes_it(ns836, monkeypatch):
    """#1018 twin spec 8.3 initial absence (failure: a card built with no
    confirming reading keeps null freshness on every idle tick after the
    first confirming reading arrives, because the idle path only re-ages a
    confirmation it already had)."""
    _1018_record(ns836, monkeypatch, _1018_at(0))
    _1018_retire_confirmations(ns836)
    first, env1, rebuilt = _836_tick(ns836, _1018_at(0, 10))
    assert rebuilt
    assert first.current_week.weekly_reading_scope is not None
    assert _1018_freshness(first) == ((None, None, None), None, None, "fresh")
    assert env1["current_week"]["freshness"] is None
    _1018_record(ns836, monkeypatch, _1018_at(5))
    second, env2, rebuilt = _836_tick(ns836, _1018_at(5, 10))
    assert not rebuilt, "the flat reading forced a heavy rebuild"
    fresh = _1018_copy("fresh", _1018_at(5), 10)
    assert _1018_freshness(second) == (
        (_1018_at(5), "fresh", 10), fresh, fresh, "fresh")
    assert env2["current_week"]["freshness"] == fresh
    assert _1018_freshness(first) == ((None, None, None), None, None, "fresh")


# ── #1050: one correction-revision identity for in-place rewrites ────────
#
# The RED oracle of docs/superpowers/specs/2026-11-04-1050-correction-signal-
# design.md §8.1/§8.2. An in-place rewrite of a Claude-published stats row (a
# direct probe UPDATE, a Model-A convergence, a reviewed rederive, the open-
# block projection) or of a Codex cycle-detail input moves none of the
# dashboard's change signals, so a running dashboard keeps the pre-rewrite
# figures on its idle path, a reused source keeps the old values and a cached
# cycle detail is never refetched. Every case follows §8.1's five steps: tick,
# apply the writer through its named production entry, assert the stored
# change (setup proof, a plain `assert`), tick again, then check every §8.2
# GREEN condition. The GREEN conditions are collected rather than raised one
# at a time, so a RED run names every condition the base fails.
#
# Surfaces are read only through what a tick publishes (the snapshot, the
# envelope, the source bundle) and through the dashboard's own detail-route
# handler, whose signatures the later tasks keep stable.

_1050_DATA_TABLES = (
    "weekly_usage_snapshots", "weekly_cost_snapshots", "percent_milestones",
    "five_hour_milestones", "five_hour_blocks", "five_hour_block_models",
    "five_hour_block_projects", "five_hour_reset_events", "week_reset_events",
    "weekly_credit_floors", "budget_milestones", "projected_milestones",
    "project_budget_milestones", "quota_window_blocks",
    "quota_percent_milestones",
)


class _1050_Green:
    """The §8.2 GREEN conditions of one case, checked together."""

    def __init__(self, case):
        self.case = case
        self.failed: list[str] = []

    def check(self, ok, label):
        if not ok:
            self.failed.append(label)

    def verify(self):
        assert not self.failed, (
            f"#1050 {self.case}: GREEN assertions failed: {self.failed}")


def _1050_arm(ns, monkeypatch):
    """Count full rebuilds (the idle path builds no forecast view) and start
    from an empty dispatch memo, exactly as `ns836` does."""
    import _lib_snapshot_cache as sc
    sc.reset_dispatch_state()
    tui = ns["_cctally_tui"]
    calls = {"rebuild": 0}
    original = tui._tui_build_forecast_view

    def counted(*args, **kwargs):
        calls["rebuild"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tui, "_tui_build_forecast_view", counted)
    ns["_1050_calls"] = calls
    return ns


def _1050_disarm(ns):
    import _lib_snapshot_cache as sc
    sc.reset_dispatch_state()
    ns["_cctally_tui"]._tui_reset_partial_retry_state()


@pytest.fixture
def ns1050(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    yield _1050_arm(ns, monkeypatch)
    _1050_disarm(ns)


@pytest.fixture
def iso1050(tmp_path, monkeypatch):
    """The isolated module the rederive command tests run under."""
    from conftest import load_isolated_cctally_module

    monkeypatch.setenv("TZ", "Etc/UTC")
    monkeypatch.setenv("CCTALLY_TEST_NO_ACCOUNT_BOUND_CUTOVER", "1")
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    ns = vars(mod)
    yield _1050_arm(ns, monkeypatch)
    _1050_disarm(ns)


def _1050_tick(ns, now):
    """One real dashboard tick, with the prior snapshot and source bundle
    threaded through the dispatch memo."""
    import _lib_snapshot_cache as sc
    before = ns["_1050_calls"]["rebuild"]
    snap = ns["_tui_build_snapshot"](
        now_utc=now, skip_sync=True, precompute_envelope=True,
        runtime_bind="127.0.0.1", display_tz_pref_override="utc")
    envelope = ns["snapshot_to_envelope"](snap, now_utc=now)
    key, _published = sc.dispatch_state()
    return SimpleNamespace(
        snap=snap, envelope=envelope, key=key,
        rebuilt=ns["_1050_calls"]["rebuild"] > before)


def _1050_source(tick, provider="claude"):
    return tick.snap.source_bundle.sources[provider]


def _1050_card_pct(tick):
    return _1050_source(tick).data["hero"]["current_week"]["used_pct"]


def _1050_claude_index(tick):
    return {e["key"]: e for e in (tick.snap.week_index or [])}


def _1050_codex_index(tick):
    quota = _1050_source(tick, "codex").data.get("quota") or {}
    return {e["key"]: e for e in (quota.get("cycle_index") or ())}


def _1050_route(ns, source, key, *, account=None):
    """The dashboard's own `/api/milestones/<source>/week/<key>` handler."""
    import urllib.parse
    path = f"/api/milestones/{source}/week/" + urllib.parse.quote(key, safe="")
    if account is not None:
        path += "?account=" + account
    sent = []
    handler = SimpleNamespace(
        path=path,
        _send_milestones_json=lambda status, body: sent.append((status, body)),
        log_error=lambda *args: sent.append(("log_error", args)),
        send_error=lambda *args: sent.append(("send_error", args)),
    )
    ns["DashboardHTTPHandler"]._handle_get_milestones_week(handler, path)
    assert len(sent) == 1 and sent[0][0] == 200, sent
    return sent[0][1]


def _1050_details(ns, source, keys):
    return {key: _1050_route(ns, source, key) for key in keys}


def _1050_store_rows(db_path):
    """Every stored row of the published data tables, read-only."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = {}
        for table in _1050_DATA_TABLES:
            if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone() is None:
                continue
            out[table] = repr(conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid").fetchall())
        return out
    finally:
        conn.close()


def _1050_stats_path():
    import _cctally_core
    return _cctally_core.DB_PATH


def _1050_cache_path():
    import _cctally_core
    return _cctally_core.CACHE_DB_PATH


def _1050_signature_of(key):
    for element in key or ():
        if isinstance(element, SnapshotSignature):
            return element
    return None


def _1050_diagnose(case, first, second):
    """Print (never assert) which pre-existing legs moved between two ticks."""
    import itertools
    a, b = _1050_signature_of(first.key), _1050_signature_of(second.key)
    moved = []
    if a is not None and b is not None:
        da, db = a._asdict(), b._asdict()
        moved = sorted(name for name in set(da) | set(db)
                       if da.get(name) != db.get(name))
    others = [
        index for index, (x, y) in enumerate(itertools.zip_longest(
            first.key or (), second.key or ()))
        if not isinstance(x, SnapshotSignature) and x != y
    ]
    sources = first.snap.source_bundle.sources
    later = second.snap.source_bundle.sources
    print(
        f"[#1050 legs] {case}: rebuilt={second.rebuilt} "
        f"signature_legs_moved={moved} other_key_elements_moved={others} "
        f"data_version_moved={first.snap.data_version != second.snap.data_version} "
        f"claude_version_moved="
        f"{sources['claude'].data_version != later['claude'].data_version} "
        f"codex_version_moved="
        f"{sources['codex'].data_version != later['codex'].data_version}")


def _1050_check_tick(green, first, second):
    green.check(second.rebuilt, "the second tick is non-idle")
    green.check(second.snap.data_version != first.snap.data_version,
                "data_version changed")


def _1050_check_claude_source(green, first, second, *, card_pct=None):
    green.check(_1050_source(second) is not _1050_source(first),
                "the Claude source object is new")
    if card_pct is not None:
        green.check(_1050_card_pct(second) == card_pct,
                    f"the Claude source card percent is {card_pct}")


def _1050_check_stamps(green, ns, source, first, second, details_before,
                       details_after, *, affected=None):
    """``affected``: the keys whose stamps must change (the others must keep
    theirs); ``None`` means every cycle's stamp must change."""
    index = _1050_claude_index if source == "claude" else _1050_codex_index
    before, after = index(first), index(second)
    for key in sorted(set(before) & set(after)):
        must_move = affected is None or key in affected
        index_moved = before[key]["detail_stamp"] != after[key]["detail_stamp"]
        detail_moved = (details_before[key]["detail_stamp"]
                        != details_after[key]["detail_stamp"])
        what = "changed" if must_move else "unchanged"
        green.check(index_moved == must_move,
                    f"{source} index detail_stamp {what} for {key}")
        green.check(detail_moved == must_move,
                    f"{source} detail echoed stamp {what} for {key}")


def _1050_check_rows(green, rows_before, rows_after):
    green.check(rows_before == rows_after,
                "the corrected store's rows are byte-equal across the ticks")


# ---- P1–P4: the issue's probe, unarmed rows, direct UPDATEs ---------------

_1050_P_NOW = dt.datetime(2026, 4, 30, 11, 30, tzinfo=dt.timezone.utc)
_1050_P_CURRENT = "2026-04-27T00:00:00Z"
_1050_P_PRIOR = "2026-04-20T00:00:00Z"


def _1050_seed_probe(conn):
    """The issue's probe seed (one cycle with a five-hour block, a five-hour
    milestone and a weekly milestone, every milestone UNARMED) plus an earlier
    cycle the rewrites never touch."""
    prior = conn.execute(
        "INSERT INTO weekly_usage_snapshots (week_start_date, week_end_date, "
        "captured_at_utc, week_start_at, week_end_at, weekly_percent, "
        "payload_json) VALUES ('2026-04-20', '2026-04-27', "
        "'2026-04-26T11:00:00Z', '2026-04-20T00:00:00Z', "
        "'2026-04-27T00:00:00Z', 80.0, '{}')").lastrowid
    conn.execute(
        "INSERT INTO percent_milestones (week_start_date, week_end_date, "
        "week_start_at, week_end_at, percent_threshold, captured_at_utc, "
        "usage_snapshot_id, cost_snapshot_id, cumulative_cost_usd, "
        "marginal_cost_usd, five_hour_percent_at_crossing, reset_event_id) "
        "VALUES ('2026-04-20', '2026-04-27', '2026-04-20T00:00:00Z', "
        "'2026-04-27T00:00:00Z', 80, '2026-04-26T11:00:00Z', ?, 0, 5.0, 0.2, "
        "NULL, 0)", (prior,))
    snap_id = conn.execute(
        "INSERT INTO weekly_usage_snapshots (week_start_date, week_end_date, "
        "captured_at_utc, week_start_at, week_end_at, weekly_percent, "
        "five_hour_percent, five_hour_window_key, payload_json) "
        "VALUES ('2026-04-27', '2026-05-04', '2026-04-30T11:00:00Z', "
        "'2026-04-27T00:00:00Z', '2026-05-04T00:00:00Z', 40.0, 30.0, "
        "1777566600, '{}')").lastrowid
    block_id = conn.execute(
        "INSERT INTO five_hour_blocks (five_hour_window_key, "
        "five_hour_resets_at, block_start_at, first_observed_at_utc, "
        "last_observed_at_utc, final_five_hour_percent, "
        "seven_day_pct_at_block_start, seven_day_pct_at_block_end, "
        "crossed_seven_day_reset, is_closed, created_at_utc, "
        "last_updated_at_utc, total_cost_usd) VALUES "
        "(1777566600, '2026-04-30T16:30:00+00:00', '2026-04-30T11:30:00+00:00', "
        "'2026-04-30T10:30:00+00:00', '2026-04-30T11:00:00+00:00', 30.0, 38.0, "
        "40.0, 0, 0, '2026-04-30T10:30:00+00:00', '2026-04-30T11:00:00+00:00', "
        "1.0)").lastrowid
    conn.execute(
        "INSERT INTO five_hour_milestones (block_id, five_hour_window_key, "
        "percent_threshold, captured_at_utc, usage_snapshot_id, block_cost_usd, "
        "reset_event_id) VALUES (?, 1777566600, 30, '2026-04-30T11:00:00Z', ?, "
        "1.0, 0)", (block_id, snap_id))
    conn.execute(
        "INSERT INTO percent_milestones (week_start_date, week_end_date, "
        "week_start_at, week_end_at, percent_threshold, captured_at_utc, "
        "usage_snapshot_id, cost_snapshot_id, cumulative_cost_usd, "
        "marginal_cost_usd, five_hour_percent_at_crossing, reset_event_id) "
        "VALUES ('2026-04-27', '2026-05-04', '2026-04-27T00:00:00Z', "
        "'2026-05-04T00:00:00Z', 40, '2026-04-30T11:00:00Z', ?, 0, 2.0, 0.1, "
        "30.0, 0)", (snap_id,))
    conn.commit()


def _1050_first_block(detail):
    return (detail.get("blocks") or [{}])[0]


def _1050_first_weekly(detail):
    segments = detail.get("segments") or [{}]
    return (segments[0].get("milestones") or [{}])[0]


# case → (statement, the stored column read back, its new value,
#         the Claude source value probe, the cycle-detail value probe)
_1050_PROBES = {
    "p1_weekly_percent": (
        "UPDATE weekly_usage_snapshots SET weekly_percent = 31.0 "
        "WHERE week_start_date = '2026-04-27'",
        "SELECT weekly_percent FROM weekly_usage_snapshots "
        "WHERE week_start_date = '2026-04-27'", 31.0,
        lambda data: data["hero"]["current_week"]["used_pct"],
        None,
    ),
    "p2_block_cost_usd": (
        "UPDATE five_hour_milestones SET block_cost_usd = 2.5",
        "SELECT block_cost_usd FROM five_hour_milestones", 2.5,
        lambda data: data["hero"]["current_week"]["five_hour_milestones"][0][
            "block_cost_usd"],
        lambda detail: _1050_first_block(detail)["milestones"][0][
            "block_cost_usd"],
    ),
    "p3_block_total_cost_usd": (
        "UPDATE five_hour_blocks SET total_cost_usd = 7.5",
        "SELECT total_cost_usd FROM five_hour_blocks", 7.5,
        None,
        lambda detail: _1050_first_block(detail)["total_cost_usd"],
    ),
    "p4_cumulative_cost_usd": (
        "UPDATE percent_milestones SET cumulative_cost_usd = 9.0 "
        "WHERE week_start_date = '2026-04-27'",
        "SELECT cumulative_cost_usd FROM percent_milestones "
        "WHERE week_start_date = '2026-04-27'", 9.0,
        lambda data: data["hero"]["current_week"]["milestones"][0][
            "cumulative_usd"],
        lambda detail: _1050_first_weekly(detail)["cumulative_usd"],
    ),
}


@pytest.mark.parametrize("case", sorted(_1050_PROBES))
def test_1050_probe_rewrite_republishes(ns1050, case):
    """#1050 §8.1 P1–P4. Detects: an in-place UPDATE of a published Claude
    value (weekly percent, five-hour milestone cost, block total, weekly
    milestone cumulative cost) on unarmed rows leaves the running dashboard
    idle on the pre-rewrite snapshot, reuses the old Claude source and keeps
    the cycle's history stamp, so the open dashboard and a cached cycle
    detail keep the old figure."""
    ns = ns1050
    statement, readback, value, source_probe, detail_probe = _1050_PROBES[case]
    conn = ns["open_db"]()
    try:
        _1050_seed_probe(conn)
    finally:
        conn.close()
    first = _1050_tick(ns, _1050_P_NOW)
    index = _1050_claude_index(first)
    current = [k for k, e in index.items()
               if e["start_at_utc"] == _1050_P_CURRENT]
    assert len(current) == 1 and any(
        e["start_at_utc"] == _1050_P_PRIOR for e in index.values()), index
    details_before = _1050_details(ns, "claude", index)

    conn = ns["open_db"]()
    try:
        conn.execute(statement)
        conn.commit()
        assert conn.execute(readback).fetchone()[0] == value
    finally:
        conn.close()
    rows_before = _1050_store_rows(_1050_stats_path())

    second = _1050_tick(ns, _1050_P_NOW + dt.timedelta(minutes=1))
    details_after = _1050_details(ns, "claude", index)
    rows_after = _1050_store_rows(_1050_stats_path())
    _1050_diagnose(case, first, second)
    if detail_probe is not None:
        assert detail_probe(details_after[current[0]]) == value, (
            "non-vacuity: the fetched detail publishes the rewritten value")

    green = _1050_Green(case)
    _1050_check_tick(green, first, second)
    _1050_check_claude_source(green, first, second)
    if source_probe is not None:
        green.check(source_probe(_1050_source(second).data) == value,
                    f"the Claude source publishes {value}")
    _1050_check_stamps(green, ns, "claude", first, second, details_before,
                       details_after, affected=set(current))
    _1050_check_rows(green, rows_before, rows_after)
    green.verify()


# ---- W1: Model-A convergence ------------------------------------------------

_1050_W1_AT = "2026-07-25T15:00:00Z"
_1050_W1_EVT = "sa:1050:w1"


def _1050_snapshot_columns(*, start, end, captured, percent):
    return {
        "captured_at_utc": captured,
        "week_start_date": start,
        "week_end_date": end,
        "week_start_at": f"{start}T00:00:00+00:00",
        "week_end_at": f"{end}T00:00:00+00:00",
        "weekly_percent": percent,
        "source": "record-usage",
        "payload_json": "{}",
        "account_key": "unattributed",
    }


def _1050_emit_snapshot(jr, conn, evt_id, columns):
    """One `snapshot_accept` Model-A emission inside one ingest cycle."""
    ctx = jr.IngestContext(conn=conn, batch=[])
    conn.execute("BEGIN IMMEDIATE")
    try:
        rowid = jr.emit_model_a(
            ctx, kind="snapshot_accept", evt_id=evt_id,
            table="weekly_usage_snapshots", columns=columns,
            at=columns["captured_at_utc"])
        conn.commit()
        return rowid
    except BaseException:
        conn.rollback()
        raise


def _1050_journal_line_count(jr):
    import _lib_journal
    count = 0
    for name in jr.list_segments():
        with open(jr._cctally_core.JOURNAL_DIR / name, "rb") as fh:
            count += sum(_lib_journal.decode_line(raw) is not None for raw in fh)
    return count


def test_1050_model_a_convergence_republishes(ns1050):
    """#1050 §8.1 W1. Detects: a live Model-A convergence
    (`emit_model_a` → CLASSIFY_CONFLICT → `_converge_row_from_effective`)
    rewrites a snapshot's `weekly_percent` back to its retained effective
    value with no journal append, and the dashboard keeps publishing the
    drifted percent."""
    ns = ns1050
    import _cctally_journal as jr
    conn = jr._cctally_core.open_db()
    try:
        _1050_emit_snapshot(jr, conn, "sa:1050:w1-prior", _1050_snapshot_columns(
            start="2026-07-13", end="2026-07-20",
            captured="2026-07-18T15:00:00Z", percent=80.0))
        current = _1050_snapshot_columns(
            start="2026-07-20", end="2026-07-27", captured=_1050_W1_AT,
            percent=40.0)
        rowid = _1050_emit_snapshot(jr, conn, _1050_W1_EVT, current)
        assert rowid is not None
        conn.execute(
            "UPDATE weekly_usage_snapshots SET weekly_percent = 31.0 "
            "WHERE id = ?", (rowid,))
        conn.commit()
    finally:
        conn.close()
    now = dt.datetime(2026, 7, 25, 15, 30, tzinfo=dt.timezone.utc)
    first = _1050_tick(ns, now)
    assert _1050_card_pct(first) == 31.0
    index = _1050_claude_index(first)
    affected = {k for k, e in index.items()
                if e["start_at_utc"] == "2026-07-20T00:00:00Z"}
    assert len(affected) == 1 and len(index) == 2, index
    details_before = _1050_details(ns, "claude", index)

    lines = _1050_journal_line_count(jr)
    conn = jr._cctally_core.open_db()
    try:
        # A divergent re-emission under the retained id is a conflict: it
        # appends nothing and converges the row to the effective 40.0.
        assert _1050_emit_snapshot(
            jr, conn, _1050_W1_EVT, dict(current, weekly_percent=55.0)) == rowid
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots WHERE id = ?",
            (rowid,)).fetchone()[0] == 40.0
    finally:
        conn.close()
    assert _1050_journal_line_count(jr) == lines, "no journal record appended"
    rows_before = _1050_store_rows(_1050_stats_path())

    second = _1050_tick(ns, now + dt.timedelta(minutes=1))
    details_after = _1050_details(ns, "claude", index)
    rows_after = _1050_store_rows(_1050_stats_path())
    _1050_diagnose("w1", first, second)

    green = _1050_Green("w1")
    _1050_check_tick(green, first, second)
    _1050_check_claude_source(green, first, second, card_pct=40.0)
    _1050_check_stamps(green, ns, "claude", first, second, details_before,
                       details_after, affected=affected)
    _1050_check_rows(green, rows_before, rows_after)
    green.verify()


# ---- W2/W5: a reviewed `db rederive` correction ----------------------------

_1050_RD_AT = "2026-07-25T12:00:00Z"
_1050_KNOWN_MODEL = "claude-3-5-sonnet-20241022"


def _1050_insert_claude_entries(ns, rows, *, account):
    """Claude cache rows: (offset, ISO timestamp, input tokens)."""
    path = "/tmp/claude/projects/repo/1050-session.jsonl"
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
            (path, 100000, 1, 100000, _1050_RD_AT, "session-1050", "/repo"))
        for offset, stamp, input_tokens in rows:
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, input_tokens, "
                " output_tokens, cache_create_tokens, cache_read_tokens, "
                " cache_create_1h_tokens, account_key) "
                "VALUES (?,?,?,?,?,0,0,0,0,?)",
                (path, offset, stamp, _1050_KNOWN_MODEL, input_tokens, account))
        conn.commit()
    finally:
        conn.close()


def _1050_rederive(ns, capsys, *, yes):
    capsys.readouterr()
    code = ns["cmd_db_rederive"](
        argparse_namespace(family="claude-usage", yes=yes, json=True))
    out = capsys.readouterr().out
    assert code == 0, out
    return json.loads(out)


def argparse_namespace(**kwargs):
    import argparse
    return argparse.Namespace(**kwargs)


def _1050_seed_wrong_snapshot_journal(ns):
    """The `test_wrong_snapshot_is_superseded_and_applied_plan_is_noop`
    shape: the raw observation says 10.0, the retained `snapshot_accept`
    (and its cost snapshot) a deliberately wrong 99.0, published by a
    rebuild."""
    import _cctally_journal as runtime
    import _lib_journal as journal

    resets = int(dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc).timestamp())
    obs = journal.make_obs(
        at=_1050_RD_AT, src="record-usage", provider="claude", account="acct-a",
        payload={"captured_at": _1050_RD_AT, "source": "statusline",
                 "weekly_percent": 10.0, "resets_at": resets})
    cache = ns["open_cache_db"]()
    try:
        desired = ns["plan_claude_usage_rederive"](
            [obs], cache_conn=cache,
            journal_high_water=("observations-2026-07.jsonl", 1))
    finally:
        cache.close()
    wrong = []
    for action in desired.actions:
        payload = dict(action.payload or {})
        if payload.get("kind") == "snapshot_accept":
            payload["weekly_percent"] = 99.0
        if payload.get("kind") == "weekly_cost_snapshot":
            payload["cost_usd"] += 5.0
        wrong.append(journal.make_evt(
            kind=payload.pop("kind"), id=action.event_id, at=action.at,
            payload=payload))
    fixed = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
    for record in [obs, *wrong]:
        runtime.append_record(record, now_utc=fixed)
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"))


def _1050_max_snapshot_id(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT MAX(id) FROM weekly_usage_snapshots").fetchone()[0]
    finally:
        conn.close()


def _1050_latest_weekly_percent(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def test_1050_reviewed_rederive_snapshot_republishes(
        iso1050, capsys, monkeypatch):
    """#1050 §8.1 W2. Detects: a completed reviewed `db rederive --yes`
    supersedes a wrong `snapshot_accept` and republishes the corrected
    `weekly_percent` in place (snapshot `MAX(id)` unchanged), yet the
    dashboard reuses the Claude source with the wrong percent and keeps every
    cycle's history stamp."""
    ns = iso1050
    import _cctally_journal as runtime
    monkeypatch.setattr(runtime, "ALERT_DISPATCHER", lambda alerts: None)
    _1050_insert_claude_entries(
        ns, [(0, "2026-07-25T11:00:00+00:00", 100)], account="acct-a")
    _1050_seed_wrong_snapshot_journal(ns)
    now = dt.datetime(2026, 7, 25, 12, 30, tzinfo=dt.timezone.utc)
    first = _1050_tick(ns, now)
    assert _1050_card_pct(first) == 99.0
    index = _1050_claude_index(first)
    assert index
    details_before = _1050_details(ns, "claude", index)

    max_id = _1050_max_snapshot_id(ns)
    applied = _1050_rederive(ns, capsys, yes=True)
    assert applied["status"] == "applied", applied
    assert _1050_latest_weekly_percent(ns) == pytest.approx(10.0)
    assert _1050_max_snapshot_id(ns) == max_id
    rows_before = _1050_store_rows(_1050_stats_path())

    second = _1050_tick(ns, now + dt.timedelta(minutes=1))
    details_after = _1050_details(ns, "claude", index)
    rows_after = _1050_store_rows(_1050_stats_path())
    _1050_diagnose("w2", first, second)

    green = _1050_Green("w2")
    _1050_check_tick(green, first, second)
    _1050_check_claude_source(green, first, second, card_pct=10.0)
    _1050_check_stamps(green, ns, "claude", first, second, details_before,
                       details_after, affected=None)
    _1050_check_rows(green, rows_before, rows_after)
    green.verify()


def _1050_observe(points, *, account):
    """Append raw status-line observations and run the authoritative ingest
    cycle after each, the record path's own write sequence."""
    import _cctally_journal as runtime
    import _lib_journal as journal

    weekly_reset = int(
        dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc).timestamp())
    for at, five_reset, weekly_pct, five_pct in points:
        payload = {"captured_at": at, "source": "statusline",
                   "weekly_percent": weekly_pct, "resets_at": weekly_reset}
        if five_reset is not None:
            payload.update(five_hour_percent=five_pct,
                           five_hour_resets_at=five_reset)
        runtime.append_record(journal.make_obs(
            at=at, src="record-usage", provider="claude", account=account,
            payload=payload))
        assert runtime.run_stats_ingest(mode="authoritative").ran is True


def test_1050_weekly_milestone_cost_rederive_republishes(
        iso1050, capsys, monkeypatch):
    """#1050 §8.1 W5. Detects: a reviewed `db rederive --yes` whose scratch
    replay re-derives a weekly `percent_milestone`'s as-of-bounded
    `cumulative_cost_usd` (a Claude cache entry before the crossing that the
    recorded milestone did not count) rewrites the cost in place with the
    snapshots unchanged, yet the dashboard reuses the Claude source with the
    old cost and keeps every cycle's history stamp."""
    ns = iso1050
    import _cctally_journal as runtime
    monkeypatch.setattr(runtime, "ALERT_DISPATCHER", lambda alerts: None)
    account = "acct-1050"
    _1050_insert_claude_entries(
        ns, [(0, "2026-07-25T11:55:00+00:00", 1_000_000)], account=account)
    _1050_observe([
        ("2026-07-25T12:00:00Z", "2026-07-25T15:00:00Z", 1.0, 1.0),
        ("2026-07-25T12:45:00Z", "2026-07-25T15:00:00Z", 2.0, 2.0),
    ], account=account)

    def milestones():
        conn = ns["open_db"]()
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT week_start_date, percent_threshold, reset_event_id, "
                "       cumulative_cost_usd FROM percent_milestones "
                "ORDER BY week_start_date, percent_threshold")]
        finally:
            conn.close()

    recorded = milestones()
    assert recorded and recorded[0][3] == pytest.approx(3.0), recorded
    # The late cache entry lands before the first crossing; the recorded
    # milestone never counted it. It lands BEFORE the first tick, so its own
    # cache leg is already part of tick 1's dispatch read.
    _1050_insert_claude_entries(
        ns, [(100, "2026-07-25T11:58:00+00:00", 1_000_000)], account=account)
    now = dt.datetime(2026, 7, 25, 13, 0, tzinfo=dt.timezone.utc)
    first = _1050_tick(ns, now)
    old_cumulative = (
        _1050_source(first).data["hero"]["current_week"]["milestones"][0][
            "cumulative_usd"])
    assert old_cumulative == pytest.approx(3.0)
    index = _1050_claude_index(first)
    assert index
    details_before = _1050_details(ns, "claude", index)

    snapshots_before = _1050_max_snapshot_id(ns), _1050_latest_weekly_percent(ns)
    preview = _1050_rederive(ns, capsys, yes=False)
    assert preview["actionCountsByEventKind"]["percent_milestone"][
        "supersede"] >= 1, preview
    applied = _1050_rederive(ns, capsys, yes=True)
    assert applied["status"] == "applied", applied
    corrected = milestones()
    assert [r[:3] for r in corrected] == [r[:3] for r in recorded], (
        "milestone membership unchanged")
    assert corrected[0][3] == pytest.approx(6.0) != recorded[0][3]
    assert (_1050_max_snapshot_id(ns), _1050_latest_weekly_percent(ns)) == (
        snapshots_before), "the snapshots are unchanged"
    rows_before = _1050_store_rows(_1050_stats_path())

    second = _1050_tick(ns, now + dt.timedelta(minutes=1))
    details_after = _1050_details(ns, "claude", index)
    rows_after = _1050_store_rows(_1050_stats_path())
    _1050_diagnose("w5", first, second)
    current = [k for k, e in index.items() if e["is_current"]]
    assert current and _1050_first_weekly(details_after[current[0]])[
        "cumulative_usd"] == pytest.approx(6.0), (
        "non-vacuity: the fetched detail publishes the corrected cost")

    green = _1050_Green("w5")
    _1050_check_tick(green, first, second)
    _1050_check_claude_source(green, first, second)
    green.check(
        _1050_source(second).data["hero"]["current_week"]["milestones"][0][
            "cumulative_usd"] == pytest.approx(6.0),
        "the Claude source publishes the corrected cumulative cost")
    _1050_check_stamps(green, ns, "claude", first, second, details_before,
                       details_after, affected=None)
    _1050_check_rows(green, rows_before, rows_after)
    green.verify()


# ---- W4: the open-block projection on the live record path -----------------

def test_1050_open_block_projection_republishes(ns1050, monkeypatch):
    """#1050 §8.1 W4. Detects: a flat reading through the live record path
    (`cmd_record_usage` → ingest → the dirty-window flush →
    `project_open_block_end`) rewrites an open block's drifted
    `total_cost_usd` back to its projection with no new entry and no new
    snapshot, yet the dashboard stays idle and the current cycle's stamp,
    and so a cached detail, keep the drifted total."""
    ns = ns1050

    def at(hour, minute):
        return dt.datetime(2026, 9, 5, hour, minute, tzinfo=dt.timezone.utc)

    # An earlier cycle the projection never touches.
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots (week_start_date, "
            "week_end_date, captured_at_utc, week_start_at, week_end_at, "
            "weekly_percent, payload_json) VALUES ('2026-08-25', "
            "'2026-09-01', '2026-08-31T09:00:00Z', "
            "'2026-08-25T15:00:00+00:00', '2026-09-01T15:00:00+00:00', 70.0, "
            "'{}')")
        conn.commit()
    finally:
        conn.close()
    _1050_insert_claude_entries(
        ns, [(0, "2026-09-05T08:30:00+00:00", 1_000_000)],
        account="unattributed")
    assert _836_record(ns, monkeypatch, at(9, 0), 51.0, 10.0) == 0

    def open_total():
        conn = ns["open_db"]()
        try:
            rows = conn.execute(
                "SELECT total_cost_usd FROM five_hour_blocks "
                "WHERE is_closed = 0").fetchall()
            assert len(rows) == 1, rows
            return rows[0][0]
        finally:
            conn.close()

    projected = open_total()
    assert projected == pytest.approx(3.0)
    conn = ns["open_db"]()
    try:
        conn.execute(
            "UPDATE five_hour_blocks SET total_cost_usd = 99.0 "
            "WHERE is_closed = 0")
        conn.commit()
    finally:
        conn.close()
    first = _1050_tick(ns, at(9, 5))
    index = _1050_claude_index(first)
    affected = {k for k, e in index.items() if e["is_current"]}
    assert len(affected) == 1 and len(index) >= 2, index
    details_before = _1050_details(ns, "claude", index)
    assert _1050_first_block(details_before[next(iter(affected))])[
        "total_cost_usd"] == 99.0

    max_id = _1050_max_snapshot_id(ns)
    _1018_record(ns, monkeypatch, at(9, 7))
    assert _1050_max_snapshot_id(ns) == max_id, "a flat reading: no new row"
    assert open_total() == pytest.approx(projected)
    rows_before = _1050_store_rows(_1050_stats_path())

    second = _1050_tick(ns, at(9, 9))
    details_after = _1050_details(ns, "claude", index)
    rows_after = _1050_store_rows(_1050_stats_path())
    _1050_diagnose("w4", first, second)
    assert _1050_first_block(details_after[next(iter(affected))])[
        "total_cost_usd"] == pytest.approx(projected)

    green = _1050_Green("w4")
    _1050_check_tick(green, first, second)
    _1050_check_claude_source(green, first, second)
    _1050_check_stamps(green, ns, "claude", first, second, details_before,
                       details_after, affected=affected)
    _1050_check_rows(green, rows_before, rows_after)
    green.verify()


# ---- §8.3 controls: identical replacement, restoration, the live close -----
#
# The child-set replacement of an open-block projection or a block-close
# convergence is a no-op when nothing changed, so an identical re-run moves
# no revision; a missing child set is still restored by a tracked INSERT;
# and the live expiry close moves the revision and the block's scope.


def _1050_revision_state():
    """The kernel singleton `(format_version, domain_generation, seq)`
    and every scope row, read-only."""
    conn = sqlite3.connect(f"file:{_1050_stats_path()}?mode=ro", uri=True)
    try:
        singleton = tuple(conn.execute(
            "SELECT format_version, domain_generation, seq "
            "FROM stats_content_revision").fetchone())
        scopes = [tuple(r) for r in conn.execute(
            "SELECT * FROM stats_content_revision_scopes "
            "ORDER BY kind, account_key, scope_key")]
        return singleton, scopes
    finally:
        conn.close()


def _1050_block_state(ns):
    """The one block's parent clock and close state, and both child sets."""
    conn = ns["open_db"]()
    try:
        parent = conn.execute(
            "SELECT id, is_closed, journal_id, last_updated_at_utc "
            "FROM five_hour_blocks").fetchall()
        assert len(parent) == 1, parent
        children = tuple(
            tuple(tuple(r) for r in conn.execute(
                f"SELECT * FROM {table} ORDER BY id"))
            for table in ("five_hour_block_models", "five_hour_block_projects"))
        return tuple(parent[0]), children
    finally:
        conn.close()


def _1050_at_0905(hour, minute):
    return dt.datetime(2026, 9, 5, hour, minute, tzinfo=dt.timezone.utc)


def _1050_seed_open_block(ns, monkeypatch):
    """W4's seed: an earlier cycle, one priced Claude entry and one live
    reading at 09:00 that opens the 08:00–13:00 block."""
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots (week_start_date, "
            "week_end_date, captured_at_utc, week_start_at, week_end_at, "
            "weekly_percent, payload_json) VALUES ('2026-08-25', "
            "'2026-09-01', '2026-08-31T09:00:00Z', "
            "'2026-08-25T15:00:00+00:00', '2026-09-01T15:00:00+00:00', 70.0, "
            "'{}')")
        conn.commit()
    finally:
        conn.close()
    _1050_insert_claude_entries(
        ns, [(0, "2026-09-05T08:30:00+00:00", 1_000_000)],
        account="unattributed")
    assert _836_record(ns, monkeypatch, _1050_at_0905(9, 0), 51.0, 10.0) == 0
    parent, children = _1050_block_state(ns)
    assert parent[1] == 0 and all(children), (
        "non-vacuity: an open block with both child sets", parent, children)


def test_1050_identical_projection_and_convergence_leave_seq_and_tick_idle(
        ns1050, monkeypatch):
    """#1050 §8.3. Detects: an identical open-block projection re-run (a flat
    reading through `cmd_record_usage`) or a repeated block-close convergence
    (`_converge_row_from_effective`) deleting and re-inserting unchanged child
    rows, which advances the revision and makes every later tick non-idle and
    every cycle re-stamp with nothing changed."""
    ns = ns1050
    _1050_seed_open_block(ns, monkeypatch)
    first = _1050_tick(ns, _1050_at_0905(9, 5))
    state = _1050_revision_state()
    parent, children = _1050_block_state(ns)

    _1018_record(ns, monkeypatch, _1050_at_0905(9, 7))
    parent_after, children_after = _1050_block_state(ns)
    assert parent_after[3] != parent[3], "setup: the projection re-ran"
    assert children_after == children, "the child rows are untouched"
    assert _1050_revision_state() == state, (
        "an identical projection moved the revision")
    second = _1050_tick(ns, _1050_at_0905(9, 9))
    _1050_diagnose("identical-projection", first, second)
    assert not second.rebuilt, "an identical projection made the tick non-idle"

    # The live expiry close freezes the block; its convergence is then
    # repeated against the retained close event.
    _1018_record(ns, monkeypatch, _1050_at_0905(13, 10))
    parent, children = _1050_block_state(ns)
    assert parent[1] == 1 and parent[2] is not None, (
        "setup: a journaled close", parent)
    third = _1050_tick(ns, _1050_at_0905(13, 12))
    state = _1050_revision_state()
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        for _ in range(2):
            assert jr._converge_row_from_effective(
                conn, parent[2]) == jr.CONVERGE_APPLIED
            conn.commit()
    finally:
        conn.close()
    assert _1050_block_state(ns) == (parent, children), (
        "the convergence changed no stored row")
    assert _1050_revision_state() == state, (
        "a repeated convergence moved the revision")
    fourth = _1050_tick(ns, _1050_at_0905(13, 14))
    _1050_diagnose("repeated-convergence", third, fourth)
    assert not fourth.rebuilt, "a repeated convergence made the tick non-idle"


def test_1050_open_child_restoration_is_global_only(ns1050, monkeypatch):
    """#1050 §8.3. Detects: an open block whose child sets went missing being
    left unrestored (or restored without moving the revision) by the no-op
    comparison, so the dashboard idles over the restored model and project
    rows; and a restoration re-stamping cycles, which a global-only child
    table must never do."""
    ns = ns1050
    _1050_seed_open_block(ns, monkeypatch)
    conn = ns["open_db"]()
    try:
        conn.execute("DELETE FROM five_hour_block_models")
        conn.execute("DELETE FROM five_hour_block_projects")
        conn.commit()
    finally:
        conn.close()
    first = _1050_tick(ns, _1050_at_0905(9, 5))
    index = _1050_claude_index(first)
    assert len(index) >= 2, index
    details_before = _1050_details(ns, "claude", index)
    (fv, dg, seq), scopes = _1050_revision_state()

    _1018_record(ns, monkeypatch, _1050_at_0905(9, 7))
    _parent, children = _1050_block_state(ns)
    assert all(children), "the projection restored both child sets"
    (fv2, dg2, seq2), scopes2 = _1050_revision_state()
    assert (fv2, dg2) == (fv, dg) and seq2 > seq, (
        "a restoration by INSERT advances seq", seq, seq2)
    assert scopes2 == scopes, "a child restoration touches no scope"

    second = _1050_tick(ns, _1050_at_0905(9, 9))
    details_after = _1050_details(ns, "claude", index)
    _1050_diagnose("child-restoration", first, second)
    green = _1050_Green("child-restoration")
    _1050_check_tick(green, first, second)
    _1050_check_claude_source(green, first, second)
    _1050_check_stamps(green, ns, "claude", first, second, details_before,
                       details_after, affected=set())
    green.verify()


def test_1050_live_expiry_close_republishes_closed_block_panel(
        ns1050, monkeypatch):
    """#1050 §8.3 live close chain. Detects: a flat reading after the block's
    reset closing it by natural expiration (`cmd_record_usage` → the expiry
    sweep → `freeze_five_hour_block_close`) without moving the revision or
    the block's scope, or the dashboard keeping the computed Blocks-panel
    row instead of the frozen close's retained facts."""
    ns = ns1050
    _1050_seed_open_block(ns, monkeypatch)
    first = _1050_tick(ns, _1050_at_0905(13, 5))
    assert first.snap.blocks_panel and all(
        r.facts_source == "computed" for r in first.snap.blocks_panel), (
        "non-vacuity: the open block's panel row is computed",
        first.snap.blocks_panel)
    (_fv, _dg, seq), scopes = _1050_revision_state()

    max_id = _1050_max_snapshot_id(ns)
    _1018_record(ns, monkeypatch, _1050_at_0905(13, 10))
    assert _1050_max_snapshot_id(ns) == max_id, "a flat reading: no new row"
    parent, _children = _1050_block_state(ns)
    assert parent[1] == 1 and parent[2] is not None, (
        "setup: the expiry close is journaled", parent)
    (_fv, _dg, seq2), scopes2 = _1050_revision_state()
    assert seq2 > seq, "the expiry close advances seq"
    before = {s[:3]: s for s in scopes}
    dirtied = [s for s in scopes2 if s[0] == "block"
               and s[1] == "unattributed" and before.get(s[:3]) != s]
    assert dirtied and all(s[-1] > seq for s in dirtied), (
        "the expiry close dirties the block's scope", scopes, scopes2)

    second = _1050_tick(ns, _1050_at_0905(13, 12))
    _1050_diagnose("live-expiry-close", first, second)
    green = _1050_Green("live-expiry-close")
    _1050_check_tick(green, first, second)
    def retained(tick):
        return any(r.facts_source == "retained" and r.models
                   for r in tick.snap.blocks_panel)

    green.check(retained(second),
                "the closed-block model panel is republished from the close")
    # Non-vacuity of that condition: a forced full build serves the frozen
    # close's retained facts.
    import _lib_snapshot_cache as sc
    sc.reset_dispatch_state()
    forced = _1050_tick(ns, _1050_at_0905(13, 14))
    assert forced.rebuilt and retained(forced), forced.snap.blocks_panel
    green.verify()


# ---- §8.3/§8.4 controls of the Claude signals (#1050 Task 4) --------------


def _1050_projects_week_pct(envelope_block, week_start_at):
    """The Projects trend's weekly percent for one week, or a sentinel."""
    weeks = ((envelope_block or {}).get("trend") or {}).get("weeks") or ()
    return next((w.get("total_pct") for w in weeks
                 if w.get("week_start_at") == week_start_at), "absent")


def test_1050_projects_memo_republishes_corrected_percent(ns1050):
    """#1050 §8.3 Projects memo. Detects: the whole-envelope Projects memo
    keyed only on append-shaped legs serving a warm envelope after an in-place
    `weekly_percent` correction, so the Projects panel and the Claude source
    copy keep the old attributed weekly percent on the rebuilt tick."""
    ns = ns1050
    conn = ns["open_db"]()
    try:
        _1050_seed_probe(conn)
    finally:
        conn.close()
    _1050_insert_claude_entries(
        ns, [(0, "2026-04-28T10:00:00+00:00", 100_000)],
        account="unattributed")
    first = _1050_tick(ns, _1050_P_NOW)
    assert _1050_projects_week_pct(
        first.snap.projects_envelope, _1050_P_CURRENT) == 40.0, (
        "non-vacuity: the warm envelope attributes the 40% week",
        first.snap.projects_envelope)

    conn = ns["open_db"]()
    try:
        conn.execute(_1050_PROBES["p1_weekly_percent"][0])
        conn.commit()
    finally:
        conn.close()
    second = _1050_tick(ns, _1050_P_NOW + dt.timedelta(minutes=1))
    _1050_diagnose("projects-memo", first, second)
    green = _1050_Green("projects-memo")
    green.check(second.rebuilt, "the second tick is non-idle")
    green.check(_1050_projects_week_pct(
        second.snap.projects_envelope, _1050_P_CURRENT) == 31.0,
        "the Projects envelope publishes the corrected 31%")
    green.check(_1050_projects_week_pct(
        _1050_source(second).data.get("projects"), _1050_P_CURRENT) == 31.0,
        "the Claude source copy publishes the corrected 31%")
    green.verify()


def _1050_make_untrusted(ns, state):
    """Leave the kernel objects untrusted: a missing trigger whose repair is
    refused because another process holds stats.db's write lock (the returned
    `BEGIN IMMEDIATE` connection; readers proceed), or a deleted singleton
    (malformed, never repaired)."""
    import _lib_stats_content_revision as k
    conn = sqlite3.connect(_1050_stats_path())
    try:
        if state == "partial_under_contention":
            conn.execute(f"DROP TRIGGER {k.KERNEL_TRIGGERS[0]}")
        else:
            conn.execute(f"DELETE FROM {k.REVISION_TABLE}")
        conn.commit()
    finally:
        conn.close()
    if state != "partial_under_contention":
        return None
    writer = sqlite3.connect(_1050_stats_path())
    writer.execute("BEGIN IMMEDIATE")
    return writer


@pytest.mark.parametrize("state", ["partial_under_contention", "malformed"])
def test_1050_install_contention_ticks_publish_fresh_sources(ns1050, state):
    """#1050 §8.4 installation states. Detects: an untrusted content revision
    (a repair refused under install contention, or a malformed store whose
    identity raises) being treated as an empty identity — the dashboard then
    idles or reuses a Claude source and serves cached cycle details across
    corrections it cannot see. Each tick must be non-idle, publish a freshly
    built Claude source and re-stamp every cycle."""
    import _lib_stats_content_revision as k
    ns = ns1050
    conn = ns["open_db"]()
    try:
        _1050_seed_probe(conn)
    finally:
        conn.close()
    first = _1050_tick(ns, _1050_P_NOW)
    writer = _1050_make_untrusted(ns, state)
    try:
        conn = ns["open_db"]()
        try:
            expected = (k.STATE_PARTIAL if writer is not None
                        else k.STATE_MALFORMED)
            assert k.classify(conn) == expected, "setup: untrusted on open"
        finally:
            conn.close()
        ticks = [first] + [
            _1050_tick(ns, _1050_P_NOW + dt.timedelta(minutes=m))
            for m in (1, 2)]
    finally:
        if writer is not None:
            writer.rollback()
            writer.close()
    green = _1050_Green(f"untrusted:{state}")
    for earlier, later in zip(ticks, ticks[1:]):
        _1050_diagnose(f"untrusted:{state}", earlier, later)
        green.check(later.rebuilt, "every tick is non-idle")
        green.check(later.snap.data_version != earlier.snap.data_version,
                    "data_version changes every tick")
        green.check(_1050_source(later) is not _1050_source(earlier),
                    "every tick publishes a freshly built Claude source")
        before, after = _1050_claude_index(earlier), _1050_claude_index(later)
        green.check(before and set(before) == set(after) and all(
            before[key]["detail_stamp"] != after[key]["detail_stamp"]
            for key in before), "every cycle is re-stamped every tick")
    green.verify()


def test_1050_fresh_store_keeps_data_version_and_claude_version_bytes(ns1050):
    """#1050 §8.3 fresh store. Detects: a never-mutated, never-published,
    never-repaired store publishing a content-revision fragment, which would
    move every install's `data_version` and Claude source version bytes (and
    every golden) with nothing changed. Appended snapshots are untracked."""
    ns = ns1050
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots (week_start_date, "
            "week_end_date, captured_at_utc, week_start_at, week_end_at, "
            "weekly_percent, payload_json) VALUES ('2026-04-27', "
            "'2026-05-04', '2026-04-30T11:00:00Z', '2026-04-27T00:00:00Z', "
            "'2026-05-04T00:00:00Z', 40.0, '{}')")
        conn.commit()
        assert tuple(conn.execute(
            "SELECT seq, domain_generation FROM stats_content_revision"
        ).fetchone()) == (0, 0), "setup: never mutated"
    finally:
        conn.close()
    tick = _1050_tick(ns, _1050_P_NOW)
    signature = _1050_signature_of(tick.key)
    assert signature.claude_content_revision == ""
    assert tick.snap.data_version == ns["_cctally_tui"]._snapshot_data_version(
        signature._replace(claude_content_revision="removed")
    ).rsplit(".cremoved", 1)[0], tick.snap.data_version
    import re
    assert not re.search(r":c(\?|[0-9a-f]{16})(:|$)",
                         _1050_source(tick).data_version), (
        _1050_source(tick).data_version)


# ---- C1: the Codex twin ----------------------------------------------------
#
# One Codex weekly cycle and one five-hour block, each with its own ladder,
# materialized by the production projection (`reconcile_codex_quota_
# projection`) from raw `quota_window_snapshots` evidence, with
# `codex_session_entries` placed at the ladder's physical boundaries so a
# boundary rewrite moves the per-milestone costs the detail publishes.

_1050_C_ROOT = "1050c1" + "0" * 26
_1050_C_ACCT_A = "a" * 28 + "1050"
_1050_C_ACCT_B = "b" * 28 + "1050"
_1050_C_NOW = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
_1050_C_WEEK_RESET = dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.timezone.utc)
_1050_C_BLOCK_RESET = dt.datetime(2026, 7, 25, 14, 0, tzinfo=dt.timezone.utc)
_1050_C_WEEK_KEY = (
    '{"limitId":"codex","observedSlot":"secondary","source":"codex",'
    f'"sourceRootKey":"{_1050_C_ROOT}","windowMinutes":10080}}')
_1050_C_BLOCK_KEY = (
    '{"limitId":"codex","observedSlot":"primary","source":"codex",'
    f'"sourceRootKey":"{_1050_C_ROOT}","windowMinutes":300}}')
_1050_C_T = [_1050_C_NOW - dt.timedelta(minutes=m) for m in (60, 40, 20)]
_1050_C_PATH = "/codex/a.jsonl"
_1050_C_LATER_PATH = "/codex/z.jsonl"
_1050_C_ENTRY_PATH = "/codex/m.jsonl"


def _1050_c_snapshot(cache, *, captured, window, percent, path=_1050_C_PATH,
                     offset, account=None, reset=None):
    weekly = window == 10_080
    cache.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, limit_id, limit_name, "
        " window_minutes, used_percent, resets_at_utc, account_key) "
        "VALUES ('codex', ?, ?, ?, ?, ?, ?, 'codex', NULL, ?, ?, ?, ?)",
        (_1050_C_ROOT, path, offset, captured.isoformat(),
         "secondary" if weekly else "primary",
         _1050_C_WEEK_KEY if weekly else _1050_C_BLOCK_KEY, window, percent,
         (reset or (_1050_C_WEEK_RESET if weekly else _1050_C_BLOCK_RESET)
          ).isoformat(), account))


def _1050_c_entry(cache, *, timestamp, offset, tokens, account=None,
                  path=_1050_C_ENTRY_PATH):
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, "
        " input_tokens, cached_input_tokens, output_tokens, "
        " reasoning_output_tokens, total_tokens, source_root_key, "
        " conversation_key, account_key) "
        "VALUES (?, ?, ?, 'session-1050', 'gpt-5', ?, 0, ?, 0, ?, ?, NULL, ?)",
        (path, offset, timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), tokens,
         tokens // 10, tokens + tokens // 10, _1050_C_ROOT, account))


def _1050_c_rematerialize(ns, cache, *, force_full=False):
    """Commit a cache change as an ingest does and re-run the projection."""
    import _cctally_quota as quota
    ns["_cctally_cache"]._bump_codex_physical_mutation_seq(cache)
    cache.commit()
    quota.reconcile_codex_quota_projection(
        source_root_keys=(_1050_C_ROOT,), now=_1050_C_NOW,
        force_full=force_full)


def _1050_c_ladder(cache, *, account, path, base_offset, block_final=7.3):
    t0, t1, t2 = _1050_C_T
    for window, percents in ((10_080, (10.2, 11.3, 12.4)),
                             (300, (5.1, 6.2, block_final))):
        for i, (captured, percent) in enumerate(zip((t0, t1, t2), percents)):
            _1050_c_snapshot(
                cache, captured=captured, window=window, percent=percent,
                path=path, offset=base_offset + 10 * i, account=account)


def _1050_seed_codex(ns, cache, variant):
    t0, t1, t2 = _1050_C_T
    cache.execute(
        "INSERT INTO codex_source_roots "
        "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
        "VALUES (?, ?, ?, ?)",
        (_1050_C_ROOT, os.environ["CODEX_HOME"], t0.isoformat(),
         t2.isoformat()))
    if variant == "d_account_swap":
        _1050_c_ladder(cache, account=_1050_C_ACCT_A, path=_1050_C_PATH,
                       base_offset=10, block_final=7.3)
        _1050_c_ladder(cache, account=_1050_C_ACCT_B, path="/codex/b.jsonl",
                       base_offset=10, block_final=7.8)
    else:
        _1050_c_ladder(cache, account=None, path=_1050_C_PATH, base_offset=10)
    if variant == "c_jitter_repeat":
        sibling = _1050_C_WEEK_RESET + dt.timedelta(seconds=30)
        _1050_c_snapshot(cache, captured=t0 + dt.timedelta(seconds=5),
                         window=10_080, percent=10.0, path="/codex/b.jsonl",
                         offset=11, reset=sibling)
        _1050_c_snapshot(cache, captured=t1 + dt.timedelta(seconds=10),
                         window=10_080, percent=11.6, path="/codex/b.jsonl",
                         offset=21, reset=sibling)
    # Y sits on the first boundary's capture second, X on the first crossing's:
    # a path rewrite of either boundary moves one of them across it.
    account = _1050_C_ACCT_A if variant == "d_account_swap" else None
    for offset, timestamp, tokens in (
            (1, t0, 1000), (2, t0 + dt.timedelta(minutes=10), 2000),
            (3, t1, 4000), (4, t1 + dt.timedelta(minutes=10), 8000),
            (6, t2, 16000)):
        _1050_c_entry(cache, timestamp=timestamp, offset=offset, tokens=tokens,
                      account=account)
    _1050_c_rematerialize(ns, cache)


def _1050_stats_scalar(ns, sql, params=()):
    conn = ns["open_db"]()
    try:
        return [tuple(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _1050_c_write(ns, cache, variant):
    """Apply the variant's writer and return its stored-change readback
    (before, after)."""
    t0, t1, t2 = _1050_C_T
    if variant == "a_block_percent":
        def read():
            return _1050_stats_scalar(
                ns, "SELECT current_percent FROM quota_window_blocks "
                    "WHERE window_minutes = 300")
        before = read()
        _1050_c_snapshot(cache, captured=t2 + dt.timedelta(minutes=5),
                         window=300, percent=7.6, offset=40)
        _1050_c_rematerialize(ns, cache)
        return before, read(), [(7.6,)]
    if variant in ("b_cycle_boundary", "b_nested_boundary"):
        window = 10_080 if variant == "b_cycle_boundary" else 300
        key = _1050_C_WEEK_KEY if window == 10_080 else _1050_C_BLOCK_KEY

        def read():
            return _1050_stats_scalar(
                ns, "SELECT percent_threshold, source_path FROM "
                    "quota_percent_milestones WHERE window_minutes = ? "
                    "ORDER BY percent_threshold", (window,))
        before = read()
        cache.execute(
            "UPDATE quota_window_snapshots SET source_path = ? "
            "WHERE logical_limit_key = ? AND line_offset = 20",
            (_1050_C_LATER_PATH, key))
        _1050_c_rematerialize(ns, cache)
        first = before[0][0]
        expected = [(first, _1050_C_LATER_PATH)] + before[1:]
        return before, read(), expected
    if variant == "d_account_swap":
        def read():
            return _1050_stats_scalar(
                ns, "SELECT account_key, current_percent FROM "
                    "quota_window_blocks WHERE window_minutes = 300 "
                    "ORDER BY account_key")
        before = read()
        for account, percent in ((_1050_C_ACCT_A, 7.8), (_1050_C_ACCT_B, 7.3)):
            cache.execute(
                "UPDATE quota_window_snapshots SET used_percent = ? "
                "WHERE window_minutes = 300 AND account_key = ? "
                "  AND line_offset = 30", (percent, account))
        _1050_c_rematerialize(ns, cache)
        return before, read(), [(_1050_C_ACCT_A, 7.8), (_1050_C_ACCT_B, 7.3)]
    if variant == "e_late_entry":
        late = t0 + dt.timedelta(minutes=15)

        def read():
            return [tuple(r) for r in cache.execute(
                "SELECT line_offset, timestamp_utc FROM codex_session_entries "
                "WHERE line_offset = 5")]
        before = read()
        _1050_c_entry(cache, timestamp=late, offset=5, tokens=32000)
        _1050_c_rematerialize(ns, cache)
        return before, read(), [
            (5, late.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))]
    if variant == "g_first_boundary":
        def read():
            return [tuple(r) for r in cache.execute(
                "SELECT captured_at_utc, source_path, line_offset FROM "
                "quota_window_snapshots WHERE logical_limit_key = ? "
                "ORDER BY captured_at_utc, source_path, line_offset LIMIT 1",
                (_1050_C_WEEK_KEY,))]
        before = read()
        entries = [tuple(r) for r in cache.execute(
            "SELECT * FROM codex_session_entries ORDER BY id")]
        cache.execute(
            "UPDATE quota_window_snapshots SET source_path = ?, "
            "line_offset = 15 WHERE logical_limit_key = ? AND line_offset = 10",
            (_1050_C_LATER_PATH, _1050_C_WEEK_KEY))
        _1050_c_rematerialize(ns, cache)
        assert [tuple(r) for r in cache.execute(
            "SELECT * FROM codex_session_entries ORDER BY id")] == entries, (
            "the accounting entries are unchanged")
        return before, read(), [(before[0][0], _1050_C_LATER_PATH, 15)]
    raise AssertionError(variant)


def _1050_codex_current(tick):
    index = _1050_codex_index(tick)
    current = [k for k, e in index.items() if e["is_current"]]
    assert len(current) == 1, index
    return current[0]


def _1050_without_stamp(detail):
    return {k: v for k, v in detail.items() if k != "detail_stamp"}


def _1050_file_state(*paths):
    out = []
    for path in paths:
        for candidate in (path, pathlib.Path(str(path) + "-wal")):
            if candidate.exists():
                out.append((candidate.name, candidate.read_bytes()))
    return out


_1050_C_VARIANTS = (
    "a_block_percent", "b_cycle_boundary", "b_nested_boundary",
    "c_jitter_repeat", "d_account_swap", "e_late_entry", "f_speed_idle",
    "g_first_boundary",
)


@pytest.mark.parametrize("variant", _1050_C_VARIANTS)
def test_1050_codex_cycle_detail_refreshes(tmp_path, monkeypatch, variant):
    """#1050 §8.1 C1. Detects: a cached Codex cycle detail whose stamp
    (key, distinct-threshold count, block count, max capture) omits what the
    detail publishes — a block's `current_percent`, a cycle or nested-block
    milestone's accounting boundary, an account/value swap, a late cache
    entry, the resolved service tier, the first physical boundary — so a
    reopened detail is served from the client cache with stale figures, and
    a tier change on a quiet dashboard never leaves the idle path. (c) pins
    the stamp's count as distinct thresholds and its capture leg as the raw
    maximum across jitter siblings."""
    monkeypatch.setenv("TZ", "Etc/UTC")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv(
        "CCTALLY_AS_OF", _1050_C_NOW.isoformat().replace("+00:00", "Z"))
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _1050_arm(ns, monkeypatch)
    cache = ns["open_cache_db"]()
    try:
        _1050_seed_codex(ns, cache, variant)
        first = _1050_tick(ns, _1050_C_NOW)
        key = _1050_codex_current(first)
        assert _1050_codex_index(first)[key]["block_count"] == 1
        detail_before = _1050_route(ns, "codex", key)
        assert detail_before["blocks"] and detail_before["blocks"][0][
            "milestones"], detail_before
        green = _1050_Green(f"c1:{variant}")

        if variant == "c_jitter_repeat":
            raw = _1050_stats_scalar(
                ns, "SELECT percent_threshold, captured_at_utc FROM "
                    "quota_percent_milestones WHERE window_minutes = 10080")
            assert len(raw) > len({r[0] for r in raw}), (
                "a threshold repeats across the jitter siblings", raw)
            green.check(
                _1050_codex_index(first)[key]["milestone_count"]
                == len({r[0] for r in raw}),
                "the Codex count is the distinct thresholds")
            # An identical re-materialization keeps the stamp.
            _1050_c_rematerialize(ns, cache, force_full=True)
            same = _1050_tick(ns, _1050_C_NOW + dt.timedelta(seconds=30))
            green.check(
                _1050_codex_index(same)[key]["detail_stamp"]
                == _1050_codex_index(first)[key]["detail_stamp"],
                "an identical re-materialization keeps the Codex stamp")
            # Moving only a sibling's repeated crossing later moves the raw
            # maximum capture and nothing the earliest-crossing detail shows.
            cache.execute(
                "UPDATE quota_window_snapshots SET captured_at_utc = ? "
                "WHERE logical_limit_key = ? AND line_offset = 21",
                ((_1050_C_T[2] + dt.timedelta(minutes=5)).isoformat(),
                 _1050_C_WEEK_KEY))
            _1050_c_rematerialize(ns, cache)
            moved = _1050_stats_scalar(
                ns, "SELECT MAX(captured_at_utc) FROM quota_percent_milestones "
                    "WHERE window_minutes = 10080")
            assert moved[0][0] > max(r[1] for r in raw), moved
            rows_before = _1050_store_rows(_1050_stats_path())
            second = _1050_tick(ns, _1050_C_NOW + dt.timedelta(minutes=1))
            detail_after = _1050_route(ns, "codex", key)
            rows_after = _1050_store_rows(_1050_stats_path())
            _1050_diagnose(f"c1:{variant}", first, second)
            green.check(
                _1050_codex_index(second)[key]["detail_stamp"]
                != _1050_codex_index(first)[key]["detail_stamp"],
                "a moved raw maximum capture moves the Codex stamp")
            _1050_check_rows(green, rows_before, rows_after)
            green.verify()
            return

        if variant == "f_speed_idle":
            quiet = _1050_tick(ns, _1050_C_NOW + dt.timedelta(seconds=30))
            assert not quiet.rebuilt, "the dashboard is genuinely idle"
            assert ns["_resolve_codex_speed"]("auto") == "standard"
            files = _1050_file_state(
                _1050_stats_path(), _1050_cache_path())
            (codex_home / "config.toml").write_text(
                'service_tier = "fast"\n', encoding="utf-8")
            assert ns["_resolve_codex_speed"]("auto") == "fast"
            assert _1050_file_state(
                _1050_stats_path(), _1050_cache_path()) == files, (
                "both database files are unchanged")
            first = quiet
        else:
            before, after, expected = _1050_c_write(ns, cache, variant)
            assert after == expected and after != before, (before, after)
        rows_before = _1050_store_rows(_1050_stats_path())

        second = _1050_tick(ns, _1050_C_NOW + dt.timedelta(minutes=1))
        detail_after = _1050_route(ns, "codex", key)
        rows_after = _1050_store_rows(_1050_stats_path())
        _1050_diagnose(f"c1:{variant}", first, second)
        assert key in _1050_codex_index(second), _1050_codex_index(second)

        if variant == "f_speed_idle":
            green.check(second.rebuilt, "the second tick is non-idle")
        _1050_check_stamps(
            green, ns, "codex", first, second, {key: detail_before},
            {key: detail_after}, affected={key})
        if variant in ("b_cycle_boundary", "b_nested_boundary",
                       "d_account_swap"):
            green.check(
                _1050_source(second, "codex").data_version
                != _1050_source(first, "codex").data_version,
                "codex_version changed")
            green.check(
                _1050_without_stamp(detail_after)
                != _1050_without_stamp(detail_before),
                "the route result changed")
        _1050_check_rows(green, rows_before, rows_after)
        green.verify()
    finally:
        cache.close()
        _1050_disarm(ns)


# ---- C1 controls: the Codex stamp's inputs, read directly -----------------
#
# The source build hands `build_codex_cycle_index` a process memo
# (`_CodexCycleInputsMemo`) so the index does not re-read every cycle's
# boundaries and entry aggregates on every build; the detail route always
# reads fresh. These controls drive the index with and without that memo on
# the same seeds and pin that the two agree with the detail route's stamp.


class _1050CBoundary:
    source_root_keys = (_1050_C_ROOT,)
    quota_identity = None
    resets_at = _1050_C_WEEK_RESET


def _1050_c_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Etc/UTC")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv(
        "CCTALLY_AS_OF", _1050_C_NOW.isoformat().replace("+00:00", "Z"))
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_milestone_history as mh
    mh.reset_codex_cycle_inputs_memo()
    return ns, codex_home


def _1050_c_index(ns, *, account=None, memo=True):
    """``build_codex_cycle_index`` as the source build calls it (``memo``) or
    as a caller without a stats digest does (fresh reads)."""
    import _cctally_milestone_history as mh
    import _lib_dashboard_sources as lds
    conn, cache = ns["open_db"](), ns["open_cache_db"]()
    try:
        kwargs = {"cache_conn": cache, "speed": "auto"}
        if memo:
            kwargs["stats_digest"] = lds.codex_stats_digest(conn)
        return {e["key"]: e for e in mh.build_codex_cycle_index(
            conn, identity=_1050CBoundary(), now_utc=_1050_C_NOW,
            account_key=account, **kwargs)}
    finally:
        conn.close()
        cache.close()


def _1050_c_detail_stamp(ns, key, *, account=None):
    import _cctally_milestone_history as mh
    conn, cache = ns["open_db"](), ns["open_cache_db"]()
    try:
        detail = mh.build_codex_cycle_detail(
            conn, cache, identity=_1050CBoundary(), key=key, speed="auto",
            now_utc=_1050_C_NOW, account_key=account)
    finally:
        conn.close()
        cache.close()
    assert isinstance(detail, dict), detail
    return detail["detail_stamp"]


def _1050_c_assert_coherent(ns, accounts):
    """The memoized index equals the fresh index, and every entry's stamp is
    the detail route's own; returns the memoized index per account."""
    out = {}
    for account in accounts:
        memoized = _1050_c_index(ns, account=account)
        assert memoized, f"no Codex cycles for {account}"
        assert memoized == _1050_c_index(ns, account=account, memo=False), (
            f"the memoized index diverges from a fresh read for {account}")
        for key, entry in memoized.items():
            assert entry["detail_stamp"] == _1050_c_detail_stamp(
                ns, key, account=account), (
                f"index stamp != detail stamp for {key} ({account})")
        out[account] = memoized
    return out


def _1050_c_current_key(index):
    current = [k for k, e in index.items() if e["is_current"]]
    assert len(current) == 1, index
    return current[0]


_1050_C_PRIOR_RESET = _1050_C_WEEK_RESET - dt.timedelta(days=7)
_1050_C_SHARED_PATH = "/codex/prior.jsonl"


def _1050_c_seed_shared_block(ns, cache):
    """A prior weekly cycle ending at the current cycle's start, and one 5-hour
    block straddling that boundary, so both cycles select the block and share
    its entry-aggregate key. The prior cycle's own ladder ends before the
    boundary, so an entry after it lands in the shared block's range only."""
    p = _1050_C_PRIOR_RESET
    for i, (minutes, percent) in enumerate(((120, 10.2), (100, 11.3),
                                            (80, 12.4))):
        _1050_c_snapshot(cache, captured=p - dt.timedelta(minutes=minutes),
                         window=10_080, percent=percent,
                         path=_1050_C_SHARED_PATH, offset=10 + 10 * i,
                         reset=p)
    for i, (minutes, percent) in enumerate(((-120, 5.1), (-60, 6.2),
                                            (60, 7.3))):
        _1050_c_snapshot(cache, captured=p + dt.timedelta(minutes=minutes),
                         window=300, percent=percent,
                         path=_1050_C_SHARED_PATH, offset=15 + 10 * i,
                         reset=p + dt.timedelta(hours=2))
    _1050_c_rematerialize(ns, cache)


@pytest.mark.parametrize(
    "variant", _1050_C_VARIANTS + ("h_shared_block_late_entry",))
def test_1050_codex_detail_stamp_equals_index_stamp(
        tmp_path, monkeypatch, variant):
    """#1050 §4.7. Detects: an index stamp computed from different inputs than
    the detail route's (the memo serving a stale boundary or aggregate, or a
    scope mismatch), so the client either refetches forever or never refetches
    a moved detail. Every C1 seed, before and after its writer, merged and per
    account. ``h_shared_block_late_entry``: two cycles share a straddling
    5-hour block's aggregate key and a late entry evicts it; the first cycle
    recomputes and reinserts the key, and the second must not keep the
    digest of the old aggregate because the key is merely present again."""
    ns, codex_home = _1050_c_env(tmp_path, monkeypatch)
    accounts = ((None, _1050_C_ACCT_A, _1050_C_ACCT_B)
                if variant == "d_account_swap" else (None,))
    cache = ns["open_cache_db"]()
    try:
        _1050_seed_codex(ns, cache, variant)
        if variant == "h_shared_block_late_entry":
            _1050_c_seed_shared_block(ns, cache)
        before = _1050_c_assert_coherent(ns, accounts)
        if variant == "h_shared_block_late_entry":
            assert len(before[None]) >= 2, (
                "non-vacuity: the prior and current cycles", before[None])
            _1050_c_entry(cache, timestamp=_1050_C_PRIOR_RESET
                          + dt.timedelta(minutes=30), offset=1, tokens=32000,
                          path="/codex/late.jsonl")
            _1050_c_rematerialize(ns, cache)
        elif variant == "f_speed_idle":
            (codex_home / "config.toml").write_text(
                'service_tier = "fast"\n', encoding="utf-8")
        elif variant == "c_jitter_repeat":
            cache.execute(
                "UPDATE quota_window_snapshots SET captured_at_utc = ? "
                "WHERE logical_limit_key = ? AND line_offset = 21",
                ((_1050_C_T[2] + dt.timedelta(minutes=5)).isoformat(),
                 _1050_C_WEEK_KEY))
            _1050_c_rematerialize(ns, cache)
        else:
            _1050_c_write(ns, cache, variant)
        after = _1050_c_assert_coherent(ns, accounts)
        key = _1050_c_current_key(after[None])
        assert after[None][key]["detail_stamp"] != before[None][key][
            "detail_stamp"], "the writer moves the current cycle's stamp"
    finally:
        cache.close()


def test_1050_identical_codex_rematerialization_keeps_stamp_and_digest(
        tmp_path, monkeypatch):
    """#1050 §8.3. Detects: churn on unchanged state — a full re-projection
    that rewrites identical rows moving `codex_stats_digest` (and with it
    `codex_version` and every Codex stamp), or the stamp itself, so an idle
    store rebuilds and refetches on every projection pass."""
    import _lib_dashboard_sources as lds
    ns, _home = _1050_c_env(tmp_path, monkeypatch)
    cache = ns["open_cache_db"]()
    try:
        _1050_seed_codex(ns, cache, "base")

        def digest():
            conn = ns["open_db"]()
            try:
                return lds.codex_stats_digest(conn)
            finally:
                conn.close()

        digest_before = digest()
        before = _1050_c_assert_coherent(ns, (None,))
        _1050_c_rematerialize(ns, cache, force_full=True)
        assert digest() == digest_before
        assert _1050_c_assert_coherent(ns, (None,)) == before
    finally:
        cache.close()


def test_1050_window_spend_adoption_moves_the_codex_stamp(
        tmp_path, monkeypatch):
    """#1050 §4.7 (G6). Detects: window-scoped spend adoption, which stamps
    `codex_session_entries.account_key` in place without adding a row,
    leaving an account-focused Codex stamp unchanged while that account's
    detail starts pricing the adopted spend."""
    import _cctally_cache
    ns, _home = _1050_c_env(tmp_path, monkeypatch)
    cache = ns["open_cache_db"]()
    try:
        t0, t1, t2 = _1050_C_T
        cache.execute(
            "INSERT INTO codex_source_roots (source_root_key, "
            "canonical_root_path, first_seen_utc, last_seen_utc) "
            "VALUES (?, ?, ?, ?)",
            (_1050_C_ROOT, os.environ["CODEX_HOME"], t0.isoformat(),
             t2.isoformat()))
        _1050_c_ladder(cache, account=_1050_C_ACCT_A, path=_1050_C_PATH,
                       base_offset=10)
        for offset, timestamp in ((1, t0), (3, t1), (6, t2)):
            _1050_c_entry(cache, timestamp=timestamp, offset=offset,
                          tokens=1000 * offset, account=None)
        _1050_c_rematerialize(ns, cache)
        before = _1050_c_assert_coherent(ns, (_1050_C_ACCT_A,))[_1050_C_ACCT_A]
        unattributed = cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries "
            "WHERE account_key IS NULL").fetchone()[0]
        stamped = _cctally_cache.apply_codex_window_spend_adoption(
            cache, strict=True)
        cache.commit()
        assert stamped > 0 and cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries "
            "WHERE account_key = ?", (_1050_C_ACCT_A,)).fetchone()[0] == (
            unattributed), "adoption stamped the window's spend"
        after = _1050_c_assert_coherent(ns, (_1050_C_ACCT_A,))[_1050_C_ACCT_A]
        key = _1050_c_current_key(after)
        assert after[key]["detail_stamp"] != before[key]["detail_stamp"]
    finally:
        cache.close()


@pytest.mark.parametrize("change", ("multiplier_only", "alias_only"))
def test_1050_codex_pricing_identity(change):
    """#1050 §4.7 (H4). Detects: a Codex pricing identity built from the rate
    table alone, which keeps every Codex stamp when only a fast multiplier or
    an alias moves — both reprice the detail at read."""
    import _cctally_milestone_history as mh
    import _lib_pricing as lp
    live = lp.current_pricing_snapshot()
    fields = {name: getattr(live, name) for name in lp.PricingSnapshot.__slots__}
    assert lp.codex_pricing_identity(lp.PricingSnapshot(**fields)) == (
        lp.live_codex_pricing_identity()), "equal semantics hash equally"
    if change == "multiplier_only":
        fields["fast_multipliers"] = dict(
            live.fast_multipliers,
            codex_fallback=float(live.fast_multipliers["codex_fallback"]) + 1.0)
    else:
        fields["aliases"] = dict(live.aliases, **{"gpt-1050-alias": "gpt-5"})
    moved = lp.codex_pricing_identity(lp.PricingSnapshot(**fields))
    assert moved != lp.live_codex_pricing_identity()
    assert mh._codex_content_component("d", 1, "standard", moved) != (
        mh._codex_content_component(
            "d", 1, "standard", lp.live_codex_pricing_identity()))


def _1050_c_input_change(ns, cache, codex_home, monkeypatch, change):
    """Move exactly one cost input, leaving stats.db as it is."""
    t0 = _1050_C_T[0]
    if change == "first_boundary_append":
        # An earlier observation of the same weekly reset, below the first
        # threshold: the breakdown opens from it, and it is a pure append.
        _1050_c_snapshot(cache, captured=t0 - dt.timedelta(minutes=10),
                         window=10_080, percent=0.4, offset=5)
    elif change == "first_boundary_rewrite":
        cache.execute(
            "UPDATE quota_window_snapshots SET source_path = ?, "
            "line_offset = 15 WHERE logical_limit_key = ? AND line_offset = 10",
            (_1050_C_LATER_PATH, _1050_C_WEEK_KEY))
    elif change == "entry_selection":
        _1050_c_entry(cache, timestamp=t0 + dt.timedelta(minutes=15),
                      offset=5, tokens=32000)
    elif change == "accounting_counter":
        # Moves the accounting counter and no selected token or row count.
        cache.execute(
            "UPDATE codex_session_entries SET session_id = 'session-1050b' "
            "WHERE line_offset = 2")
    elif change == "speed":
        (codex_home / "config.toml").write_text(
            'service_tier = "fast"\n', encoding="utf-8")
    elif change == "pricing":
        import _lib_pricing as lp
        monkeypatch.setattr(lp, "live_codex_pricing_identity",
                            lambda: "1050-new-binary")
    else:
        raise AssertionError(change)
    cache.commit()


@pytest.mark.parametrize("change", (
    "first_boundary_append", "first_boundary_rewrite", "entry_selection",
    "accounting_counter", "speed", "pricing"))
def test_1050_codex_reuse_refused_per_input(tmp_path, monkeypatch, change):
    """#1050 §4.7. Detects: the index memo reusing a cycle's memoized stamp
    after one of its cost inputs moved with stats.db unchanged — a first
    boundary (appended or rewritten), an entry selection, the accounting
    counter, the resolved speed or the pricing identity — so the published
    stamp stays put while the detail route's moves."""
    ns, codex_home = _1050_c_env(tmp_path, monkeypatch)
    cache = ns["open_cache_db"]()
    try:
        _1050_seed_codex(ns, cache, "base")
        before = _1050_c_assert_coherent(ns, (None,))[None]
        stats_before = _1050_store_rows(_1050_stats_path())
        _1050_c_input_change(ns, cache, codex_home, monkeypatch, change)
        assert _1050_store_rows(_1050_stats_path()) == stats_before
        after = _1050_c_assert_coherent(ns, (None,))[None]
        key = _1050_c_current_key(after)
        assert after[key]["detail_stamp"] != before[key]["detail_stamp"], (
            f"{change} moves the current cycle's stamp")
    finally:
        cache.close()


_1050_C_COST_INPUT_SITES = (
    "_cctally_milestone_history._codex_cycle_inputs",
    "_cctally_milestone_history._CodexCycleInputsMemo.sync",
)


def test_1050_codex_cost_input_reads_stream_without_temp_btrees(
        tmp_path, monkeypatch):
    """#1050 §8.4. Detects: a Codex cost-input read (the ladders, the bulk
    first boundaries, the per-model entry aggregates, the memo's ledger and
    extent reads) that sorts, groups or materializes a history-sized input
    through a temp structure on every index build — the #901 I1 rule. Covers
    the memo's cold build and its incremental sync after an append and a
    late entry."""
    import _sql_plan_guard as guard
    ns, _home = _1050_c_env(tmp_path, monkeypatch)
    cache = ns["open_cache_db"]()
    try:
        _1050_seed_codex(ns, cache, "base")
        with guard.capture_sql_plans() as recorder:
            _1050_c_index(ns)
            _1050_c_snapshot(cache, captured=_1050_C_T[0] - dt.timedelta(
                minutes=10), window=10_080, percent=0.4, offset=5)
            _1050_c_entry(cache, timestamp=_1050_C_T[0] + dt.timedelta(
                minutes=15), offset=5, tokens=32000)
            cache.commit()
            _1050_c_index(ns)
        reads = [s for s in recorder.statements
                 if any(site in s.call_sites
                        for site in _1050_C_COST_INPUT_SITES)]
        assert any("codex_session_entries" in s.sql for s in reads)
        assert any("quota_window_snapshots" in s.sql for s in reads)
        offenders = [(s.sql, s.plan) for s in reads
                     if any(marker in detail for detail in s.plan
                            for marker in guard.TEMP_MARKERS)]
        assert offenders == []
    finally:
        cache.close()


@pytest.mark.parametrize("rewrite", ("milestone_boundary", "block_account_swap"))
def test_1050_codex_stats_only_rewrite_moves_digest_and_stamp(
        tmp_path, monkeypatch, rewrite):
    """#1050 §4.7 (G3). Detects: a stats-side Codex rewrite that no cache leg
    records — a milestone's accounting boundary moved, or two accounts' block
    `current_percent` values swapped — leaving `codex_stats_digest` (and with
    it `codex_version` and `_codex_quota_reuse_identity`) unchanged, so the
    prior Codex source and every memoized cycle stamp are reused while the
    detail route prices the new boundary or shows the swapped values."""
    import _lib_dashboard_sources as lds
    ns, _home = _1050_c_env(tmp_path, monkeypatch)
    swap = rewrite == "block_account_swap"
    accounts = (None, _1050_C_ACCT_A, _1050_C_ACCT_B) if swap else (None,)
    cache = ns["open_cache_db"]()
    try:
        _1050_seed_codex(ns, cache, "d_account_swap" if swap else "base")

        def digest():
            conn = ns["open_db"]()
            try:
                return lds.codex_stats_digest(conn)
            finally:
                conn.close()

        digest_before = digest()
        before = _1050_c_assert_coherent(ns, accounts)
        conn = ns["open_db"]()
        try:
            if swap:
                cur = conn.execute(
                    "UPDATE quota_window_blocks SET current_percent = CASE "
                    "account_key WHEN ? THEN 7.8 WHEN ? THEN 7.3 END "
                    "WHERE source='codex' AND window_minutes = 300",
                    (_1050_C_ACCT_A, _1050_C_ACCT_B))
            else:
                cur = conn.execute(
                    "UPDATE quota_percent_milestones SET source_path = ? "
                    "WHERE source='codex' AND window_minutes = 10080 "
                    "  AND percent_threshold = (SELECT MIN(percent_threshold) "
                    "    FROM quota_percent_milestones "
                    "    WHERE source='codex' AND window_minutes = 10080)",
                    (_1050_C_LATER_PATH,))
            assert cur.rowcount >= 1, "the stored rewrite applied"
            conn.commit()
        finally:
            conn.close()
        assert digest() != digest_before, "the rewrite moves the digest"
        after = _1050_c_assert_coherent(ns, accounts)
        assert any(
            after[a][k]["detail_stamp"] != before[a][k]["detail_stamp"]
            for a in accounts for k in after[a] if k in before[a]), (
            "the rewrite moves a Codex stamp")
    finally:
        cache.close()
