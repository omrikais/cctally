"""Ancient-DB → head long-haul + mid-era FTS interaction tests (#279 S7 W2).

The per-migration goldens pin each handler in isolation; this module is the
only end-to-end net that flows a HISTORICAL DB shape through the WHOLE dispatcher
chain, catching migration *interaction* bugs (e.g. 010 search-split → 016
drop-aux → 018 title-fts, or a 5h-backfill / recompute-gate straddle) that no
single-migration golden can.

Gate-honest flow (spec W2): the frozen SQLite fixtures alone can NEVER bring
stats 008-010 past the recompute gate, because cache 001 WIPES every seeded
``session_entries`` + the walk-complete sentinel, and the gate then requires a
completed walk + non-empty entries. So the test ships a small deterministic
synthetic JSONL corpus and runs a REAL ``sync_cache`` over it after the wipe.

Isolation via ``redirect_paths`` (HOME → tmp, cache/stats/logs → tmp share);
the #190 autouse prod-log guard asserts the real prod migration-errors.log is
untouched at teardown.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script, redirect_paths


ANCIENT = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "ancient"
)


def _stage(ns, tmp_path, *, cache_name: str):
    """Copy an ancient stats + a named cache fixture + the corpus into the
    redirected HOME and return the core module for path reads."""
    import _cctally_core as core
    shutil.copy(ANCIENT / "stats.sqlite", core.DB_PATH)
    shutil.copy(ANCIENT / cache_name, core.CACHE_DB_PATH)
    # Corpus → tmp/.claude/projects/ (redirect_paths deleted CLAUDE_CONFIG_DIR,
    # so _get_claude_data_dirs resolves HOME/.claude).
    dest_projects = tmp_path / ".claude" / "projects"
    dest_projects.mkdir(parents=True, exist_ok=True)
    src_projects = ANCIENT / "corpus" / "projects"
    for sub in src_projects.iterdir():
        shutil.copytree(sub, dest_projects / sub.name, dirs_exist_ok=True)
    return core


def _markers(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    finally:
        conn.close()


def _user_version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _cache_meta(conn, key):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def test_ancient_stats_and_cache_reach_head(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = _stage(ns, tmp_path, cache_name="cache.sqlite")

    # Pre-state sanity: pre-framework (no schema_migrations), duplicate entries.
    assert _user_version(core.DB_PATH) == 0
    assert _user_version(core.CACHE_DB_PATH) == 0
    pre_cache = sqlite3.connect(core.CACHE_DB_PATH)
    assert pre_cache.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == 2
    pre_cache.close()

    # ── 1. open cache: dispatcher upgrades legacy schema, cache 001 WIPES the
    #      duplicate entries (D1 upgrade path) and stamps the chain.
    cache_conn = ns["open_cache_db"]()
    try:
        assert cache_conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0] == 0, "cache 001 must wipe the seeded duplicate entries"
        assert _cache_meta(cache_conn, "claude_ingest_walk_complete") is None, (
            "cache 001 must clear/leave-absent the walk-complete sentinel"
        )

        # ── 2. real sync_cache over the synthetic corpus repopulates entries
        #      (dedup: the streaming pair collapses to the higher-token row) +
        #      writes the walk-complete sentinel.
        ns["sync_cache"](cache_conn)
        n_entries = cache_conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0]
        assert n_entries >= 1, "sync_cache must repopulate entries from the corpus"
        assert _cache_meta(cache_conn, "claude_ingest_walk_complete") is not None, (
            "a clean walk must write the walk-complete sentinel (recompute gate)"
        )
        # Dedup happened: the intermediate output=1 row lost to the final=1000.
        max_out = cache_conn.execute(
            "SELECT MAX(output_tokens) FROM session_entries"
        ).fetchone()[0]
        assert max_out == 1000, "dedup must keep the higher-token final row"
    finally:
        cache_conn.close()

    # ── 3. open stats: with the gate satisfied, the whole 12-migration chain
    #      runs. Iterate a few opens to absorb any transient deferral. DB journal
    #      redesign §8: once the legacy dispatcher reaches the export baseline
    #      (head 13), ``open_db`` CUTS THE DB OVER to STATS_INDEX_EPOCH — so the
    #      "reached head" signal for stats.db is the epoch, not len(registry). A
    #      still-deferred recompute leaves user_version < 13 and NO cutover (spec
    #      §8 step 1: never journal a pre-recompute shape), so the epoch value is
    #      itself the "all migrations applied then cut over" proof.
    _epoch = core.STATS_INDEX_EPOCH
    for _ in range(4):
        conn = ns["open_db"]()
        conn.close()
        if _user_version(core.DB_PATH) == _epoch:
            break
    # And re-open cache once more in case a stats-side eager step left work.
    for _ in range(2):
        c = ns["open_cache_db"]()
        c.close()
        if _user_version(core.CACHE_DB_PATH) == len(ns["_CACHE_MIGRATIONS"]):
            break

    stats_names = {m.name for m in ns["_STATS_MIGRATIONS"]}
    cache_names = {m.name for m in ns["_CACHE_MIGRATIONS"]}

    # stats.db cut over to the epoch (its "at head" signal); cache.db at head.
    assert _user_version(core.DB_PATH) == _epoch, (
        f"stats.db not cut over to epoch {_epoch}: uv={_user_version(core.DB_PATH)} "
        f"markers={sorted(_markers(core.DB_PATH))}"
    )
    assert _user_version(core.CACHE_DB_PATH) == len(cache_names), (
        f"cache.db not at head: uv={_user_version(core.CACHE_DB_PATH)}"
    )

    # Every registered migration stamped (no injected test-only entries here).
    assert _markers(core.DB_PATH) == stats_names
    assert _markers(core.CACHE_DB_PATH) == cache_names

    # The three recompute markers are APPLIED (not silently deferred — a
    # deferred gate that still passed the version assertion is the failure mode).
    for m in (
        "008_recompute_weekly_cost_snapshots_dedup_fix",
        "009_recompute_five_hour_blocks_dedup_fix",
        "010_recompute_percent_milestones_dedup_fix",
    ):
        assert m in _markers(core.DB_PATH), f"recompute migration {m} not applied"

    # ── Data invariants (spot-checks) ──
    conn = sqlite3.connect(core.DB_PATH)
    try:
        cols = lambda t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        # 005/006/007 column adds.
        assert "reset_event_id" in cols("percent_milestones")
        assert "reset_event_id" in cols("five_hour_milestones")
        assert "observed_pre_credit_pct" in cols("week_reset_events")
        # 003 merged the jitter-forked duplicate blocks into one.
        assert conn.execute("SELECT COUNT(*) FROM five_hour_blocks").fetchone()[0] == 1
        # 012 unified the budget tables (vendor col present, Codex table gone).
        assert "vendor" in cols("budget_milestones")
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='codex_budget_milestones'"
        ).fetchone()[0] == 0
    finally:
        conn.close()

    # cache 020's physical-unique index exists at head.
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert any("session_entries" in n and "unique" in n.lower() for n in idx) or \
            any("physical" in n.lower() for n in idx), (
            f"cache 020 physical-unique index missing; indexes={sorted(idx)}"
        )
    finally:
        conn.close()


def test_midera_cache_fts_split_drop_and_title_arm(monkeypatch, tmp_path):
    """The ancient pre-001 cache can't exercise 010→016→018 (production applies
    the CURRENT schema before dispatch, on which 010 merely arms a flag, 016
    no-ops without search_aux, and 018 sets a flag). The mid-era fixture carries
    the LEGACY unsplit FTS + a populated search_aux, so open → sync → reopen
    drives the real index split, the search_aux DROP, and the title-FTS arming."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = _stage(ns, tmp_path, cache_name="cache-midera.sqlite")

    def _has_col(path, col):
        conn = sqlite3.connect(path)
        try:
            return col in {r[1] for r in conn.execute(
                "PRAGMA table_info(conversation_messages)")}
        finally:
            conn.close()

    # Pre-state: legacy shape carries search_aux + the aux FTS shadow.
    assert _has_col(core.CACHE_DB_PATH, "search_aux"), "mid-era must start WITH search_aux"

    # Drive: open (010 arms the split), sync (consumes split → 011-020 including
    # 016's search_aux DROP + 018's title-FTS), a few reopen/sync passes to
    # absorb the multi-stage consume.
    for _ in range(4):
        conn = ns["open_cache_db"]()
        try:
            ns["sync_cache"](conn)
        finally:
            conn.close()
        if _user_version(core.CACHE_DB_PATH) == len(ns["_CACHE_MIGRATIONS"]):
            break

    # At head.
    assert _user_version(core.CACHE_DB_PATH) == len(ns["_CACHE_MIGRATIONS"]), (
        f"mid-era cache not at head: markers={sorted(_markers(core.CACHE_DB_PATH))}"
    )
    # 016 dropped the dead search_aux column (the real DROP path, not a no-op).
    assert not _has_col(core.CACHE_DB_PATH, "search_aux"), (
        "016 must DROP search_aux once the split is consumed"
    )
    # 018 armed / created the title FTS.
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        objs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert any("title" in n and "fts" in n for n in objs), (
            f"018 title-FTS not present; objects={sorted(objs)}"
        )
        # The split index landed (search_tool / search_thinking are live).
        fts_sql = " ".join(
            r[0] or "" for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql LIKE '%fts5%'")
        )
        assert "search_aux" not in fts_sql, "no FTS may still reference search_aux"
    finally:
        conn.close()
