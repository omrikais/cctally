"""M5 Task 5.2 (#268, spec §7 / Codex F4) — orphan-prune cache invalidation.

An orphan prune (`_dashboard_self_heal_orphans` → `_prune_orphaned_cache_entries`)
DELETES `session_entries` / `session_files` rows in place. `MAX(session_entries.id)`
alone can't detect that — deleting a NON-max row leaves the max unchanged — so a
prune that actually removed rows must bump the cache-generation counter (a
composite-signature leg, so the next rebuild can't idle-short-circuit) AND clear
the Group A / session caches, forcing a correct cold recompute on the next tick.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import shutil

import pytest
from conftest import load_script, redirect_paths  # type: ignore


NOW_UTC = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)


def _line(session_id, uuid, msg_id, req_id, *, ts):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "parentUuid": None,
        "sessionId": session_id, "requestId": req_id, "timestamp": ts,
        "cwd": "/Users/u/proj",
        "message": {
            "role": "assistant", "id": msg_id, "model": "claude-opus-4-8",
            "usage": {"input_tokens": 100, "output_tokens": 40,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    return ns, tmp_path


def _sync(ns):
    conn = ns["open_cache_db"]()
    ns["sync_cache"](conn)
    conn.close()


def test_prune_of_non_max_row_bumps_generation_and_clears_caches(env):
    ns, tmp_path = env
    import _lib_snapshot_cache as sc

    home = pathlib.Path(os.environ["HOME"])
    # 1) Orphan session, ingested FIRST → its session_entries get the LOW ids.
    orphan_dir = home / ".claude" / "projects" / "-gone"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "s_orphan.jsonl").write_text(
        _line("S_ORPH", "uo", "mo", "ro", ts="2026-07-01T00:00:00Z")
    )
    _sync(ns)
    # 2) Survivor session, ingested SECOND → HIGHER ids. Stays on disk.
    keep_dir = home / ".claude" / "projects" / "-Users-u-proj"
    keep_dir.mkdir(parents=True, exist_ok=True)
    (keep_dir / "s_keep.jsonl").write_text(
        _line("S_KEEP", "uk", "mk", "rk", ts="2026-07-03T00:00:00Z")
    )
    _sync(ns)

    conn = ns["open_cache_db"]()
    max_before = conn.execute("SELECT MAX(id) FROM session_entries").fetchone()[0]
    conn.close()
    # Remove the orphan's transcript from disk so the prune can drop it.
    shutil.rmtree(orphan_dir)

    # Prime the module caches with sentinels so we can prove they get cleared.
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.group_a_cache().put("daily", "2026-07-01", object())
    sc.session_cache().put("sentinel", object())
    gen0 = sc.current_generation()

    res = ns["_dashboard_self_heal_orphans"](skip_sync=False)

    # The prune deleted the orphan (a non-max row) ...
    assert res is not None and res.pruned_files >= 1
    conn = ns["open_cache_db"]()
    max_after = conn.execute("SELECT MAX(id) FROM session_entries").fetchone()[0]
    conn.close()
    assert max_after == max_before, (
        "the survivor's id is the max both before and after — prune left MAX(id) "
        "unchanged, which is exactly why the generation bump is needed"
    )
    # ... so the generation advanced and BOTH caches were cleared (Codex F4).
    assert sc.current_generation() == gen0 + 1, (
        "an in-place deletion must bump the cache-generation counter"
    )
    assert sc.group_a_cache().get("daily", "2026-07-01") is None, (
        "the Group A cache must be cleared after a real prune"
    )
    assert sc.session_cache().get_all() == {}, (
        "the session cache must be cleared after a real prune"
    )


def test_prune_noop_does_not_bump_generation(env):
    """No orphan on disk → the prune deletes nothing → no generation bump, caches
    untouched (non-vacuity: the invalidation fires ONLY on a real deletion)."""
    ns, tmp_path = env
    import _lib_snapshot_cache as sc

    home = pathlib.Path(os.environ["HOME"])
    keep_dir = home / ".claude" / "projects" / "-Users-u-proj"
    keep_dir.mkdir(parents=True, exist_ok=True)
    (keep_dir / "s_keep.jsonl").write_text(
        _line("S_KEEP", "uk", "mk", "rk", ts="2026-07-03T00:00:00Z")
    )
    _sync(ns)

    sc.reset_group_a_state()
    sc.session_cache().put("sentinel", object())
    gen0 = sc.current_generation()

    res = ns["_dashboard_self_heal_orphans"](skip_sync=False)

    assert res is not None and res.pruned_files == 0
    assert sc.current_generation() == gen0, "a no-op prune must not bump generation"
    assert "sentinel" in sc.session_cache().get_all(), (
        "a no-op prune must not clear the caches"
    )


def test_prune_clears_weekref_cost_cache(env):
    """#269 M3.2 (spec §6): a real prune (non-max deletion) must ALSO clear the
    shared per-weekref immutable-cost cache. A prune deletes session_entries
    possibly WITHOUT lowering MAX(id), so the reconcile's max-id-regression check
    can't catch it — the explicit prune-site clear must."""
    ns, tmp_path = env
    import _lib_snapshot_cache as sc

    home = pathlib.Path(os.environ["HOME"])
    orphan_dir = home / ".claude" / "projects" / "-gone"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "s_orphan.jsonl").write_text(
        _line("S_ORPH", "uo", "mo", "ro", ts="2026-07-01T00:00:00Z")
    )
    _sync(ns)
    keep_dir = home / ".claude" / "projects" / "-Users-u-proj"
    keep_dir.mkdir(parents=True, exist_ok=True)
    (keep_dir / "s_keep.jsonl").write_text(
        _line("S_KEEP", "uk", "mk", "rk", ts="2026-07-03T00:00:00Z")
    )
    _sync(ns)
    shutil.rmtree(orphan_dir)

    # Prime the weekref cache + its watermark with sentinels so we can prove the
    # prune clears them.
    sc.reset_weekref_cost_state()
    sc._WEEKREF_COST_CACHE[("s", "e")] = 1.23
    sc._WEEKREF_COST_LAST_SEEN["max_id"] = 999

    res = ns["_dashboard_self_heal_orphans"](skip_sync=False)
    assert res is not None and res.pruned_files >= 1

    assert sc._WEEKREF_COST_CACHE == {}, (
        "a real prune must clear the weekref-cost cache (a non-max deletion the "
        "reconcile's max-id regression check cannot catch)"
    )
    assert sc._WEEKREF_COST_LAST_SEEN == {}, (
        "the prune-site clear must also reset the weekref watermark"
    )


