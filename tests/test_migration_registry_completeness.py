"""Registry-completeness guard (#279 S7 W1).

Turns the former "per-migration goldens are lazy-adopted; not retroactively
backfilled" policy into an ENFORCED invariant, now that W3 has backfilled the
last 7 gaps:

  1. Registry counts are pinned (12 stats / 20 cache) with the test-injection
     env var (``CCTALLY_MIGRATION_TEST_MODE``) asserted ABSENT — when it is
     armed, ``bin/_cctally_db`` registers a REAL extra entry in each registry
     (``013_test_failure_injection`` / ``021_test_cache_migration``) that has no
     golden, so the guard must run against the clean 12/20 shape.
  2. Bijection: the set of migration names in BOTH registries equals the set of
     ``per-migration/<name>/`` golden dirs — no migration missing a golden, and
     no orphan golden dir. Each dir carries both ``pre.sqlite`` + ``post.sqlite``.
  3. Every migration maps (via the explicit MANIFEST below) to a golden test
     module that declares ``IDEMPOTENCY_COVERED = True`` — a structural marker
     (imported + asserted, NOT a source-text grep), because the 25 pre-existing
     golden modules use THREE different idempotency-test names
     (``test_migration_handler_idempotent_against_marker`` /
     ``test_handler_is_idempotent_on_rerun`` / an inline second invocation), so
     a single-name grep would be vacuous. Standardizing those names is out of
     scope (churn).

Adding a migration? Ship its ``build_per_migration_*`` (or hand-built) golden
dir AND its golden test module (with ``IDEMPOTENCY_COVERED = True``) AND a
MANIFEST row AND bump the pinned count below. The guard fails loudly on any
omission.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from conftest import load_script


PER_MIGRATION_ROOT = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
)

# Pinned registry sizes. Bump BOTH when a migration ships (see module docstring).
EXPECTED_STATS_COUNT = 12
EXPECTED_CACHE_COUNT = 20

# migration name -> its per-migration golden TEST MODULE (stem). The module must
# declare ``IDEMPOTENCY_COVERED = True``. The historical mixed naming is why this
# is explicit rather than derived: cache 001-007 + stats 005/006/008-012 use
# ``test_migration_NNN_*``; cache 008-020 use ``test_cache_migration_NNN_*``; the
# W3 backfill uses ``test_stats_migration_NNN_*`` (stats 001-004/007) and
# ``test_cache_migration_00{5,6}_*`` (cache 005/006).
MANIFEST = {
    # ── stats registry ──
    "001_five_hour_block_models_backfill_v1": "test_stats_migration_001_per_migration_goldens",
    "002_five_hour_block_projects_backfill_v1": "test_stats_migration_002_per_migration_goldens",
    "003_merge_5h_block_duplicates_v1": "test_stats_migration_003_per_migration_goldens",
    "004_heal_forked_week_start_date_buckets": "test_stats_migration_004_per_migration_goldens",
    "005_percent_milestones_reset_event_id": "test_migration_005_per_migration_goldens",
    "006_five_hour_milestones_reset_event_id": "test_migration_006_per_migration_goldens",
    "007_observed_pre_credit_pct": "test_stats_migration_007_per_migration_goldens",
    "008_recompute_weekly_cost_snapshots_dedup_fix": "test_migration_008_per_migration_goldens",
    "009_recompute_five_hour_blocks_dedup_fix": "test_migration_009_per_migration_goldens",
    "010_recompute_percent_milestones_dedup_fix": "test_migration_010_per_migration_goldens",
    "011_budget_milestone_period_keys": "test_migration_011_per_migration_goldens",
    "012_unify_budget_milestones_vendor": "test_migration_012_per_migration_goldens",
    # ── cache registry ──
    "001_dedup_highest_wins": "test_migration_001_per_migration_goldens",
    "002_conversation_messages_backfill": "test_migration_002_per_migration_goldens",
    "003_conversation_reingest_tool_ids": "test_migration_003_per_migration_goldens",
    "004_conversation_reingest_subagent_kind": "test_migration_004_per_migration_goldens",
    "005_conversation_reingest_meta": "test_cache_migration_005_per_migration_goldens",
    "006_conversation_reingest_source_tool_use_id": "test_cache_migration_006_per_migration_goldens",
    "007_conversation_reingest_enrichment": "test_migration_007_per_migration_goldens",
    "008_session_entries_speed_backfill": "test_cache_migration_008_per_migration_goldens",
    "009_conversation_media_reingest": "test_cache_migration_009_per_migration_goldens",
    "010_conversation_search_split": "test_cache_migration_010_per_migration_goldens",
    "011_conversation_promote_command_args": "test_cache_migration_011_per_migration_goldens",
    "012_create_conversation_ai_titles": "test_cache_migration_012_per_migration_goldens",
    "013_create_conversation_sessions": "test_cache_migration_013_per_migration_goldens",
    "014_conversation_queued_prompt_reingest": "test_cache_migration_014_per_migration_goldens",
    "015_conversation_sessions_filter_columns": "test_cache_migration_015_per_migration_goldens",
    "016_drop_search_aux": "test_cache_migration_016_per_migration_goldens",
    "017_arm_nested_agent_reingest": "test_cache_migration_017_per_migration_goldens",
    "018_create_conversation_title_fts": "test_cache_migration_018_per_migration_goldens",
    "019_create_conversation_file_touches": "test_cache_migration_019_per_migration_goldens",
    "020_session_entries_physical_unique": "test_cache_migration_020_per_migration_goldens",
}


def _registry_names():
    # The test-injection block registers a REAL 13th stats / 21st cache entry
    # when armed (bin/_cctally_db.py). Assert it is OFF so the registries hold
    # exactly the shippable 12/20 — else the guard would demand a golden for an
    # injected migration.
    assert os.environ.get("CCTALLY_MIGRATION_TEST_MODE") != "1", (
        "CCTALLY_MIGRATION_TEST_MODE must be unset for this guard — it injects "
        "extra, golden-less migrations into both registries."
    )
    ns = load_script()
    stats = [m.name for m in ns["_STATS_MIGRATIONS"]]
    cache = [m.name for m in ns["_CACHE_MIGRATIONS"]]
    return stats, cache


def test_registry_counts_are_pinned():
    stats, cache = _registry_names()
    assert len(stats) == EXPECTED_STATS_COUNT, (
        f"stats registry is {len(stats)}, expected {EXPECTED_STATS_COUNT}; a "
        f"new migration landed — bump EXPECTED_STATS_COUNT + add its golden + "
        f"MANIFEST row."
    )
    assert len(cache) == EXPECTED_CACHE_COUNT, (
        f"cache registry is {len(cache)}, expected {EXPECTED_CACHE_COUNT}; a "
        f"new migration landed — bump EXPECTED_CACHE_COUNT + add its golden + "
        f"MANIFEST row."
    )


def test_registry_golden_dir_bijection():
    """Every migration has a golden dir (pre+post); every golden dir maps back
    to a registered migration (no orphans)."""
    stats, cache = _registry_names()
    registry_names = set(stats) | set(cache)
    # stats and cache full names are disjoint (distinct descriptive suffixes).
    assert len(registry_names) == len(stats) + len(cache), (
        "a stats and a cache migration share a full name — unexpected collision"
    )
    golden_dirs = {p.name for p in PER_MIGRATION_ROOT.iterdir() if p.is_dir()}

    missing = registry_names - golden_dirs
    assert not missing, f"migrations without a per-migration golden dir: {sorted(missing)}"
    orphans = golden_dirs - registry_names
    assert not orphans, f"orphan golden dirs (no registered migration): {sorted(orphans)}"

    for name in sorted(registry_names):
        d = PER_MIGRATION_ROOT / name
        assert (d / "pre.sqlite").exists(), f"{name}: missing pre.sqlite"
        assert (d / "post.sqlite").exists(), f"{name}: missing post.sqlite"


def test_manifest_covers_every_migration():
    stats, cache = _registry_names()
    registry_names = set(stats) | set(cache)
    assert set(MANIFEST) == registry_names, (
        "MANIFEST is out of sync with the registries — "
        f"missing rows: {sorted(registry_names - set(MANIFEST))}; "
        f"stale rows: {sorted(set(MANIFEST) - registry_names)}"
    )


@pytest.mark.parametrize("migration,module_stem", sorted(MANIFEST.items()))
def test_golden_module_declares_idempotency_covered(migration, module_stem):
    """Each golden test module declares ``IDEMPOTENCY_COVERED = True`` — the
    structural marker that it exercises the handler's second-invocation no-op
    (three different test names are in use across the 25 historical modules, so
    a single-name grep would be vacuous)."""
    mod = importlib.import_module(module_stem)
    assert getattr(mod, "IDEMPOTENCY_COVERED", None) is True, (
        f"{module_stem} (golden for {migration}) must declare "
        f"IDEMPOTENCY_COVERED = True at module level"
    )