def test_prune_noop_does_not_clear_weekref_cache(env):
    """Non-vacuity: a no-op prune (nothing deleted) must NOT clear the weekref
    cache — the clear fires ONLY on a real deletion."""
    ns, tmp_path = env
    import _lib_snapshot_cache as sc

    home = pathlib.Path(os.environ["HOME"])
    keep_dir = home / ".claude" / "projects" / "-Users-u-proj"
    keep_dir.mkdir(parents=True, exist_ok=True)
    (keep_dir / "s_keep.jsonl").write_text(
        _line("S_KEEP", "uk", "mk", "rk", ts="2026-07-03T00:00:00Z")
    )
    _sync(ns)

    sc.reset_weekref_cost_state()
    sc._WEEKREF_COST_CACHE[("s", "e")] = 4.56

    res = ns["_dashboard_self_heal_orphans"](skip_sync=False)
    assert res is not None and res.pruned_files == 0
    assert sc._WEEKREF_COST_CACHE == {("s", "e"): 4.56}, (
        "a no-op prune must not clear the weekref cache"
    )


def test_prune_then_rebuild_recomputes_correctly(env):
    """After a prune clears the caches, the next sessions rebuild recomputes cold
    from the POST-prune DB — the survivor is present, the pruned orphan is gone,
    and the cached path equals from-scratch."""
    ns, tmp_path = env
    import _lib_snapshot_cache as sc

    home = pathlib.Path(os.environ["HOME"])
    orphan_dir = home / ".claude" / "projects" / "-gone"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "s_orphan.jsonl").write_text(
        _line("S_ORPH", "uo", "mo", "ro", ts="2026-07-01T00:00:00Z")
    )
    _sync(ns)
    keep_dir = home / ".claude" / "projects" / "-Users-u-proj"
    keep_dir.mkdir(parents=True, exist_ok=True)
    (keep_dir / "s_keep.jsonl").write_text(
        _line("S_KEEP", "uk", "mk", "rk", ts="2026-07-03T00:00:00Z")
    )
    _sync(ns)
    shutil.rmtree(orphan_dir)

    ns["_dashboard_self_heal_orphans"](skip_sync=False)

    # Cached rebuild on the post-prune DB.
    cached = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True, use_session_cache=True)
    cached_ids = {r.session_id for r in cached}
    assert "S_KEEP" in cached_ids
    assert "S_ORPH" not in cached_ids
    # And it matches the from-scratch path (cache was cleared → cold recompute).
    tui = __import__("sys").modules["_cctally_tui"]
    sc.reset_session_cache_state()
    prev = getattr(tui, "_SESSION_CACHE_ENABLED", True)
    tui._SESSION_CACHE_ENABLED = False
    try:
        wide = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True, use_session_cache=True)
    finally:
        tui._SESSION_CACHE_ENABLED = prev
    assert cached == wide


def test_prune_clears_projects_env_cache(env):
    """#269 M4.5 (spec §14 Win 2): a real prune (non-max deletion) must ALSO
    clear the projects-envelope per-(project, week) cache — the same non-max
    deletion the reconcile's max-id-regression check cannot catch."""
    ns, tmp_path = env
    import _lib_snapshot_cache as sc

    home = pathlib.Path(os.environ["HOME"])
    orphan_dir = home / ".claude" / "projects" / "-gone"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "s_orphan.jsonl").write_text(
        _line("S_ORPH", "uo", "mo", "ro", ts="2026-07-01T00:00:00Z")
    )
    _sync(ns)
    keep_dir = home / ".claude" / "projects" / "-Users-u-proj"
    keep_dir.mkdir(parents=True, exist_ok=True)
    (keep_dir / "s_keep.jsonl").write_text(
        _line("S_KEEP", "uk", "mk", "rk", ts="2026-07-03T00:00:00Z")
    )
    _sync(ns)
    shutil.rmtree(orphan_dir)

    # Prime the envelope week cache + registry + watermark with sentinels.
    sc.reset_projects_env_state()
    sc._PROJECTS_ENV_WEEK_CACHE[("/p", "wk")] = ("agg",)
    sc._PROJECTS_ENV_WEEK_TOTALS["wk"] = 1.0
    sc._PROJECTS_ENV_LAST_SEEN["max_id"] = 999

    res = ns["_dashboard_self_heal_orphans"](skip_sync=False)
    assert res is not None and res.pruned_files >= 1

    assert sc._PROJECTS_ENV_WEEK_CACHE == {}, "prune must clear the envelope cache"
    assert sc._PROJECTS_ENV_WEEK_TOTALS == {}, "prune must clear the week totals"
    assert sc._PROJECTS_ENV_LAST_SEEN == {}, "prune must reset the envelope watermark"


def test_prune_noop_does_not_clear_projects_env_cache(env):
    """Non-vacuity: a no-op prune (nothing deleted) must NOT clear the envelope
    cache — the clear fires ONLY on a real deletion."""
    ns, tmp_path = env
    import _lib_snapshot_cache as sc

    home = pathlib.Path(os.environ["HOME"])
    keep_dir = home / ".claude" / "projects" / "-Users-u-proj"
    keep_dir.mkdir(parents=True, exist_ok=True)
    (keep_dir / "s_keep.jsonl").write_text(
        _line("S_KEEP", "uk", "mk", "rk", ts="2026-07-03T00:00:00Z")
    )
    _sync(ns)

    sc.reset_projects_env_state()
    sc._PROJECTS_ENV_WEEK_TOTALS["wk"] = 4.56

    res = ns["_dashboard_self_heal_orphans"](skip_sync=False)
    assert res is not None and res.pruned_files == 0
    assert sc._PROJECTS_ENV_WEEK_TOTALS == {"wk": 4.56}, (
        "a no-op prune must not clear the envelope cache"
    )
