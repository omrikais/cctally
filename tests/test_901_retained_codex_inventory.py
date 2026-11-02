"""Retained Codex identities: one collection per pass (#901 Amendment 20 W1)
read from a maintained inventory (#927, #981 Part B).

``sync_codex_cache``'s ordinary (non-targeted, non-rebuild) prune path needs
every retained Codex source identity twice: once to find the inactive orphans
and once to protect the paths whose prune it refuses. #901 W1 made it collect
them once. Each collection still ran four history-sized family scans, which on
the measured production-shaped store cost a cold hook 13.2-13.5 s against its
5 s budget (#927). Cache migration 051 adds ``codex_retained_identities``,
kept by INSERT and identity-UPDATE triggers, and the planner reads that table
once ``cache_meta.codex_identity_inventory`` is ``ready``.

(T-B1) Detects the hook's prune planning growing with history again: with the
    inventory ready, the planner's SQLite VM steps do not change when every
    identity carries 50x the child rows, and none of the child-family scans
    runs. On the base tree the steps grow with the rows and the trace shows
    the scans. A permanently pending inventory fails it (the hook would defer
    instead of planning).
(T-B2) Detects a protection or deletion change: over every adversarial shape
    the planner and drain have the same destructive effects with the
    inventory as with the legacy full scan (the oracle), stale inventory rows
    neither protect nor refuse anything and converge after the drain, an
    incomplete inventory defers the budgeted walk instead of reading as
    empty, and every writer keeps the inventory a superset.
(b) Equivalence: the plan equals a frozen copy of the pre-W1 code on
    adversarial data (relative paths, NULL root keys, a family lacking its
    terminal file row, a vanished root, duplicate identities across families,
    a configured but unrecognizable root, an unreadable parent directory), in
    both inventory states.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import shutil
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

ROLLOUTS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"

#: The retained-identity scans, by family, exactly as the product issues them.
FAMILY_SCANS = {
    "codex_session_files":
        "SELECT path, source_root_key FROM codex_session_files",
    "codex_session_entries":
        "SELECT source_path, source_root_key FROM codex_session_entries",
    "quota_window_snapshots":
        "SELECT source_path, source_root_key FROM quota_window_snapshots "
        "WHERE source = 'codex'",
    "codex_conversation_threads":
        "SELECT source_path, source_root_key FROM codex_conversation_threads",
    "codex_source_roots":
        "SELECT source_root_key FROM codex_source_roots",
}


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


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


# ── (T-B1) planning cost independent of child-row multiplicity ─────────────


def _seed(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    day = provider_root / "sessions" / "2026" / "08" / "03"
    day.mkdir(parents=True)
    rollout = day / "rollout.jsonl"
    shutil.copyfile(ROLLOUTS / "modern-full.jsonl", rollout)
    shutil.copyfile(ROLLOUTS / "modern-no-quota.jsonl", day / "second.jsonl")
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    cache = ns["open_cache_db"]()
    conversations = ns["open_conversations_db"]()
    try:
        ns["sync_codex_cache"](cache)
        ns["sync_codex_conversations"](conversations)
    finally:
        conversations.close()
    return ns, cache, rollout


def _multiply_child_rows(conn, factor: int) -> None:
    """Give every retained identity ``factor`` times its child rows, without
    adding an identity: copies at offsets past every real line."""
    for k in range(1, factor):
        shift = k * 1_000_000_000
        conn.execute(
            "INSERT INTO codex_session_entries (source_path, line_offset, "
            "timestamp_utc, session_id, model, source_root_key) "
            "SELECT source_path, line_offset + ?, timestamp_utc, session_id, "
            "model, source_root_key FROM codex_session_entries "
            "WHERE line_offset < 1000000000", (shift,))
        conn.execute(
            "INSERT INTO quota_window_snapshots (source, source_root_key, "
            "source_path, line_offset, captured_at_utc, logical_limit_key, "
            "window_minutes, used_percent, resets_at_utc) "
            "SELECT source, source_root_key, source_path, line_offset + ?, "
            "captured_at_utc, logical_limit_key, window_minutes, "
            "used_percent, resets_at_utc FROM quota_window_snapshots "
            "WHERE source = 'codex' AND line_offset < 1000000000", (shift,))
        conn.execute(
            "INSERT INTO codex_conversation_threads (conversation_key, "
            "source_root_key, native_thread_id, root_thread_id, source_path) "
            "SELECT conversation_key || '#mult' || ?, source_root_key, "
            "native_thread_id || '#mult' || ?, root_thread_id, source_path "
            "FROM codex_conversation_threads "
            "WHERE native_thread_id NOT LIKE '%#mult%'",
            (k, k))
    conn.commit()


def _hook_planning_cost(tmp_path, monkeypatch, factor):
    ns, cache, rollout = _seed(tmp_path, monkeypatch)
    import _cctally_cache as cache_mod
    try:
        _multiply_child_rows(cache, factor)
        rows = cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0]
        identities = cache.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT source_path, "
            "source_root_key FROM codex_session_entries)").fetchone()[0]
        real_plan = cache_mod._plan_ordinary_codex_prune
        measured = {"steps": 0, "calls": 0, "statements": []}

        def counted_plan(conn, *args, **kwargs):
            def step():
                measured["steps"] += 1
                return 0
            measured["calls"] += 1
            conn.set_progress_handler(step, 1)
            conn.set_trace_callback(measured["statements"].append)
            try:
                return real_plan(conn, *args, **kwargs)
            finally:
                conn.set_progress_handler(None, 0)
                conn.set_trace_callback(None)

        monkeypatch.setattr(cache_mod, "_plan_ordinary_codex_prune", counted_plan)
        # The hook's call (bin/_cctally_record.py, the Codex quota leg).
        stats = ns["sync_codex_cache"](
            cache,
            lock_timeout=0,
            budget_seconds=600.0,
            active_transcript_path=str(rollout),
            quota_reconcile="defer",
        )
        ready = getattr(cache_mod, "codex_identity_inventory_ready", None)
        return {
            **measured, "stats": stats, "rows": rows, "identities": identities,
            "ready": None if ready is None else ready(cache),
        }
    finally:
        cache.close()


def test_inventory_vm_steps_independent_of_child_multiplicity(
    tmp_path, monkeypatch,
):
    one = _hook_planning_cost(tmp_path / "x1", monkeypatch, 1)
    fifty = _hook_planning_cost(tmp_path / "x50", monkeypatch, 50)
    # The multiplied store really holds 50x the child rows for the same
    # identities, and the hook really planned its prune on both.
    assert fifty["identities"] == one["identities"] > 0
    assert fifty["rows"] == 50 * one["rows"]
    assert one["calls"] == fifty["calls"] == 1, (
        "the budgeted hook never planned its prune (a pending inventory "
        "defers the whole walk)")
    assert one["steps"] > 0
    # GREEN: the planner's VM work follows the identity count only.
    assert fifty["steps"] <= one["steps"] + one["steps"] // 10, (
        f"planning VM steps grew with child rows: x1={one['steps']} "
        f"x50={fifty['steps']}")
    child_scans = [
        _normalized(FAMILY_SCANS[family])
        for family in ("codex_session_entries", "quota_window_snapshots",
                       "codex_conversation_threads")
    ]
    seen = [statement for statement in fifty["statements"]
            if _normalized(statement) in child_scans]
    assert seen == [], f"history-family scans ran while planning: {seen}"
    for run in (one, fifty):
        assert not run["stats"].lock_contended
        assert run["stats"].deferred_reason is None
        assert run["stats"].prune_refused is False
        assert run["ready"] is True


# ── (b) equivalence with a frozen copy of the pre-W1 code ───────────────────
#
# Verbatim copies of `_collect_retained_codex_paths_and_roots`,
# `_collect_inactive_codex_paths_and_roots` and the ordinary prune path's
# decision sequence in `sync_codex_cache` as they stood at 3d757f7c3.


def _frozen_collect_retained_codex_paths_and_roots(conn):
    retained_identities: set[tuple[str, str | None]] = set()
    family_queries = (
        "SELECT path, source_root_key FROM codex_session_files",
        "SELECT source_path, source_root_key FROM codex_session_entries",
        "SELECT source_path, source_root_key FROM quota_window_snapshots "
        "WHERE source = 'codex'",
        "SELECT source_path, source_root_key FROM codex_conversation_threads",
    )
    for query in family_queries:
        for source_path, root_key in conn.execute(query):
            if not os.path.isabs(source_path):
                continue
            retained_identities.add((source_path, root_key))
    retained_root_keys = {
        root_key for _path, root_key in retained_identities if root_key is not None
    }
    retained_root_keys.update(
        root_key
        for (root_key,) in conn.execute(
            "SELECT source_root_key FROM codex_source_roots"
        )
    )
    return (
        sorted(retained_identities, key=lambda item: (item[0], item[1] or "")),
        retained_root_keys,
    )


def _frozen_collect_inactive_codex_paths_and_roots(
    conn, current_file_identities, active_root_keys,
):
    retained_identities, retained_root_keys = (
        _frozen_collect_retained_codex_paths_and_roots(conn)
    )
    current_paths = {path for path, _root_key in current_file_identities}
    terminal_file_identities = {
        (path, root_key)
        for path, root_key in conn.execute(
            "SELECT path, source_root_key FROM codex_session_files"
        )
    }
    stale_identities = {
        identity
        for identity in retained_identities
        if (
            identity not in current_file_identities
            and not (
                identity[0] in current_paths
                and identity in terminal_file_identities
            )
        )
    }
    stale_root_keys = {
        root_key
        for root_key in retained_root_keys
        if root_key not in active_root_keys
    }
    return sorted(stale_identities, key=lambda item: (item[0], item[1] or "")), stale_root_keys


def _frozen_ordinary_prune_plan(cache_mod, conn, current_file_identities,
                                active_root_keys, prune_scope):
    orphan_sources, orphan_root_keys = _frozen_collect_inactive_codex_paths_and_roots(
        conn, current_file_identities, active_root_keys,
    )
    (
        safe_sources,
        refused_orphan_sources,
        safe_root_keys,
        refused_orphan_root_keys,
    ) = cache_mod._partition_codex_prune_candidates(
        prune_scope, orphan_sources, orphan_root_keys
    )
    retained_sources, _retained_root_keys = (
        _frozen_collect_retained_codex_paths_and_roots(conn)
    )
    retained_source_root_keys = {
        root_key
        for _path, root_key in retained_sources
        if root_key is not None
    }
    (
        _safe_retained_sources,
        refused_sources,
        _safe_retained_roots,
        refused_root_keys,
    ) = cache_mod._partition_codex_prune_candidates(
        prune_scope, retained_sources, retained_source_root_keys
    )
    return {
        "orphan_sources": orphan_sources,
        "orphan_root_keys": orphan_root_keys,
        "safe_sources": safe_sources,
        "refused_orphan_sources": refused_orphan_sources,
        "safe_root_keys": safe_root_keys,
        "refused_orphan_root_keys": refused_orphan_root_keys,
        "retained_sources": retained_sources,
        "refused_sources": refused_sources,
        "refused_root_keys": refused_root_keys,
    }


NOW = "2026-08-03T00:00:00Z"
LIVE, GONE, UNREC, VANISHED = "rk-live", "rk-gone", "rk-unrec", "rk-vanished"


def _file(conn, path, root_key):
    conn.execute(
        "INSERT INTO codex_session_files (path, size_bytes, mtime_ns, "
        "last_byte_offset, last_ingested_at, source_root_key) "
        "VALUES (?, 1, 1, 1, ?, ?)",
        (path, NOW, root_key),
    )


def _entry(conn, path, root_key, offset):
    conn.execute(
        "INSERT INTO codex_session_entries (source_path, line_offset, "
        "timestamp_utc, session_id, model, source_root_key) "
        "VALUES (?, ?, ?, 'session', 'gpt-synthetic', ?)",
        (path, offset, NOW, root_key),
    )


def _quota(conn, source, path, root_key, offset):
    conn.execute(
        "INSERT INTO quota_window_snapshots (source, source_root_key, "
        "source_path, line_offset, captured_at_utc, logical_limit_key, "
        "window_minutes, used_percent, resets_at_utc) "
        "VALUES (?, ?, ?, ?, ?, 'primary', 300, 10.0, '2026-08-03T05:00:00Z')",
        (source, root_key, path, offset, NOW),
    )


def _thread(conn, path, root_key, key):
    conn.execute(
        "INSERT INTO codex_conversation_threads (conversation_key, "
        "source_root_key, native_thread_id, root_thread_id, source_path) "
        "VALUES (?, ?, ?, ?, ?)",
        (key, root_key, f"native-{key}", f"root-{key}", path),
    )


def _root(conn, root_key, path):
    conn.execute(
        "INSERT INTO codex_source_roots (source_root_key, canonical_root_path, "
        "first_seen_utc, last_seen_utc) VALUES (?, ?, ?, ?)",
        (root_key, path, NOW, NOW),
    )


@pytest.fixture(params=["inventory-ready", "inventory-bootstrap"])
def adversarial(request, tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_cache

    live = tmp_path / "live" / "sessions"
    live.mkdir(parents=True)
    gone = tmp_path / "gone" / "sessions"  # never created: a vanished root
    unrec = tmp_path / "unrec" / "sessions"
    unrec.mkdir(parents=True)
    paths = {
        # Present in every family under one identity (duplicates across
        # families), and currently discovered.
        "current": str(live / "current.jsonl"),
        # Children without a terminal file row; not discovered any more.
        "orphan_child": str(live / "orphan-child.jsonl"),
        # A child without a terminal row at a discovered path and root.
        "child_current": str(live / "child-current.jsonl"),
        # A child at a discovered path under a different root, no terminal row.
        "child_other_root": str(live / "child-other-root.jsonl"),
        # An old terminal file at a discovered path under another root
        # (requalification, never an orphan).
        "requalify": str(live / "requalify.jsonl"),
        # NULL root keys in the two families that admit them.
        "null_root": str(live / "null-root.jsonl"),
        # Under the vanished root only.
        "gone_a": str(gone / "a.jsonl"),
        "gone_b": str(gone / "b.jsonl"),
        # Under the configured but unrecognizable root.
        "unrec": str(unrec / "u.jsonl"),
        # Under the recognized root, parent directory missing.
        "no_parent": str(live / "missing-dir" / "p.jsonl"),
        # Relative fixture rows: never pruned, never retained.
        "relative": "relative/fixture.jsonl",
        "relative_null": "relative/null.jsonl",
    }
    conn = ns["open_cache_db"]()
    _root(conn, LIVE, str(live.parent))
    _root(conn, GONE, str(gone.parent))
    _root(conn, UNREC, str(unrec.parent))
    _root(conn, VANISHED, str(tmp_path / "vanished"))  # no child anywhere
    # Duplicate identity across all four families.
    _file(conn, paths["current"], LIVE)
    _entry(conn, paths["current"], LIVE, 1)
    _quota(conn, "codex", paths["current"], LIVE, 1)
    _thread(conn, paths["current"], LIVE, "conv-current")
    # Families lacking their terminal row.
    _entry(conn, paths["orphan_child"], LIVE, 1)
    _quota(conn, "codex", paths["orphan_child"], LIVE, 1)
    _thread(conn, paths["orphan_child"], LIVE, "conv-orphan")
    _entry(conn, paths["child_current"], LIVE, 1)
    _entry(conn, paths["child_other_root"], GONE, 1)
    # Old terminal identity at a discovered path.
    _file(conn, paths["requalify"], GONE)
    _entry(conn, paths["requalify"], GONE, 1)
    # NULL root keys.
    _file(conn, paths["null_root"], None)
    _entry(conn, paths["null_root"], None, 1)
    _entry(conn, paths["null_root"], LIVE, 2)
    # Vanished root.
    _file(conn, paths["gone_a"], GONE)
    _quota(conn, "codex", paths["gone_a"], GONE, 1)
    _thread(conn, paths["gone_b"], GONE, "conv-gone")
    _quota(conn, "claude", paths["gone_b"], None, 1)  # not a Codex row
    # Unrecognizable configured root; missing parent under a recognized one.
    _file(conn, paths["unrec"], UNREC)
    _entry(conn, paths["no_parent"], LIVE, 1)
    # Relative fixture rows in every family that admits them.
    _file(conn, paths["relative"], LIVE)
    _file(conn, paths["relative_null"], None)
    _entry(conn, paths["relative"], None, 1)
    _quota(conn, "codex", paths["relative"], LIVE, 1)
    _thread(conn, paths["relative"], LIVE, "conv-relative")
    if request.param == "inventory-bootstrap":
        # An upgraded store before its bootstrap: the legacy scans decide.
        conn.execute(
            "UPDATE cache_meta SET value='bootstrap:files:0' "
            "WHERE key='codex_identity_inventory'")
        conn.execute("DELETE FROM codex_retained_identities")
    conn.commit()
    current_file_identities = {
        (paths["current"], LIVE),
        (paths["child_current"], LIVE),
        (paths["child_other_root"], LIVE),
        (paths["requalify"], LIVE),
    }
    active_root_keys = {LIVE}
    try:
        yield _cctally_cache, conn, current_file_identities, active_root_keys
    finally:
        conn.close()


SCOPES = {
    "recognized-live": ({LIVE, UNREC}, {LIVE}),
    "nothing-recognized": ({LIVE, UNREC}, set()),
    "everything-recognized": ({LIVE, UNREC, GONE}, {LIVE, UNREC, GONE}),
}


@pytest.mark.parametrize("scope_name", sorted(SCOPES))
def test_b_single_collection_plan_equals_the_frozen_two_collection_plan(
    adversarial, scope_name,
):
    cache_mod, conn, current, active = adversarial
    configured, recognized = SCOPES[scope_name]
    scope = cache_mod._CodexPruneScope(
        configured_root_keys=frozenset(configured),
        recognized_root_keys=frozenset(recognized),
    )
    expected = _frozen_ordinary_prune_plan(
        cache_mod, conn, current, active, scope)
    plan = cache_mod._plan_ordinary_codex_prune(conn, current, active, scope)
    actual = {name: getattr(plan, name) for name in expected}
    assert actual == expected
    # The data is adversarial on every axis the plan decides: something is
    # orphaned, something is refused, and something is safe in this scope.
    assert expected["orphan_sources"]
    assert expected["retained_sources"]
    if recognized:
        assert expected["safe_sources"]
    assert expected["refused_sources"]


def test_b_collectors_equal_their_frozen_copies(adversarial):
    cache_mod, conn, current, active = adversarial
    assert cache_mod._collect_retained_codex_paths_and_roots(conn) == (
        _frozen_collect_retained_codex_paths_and_roots(conn)
    )
    assert cache_mod._collect_inactive_codex_paths_and_roots(
        conn, current, active,
    ) == _frozen_collect_inactive_codex_paths_and_roots(conn, current, active)
    inventory = cache_mod._collect_retained_codex_inventory(conn)
    assert cache_mod._collect_inactive_codex_paths_and_roots(
        conn, current, active, inventory=inventory,
    ) == _frozen_collect_inactive_codex_paths_and_roots(conn, current, active)
    # The relative-path exclusion holds and the terminal set keeps every row.
    retained, root_keys = _frozen_collect_retained_codex_paths_and_roots(conn)
    assert all(os.path.isabs(path) for path, _root in retained)
    assert VANISHED in root_keys
    assert inventory.terminal_file_identities == {
        (path, root_key)
        for path, root_key in conn.execute(
            "SELECT path, source_root_key FROM codex_session_files")
    }


# ── (T-B2) protection and destructive effects vs the legacy full scan ───────
#
# Each case builds one store on a real provider tree, snapshots the data dir,
# and runs sync + prune drain twice from that snapshot: once as shipped and
# once with the retained collection replaced by the legacy four-family scan
# (the oracle). Same absolute paths and root keys in both, so the effects
# compare directly: which family rows survive, whether the walk refused, and
# which roots remain.

_ORACLE_CHILD_SCANS = (
    "SELECT source_path, source_root_key FROM codex_session_entries",
    "SELECT source_path, source_root_key FROM quota_window_snapshots "
    "WHERE source = 'codex'",
    "SELECT source_path, source_root_key FROM codex_conversation_threads",
)


def _legacy_identities(conn, *, relative=False):
    identities = set()
    for sql in (FAMILY_SCANS["codex_session_files"], *_ORACLE_CHILD_SCANS):
        for path, root_key in conn.execute(sql):
            if relative or os.path.isabs(path):
                identities.add((path, root_key))
    return identities


def _legacy_collector(cache_mod):
    def collect(conn):
        terminal = set(conn.execute(FAMILY_SCANS["codex_session_files"]))
        identities = _legacy_identities(conn)
        root_keys = {root for _path, root in identities if root is not None}
        root_keys |= {row[0] for row in conn.execute(
            FAMILY_SCANS["codex_source_roots"])}
        return cache_mod._RetainedCodexInventory(
            identities=tuple(sorted(
                identities, key=lambda item: (item[0], item[1] or ""))),
            root_keys=frozenset(root_keys),
            terminal_file_identities=frozenset(terminal),
        )
    return collect


def _inventory(conn):
    return set(conn.execute(
        "SELECT source_path, source_root_key FROM codex_retained_identities"))


def _family_effects(conn):
    return {
        table: sorted(conn.execute(
            f"SELECT {path}, source_root_key, COUNT(*) FROM {table} "
            f"WHERE 1{extra} GROUP BY 1, 2 ORDER BY 1, 2"), key=repr)
        for table, path, extra in (
            ("codex_session_files", "path", ""),
            ("codex_session_entries", "source_path", ""),
            ("quota_window_snapshots", "source_path", " AND source='codex'"),
            ("codex_conversation_threads", "source_path", ""),
        )
    }


class _Store:
    """One case's provider tree, data dir and seeded cache."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.mp = monkeypatch
        self.data = tmp_path / "data"
        self.live = tmp_path / "live"
        self.day = self.live / "sessions" / "2026" / "08" / "03"
        self.day.mkdir(parents=True)
        shutil.copyfile(ROLLOUTS / "modern-full.jsonl", self.day / "rollout.jsonl")
        shutil.copyfile(ROLLOUTS / "modern-no-quota.jsonl",
                        self.day / "second.jsonl")
        # A configured root with no recognizable Codex data: protected.
        self.unrec = tmp_path / "unrec"
        (self.unrec / "sessions").mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", f"{self.live},{self.unrec}")
        ns, cache_mod = self.load()
        conn = ns["open_cache_db"]()
        try:
            ns["sync_codex_cache"](conn, maintenance="caller")
            self.live_key = conn.execute(
                "SELECT source_root_key FROM codex_session_files "
                "WHERE path=?", (str(self.day / "rollout.jsonl"),)).fetchone()[0]
        finally:
            conn.close()
        identity = importlib.import_module("_lib_source_identity")
        self.unrec_key = identity.source_root_key(
            str(cache_mod._canonical_codex_path(self.unrec)))

    def load(self):
        ns = load_script()
        redirect_paths(ns, self.mp, self.data)
        import _cctally_cache as cache_mod
        return ns, cache_mod

    def path(self, *parts):
        return str(self.day.joinpath(*parts))

    def snapshot(self):
        self.snap = self.tmp / "snapshot"
        shutil.copytree(self.data, self.snap)

    def restore(self):
        shutil.rmtree(self.data)
        shutil.copytree(self.snap, self.data)

    def run(self, *, oracle):
        """Restore the snapshot, then sync and drain; return the effects."""
        self.restore()
        ns, cache_mod = self.load()
        if oracle:
            self.mp.setattr(cache_mod, "_collect_retained_codex_inventory",
                            _legacy_collector(cache_mod))
        conn = ns["open_cache_db"]()
        try:
            stats = ns["sync_codex_cache"](conn, maintenance="caller")
            after_sync = _family_effects(conn)
            refusal = conn.execute(
                "SELECT 1 FROM cache_meta WHERE key=?",
                (cache_mod.CODEX_ORPHAN_PRUNE_REFUSED_KEY,)).fetchone()
        finally:
            conn.close()
        cache_mod.run_codex_prune_drain(wait_for_requests=False)
        conn = ns["open_cache_db"]()
        try:
            effects = {
                "after_sync": after_sync,
                "prune_refused": stats.prune_refused,
                "prune_refused_files": stats.prune_refused_files,
                "refusal_recorded": refusal is not None,
                "after_drain": _family_effects(conn),
                "roots": sorted(r[0] for r in conn.execute(
                    FAMILY_SCANS["codex_source_roots"])),
                "debt_left": conn.execute(
                    "SELECT COUNT(*) FROM codex_prune_debt").fetchone()[0],
            }
            inventory = _inventory(conn)
            legacy = _legacy_identities(conn, relative=True)
        finally:
            conn.close()
        return effects, inventory, legacy


def _entry_row(conn, path, root_key, offset=1):
    _entry(conn, path, root_key, offset)


def _case_child_only(store, conn):
    path = store.path("vanished-child.jsonl")
    _entry(conn, path, store.live_key, 1)
    _quota(conn, "codex", path, store.live_key, 1)
    _thread(conn, path, store.live_key, "conv-vanished")


def _case_null_roots(store, conn):
    path = store.path("null-root.jsonl")
    _file(conn, path, None)
    _entry(conn, path, None, 1)


def _case_relative_paths(store, conn):
    _file(conn, "relative/fixture.jsonl", store.live_key)
    _entry(conn, "relative/fixture.jsonl", None, 1)
    _quota(conn, "codex", "relative/fixture.jsonl", store.live_key, 1)


def _case_cross_root_requalification(store, conn):
    # The discovered rollout's terminal row names another root.
    conn.execute(
        "UPDATE codex_session_files SET source_root_key='rk-previous' "
        "WHERE path=?", (store.path("rollout.jsonl"),))


def _case_refused_roots(store, conn):
    path = str(store.unrec / "sessions" / "kept.jsonl")
    _file(conn, path, store.unrec_key)
    _entry(conn, path, store.unrec_key, 1)


def _case_unavailable_parent(store, conn):
    path = store.path("missing-dir", "kept.jsonl")
    _file(conn, path, store.live_key)
    _entry(conn, path, store.live_key, 1)


def _case_stale_superset(store, conn):
    # Inventory rows whose family rows are gone (crash leftovers, direct
    # deletes): an orphan under the live root, one under the refused root,
    # one under a missing parent, and one at a discovered path.
    for path, root_key in (
        (store.path("stale-orphan.jsonl"), store.live_key),
        (str(store.unrec / "sessions" / "stale.jsonl"), store.unrec_key),
        (store.path("missing-dir", "stale.jsonl"), store.live_key),
        (store.path("rollout.jsonl"), "rk-stale"),
    ):
        conn.execute(
            "INSERT INTO codex_retained_identities VALUES (?, ?)",
            (path, root_key))


def _rebuild_inventory_objects(conn):
    """The upgrade transition: objects missing over existing history."""
    import _cctally_db
    for _kind, name in _cctally_db._CODEX_RETAINED_IDENTITY_OBJECTS:
        if _kind == "trigger":
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute("DROP TABLE IF EXISTS codex_retained_identities")
    conn.commit()
    _cctally_db._apply_codex_retained_identity_inventory(conn)


def _case_incomplete_bootstrap(store, conn):
    _case_child_only(store, conn)
    _case_unavailable_parent(store, conn)
    conn.commit()
    _rebuild_inventory_objects(conn)
    assert conn.execute(
        "SELECT value FROM cache_meta WHERE key='codex_identity_inventory'"
    ).fetchone() == ("bootstrap:files:0",)
    assert _inventory(conn) == set()


def _case_trigger_coverage(store, conn):
    import _cctally_journal as jr
    import _lib_journal as jl
    conn.commit()
    # Journal quota replay materializes a row through the cache applier.
    replayed = store.path("replayed.jsonl")
    obs = jl.make_obs(
        at="2026-08-03T12:00:00Z", src="codex-quota", provider="codex",
        payload={
            "kind": "quota_window_snapshot", "source": "codex",
            "source_root_key": store.live_key, "source_path": replayed,
            "line_offset": 7, "captured_at_utc": "2026-08-03T12:00:00+00:00",
            "observed_slot": "primary", "logical_limit_key": "limit-primary",
            "limit_id": "native-primary", "limit_name": "Primary",
            "window_minutes": 300, "used_percent": 12.0,
            "resets_at_utc": "2026-08-03T15:00:00+00:00", "plan_type": "pro",
            "individual_limit_json": None, "reached_type": None,
            "observed_model": "gpt-5.3-codex",
        })
    assert jr._quota_applier([(obs, "seg", 100)]) is None
    assert conn.execute(
        "SELECT 1 FROM quota_window_snapshots WHERE source_path=?",
        (replayed,)).fetchone() is not None
    # A source-only UPDATE turns a Claude row into a Codex one.
    flipped = store.path("flipped.jsonl")
    _quota(conn, "claude", flipped, store.live_key, 1)
    conn.execute(
        "UPDATE quota_window_snapshots SET source='codex' WHERE source_path=?",
        (flipped,))
    # Identity-column UPDATEs on the other three families.
    conn.execute(
        "UPDATE codex_session_files SET path=? WHERE path=?",
        (store.path("moved-file.jsonl"), store.path("second.jsonl")))
    conn.execute(
        "UPDATE codex_session_entries SET source_path=? WHERE id = "
        "(SELECT MIN(id) FROM codex_session_entries)",
        (store.path("moved-entry.jsonl"),))
    conn.execute(
        "UPDATE codex_conversation_threads SET source_root_key='rk-moved' "
        "WHERE rowid = (SELECT MIN(rowid) FROM codex_conversation_threads)")
    # Plain inserts in every family (any writer, migrations included).
    path = store.path("inserted.jsonl")
    _file(conn, path, store.live_key)
    _entry(conn, path, store.live_key, 1)
    _quota(conn, "codex", path, store.live_key, 1)
    _thread(conn, path, store.live_key, "conv-inserted")
    conn.commit()
    assert _legacy_identities(conn, relative=True) <= _inventory(conn)


EFFECT_CASES = {
    "child-only-rows": _case_child_only,
    "null-roots": _case_null_roots,
    "relative-paths": _case_relative_paths,
    "cross-root-requalification": _case_cross_root_requalification,
    "refused-roots": _case_refused_roots,
    "recognized-root-unavailable-parent": _case_unavailable_parent,
    "stale-superset-entries": _case_stale_superset,
    "incomplete-bootstrap": _case_incomplete_bootstrap,
    "trigger-coverage-of-every-writer": _case_trigger_coverage,
}


@pytest.mark.parametrize("case", sorted(EFFECT_CASES))
def test_inventory_protection_matches_legacy_effects(tmp_path, monkeypatch, case):
    store = _Store(tmp_path, monkeypatch)
    ns, cache_mod = store.load()
    conn = ns["open_cache_db"]()
    try:
        EFFECT_CASES[case](store, conn)
        conn.commit()
    finally:
        conn.close()
    store.snapshot()

    if case == "incomplete-bootstrap":
        # A budgeted (hook) walk defers whole: no planning, no debt, no
        # refusal record, and the bootstrap is requested after unlock.
        store.restore()
        ns, cache_mod = store.load()
        spawned = []
        update = importlib.import_module("_cctally_update")
        monkeypatch.setattr(update, "_spawn_detached",
                            lambda command: spawned.append(command) or True)
        conn = ns["open_cache_db"]()
        try:
            before = _family_effects(conn)
            stats = ns["sync_codex_cache"](
                conn, lock_timeout=0, budget_seconds=600.0,
                quota_reconcile="defer")
            assert stats.deferred_reason == "inventory_pending"
            assert not stats.prune_refused
            assert conn.execute(
                "SELECT COUNT(*) FROM codex_prune_debt").fetchone()[0] == 0
            assert conn.execute(
                "SELECT 1 FROM cache_meta WHERE key=?",
                (cache_mod.CODEX_ORPHAN_PRUNE_REFUSED_KEY,)).fetchone() is None
            assert _family_effects(conn) == before
            assert not cache_mod.codex_identity_inventory_ready(conn)
        finally:
            conn.close()
        assert spawned == [cache_mod.CODEX_INVENTORY_BOOTSTRAP_COMMAND]
        # The bootstrap converges to exactly the legacy identity set.
        assert cache_mod.run_codex_inventory_bootstrap(
            wait_for_requests=False) > 0
        conn = ns["open_cache_db"]()
        try:
            assert cache_mod.codex_identity_inventory_ready(conn)
            assert _inventory(conn) == _legacy_identities(conn, relative=True)
        finally:
            conn.close()
        shutil.rmtree(store.snap)
        store.snapshot()

    product, inventory, legacy = store.run(oracle=False)
    expected, _oracle_inventory, _ = store.run(oracle=True)
    assert product == expected
    # The inventory stays a superset of what the families carry, and stale
    # rows the scope lets the drain judge are gone after it.
    assert legacy <= inventory
    if case == "stale-superset-entries":
        assert (store.path("stale-orphan.jsonl"), store.live_key) not in inventory
        assert expected["prune_refused"] is False
    if case in ("refused-roots", "recognized-root-unavailable-parent"):
        assert expected["prune_refused"] is True


def test_empty_root_identity_is_protected_never_orphaned(tmp_path, monkeypatch):
    """Detects an identity stored under root '' being pruned as the NULL-root
    identity: prune debt stores NULL as '', so B5 never plans it."""
    store = _Store(tmp_path, monkeypatch)
    ns, cache_mod = store.load()
    path = store.path("empty-root.jsonl")
    conn = ns["open_cache_db"]()
    try:
        _file(conn, path, "")
        _entry(conn, path, "", 1)
        conn.commit()
        stats = ns["sync_codex_cache"](conn, maintenance="caller")
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_prune_debt").fetchone()[0] == 0
        assert stats.prune_refused is True
    finally:
        conn.close()
    cache_mod.run_codex_prune_drain(wait_for_requests=False)
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries "
            "WHERE source_path=? AND source_root_key=''", (path,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_bootstrap_resumes_after_interruption_and_yields_to_a_rebuild(
    tmp_path, monkeypatch,
):
    """Detects a bootstrap that loses or duplicates work when interrupted, or
    that overwrites a rebuild's `ready` with a stale cursor."""
    store = _Store(tmp_path, monkeypatch)
    ns, cache_mod = store.load()
    conn = ns["open_cache_db"]()
    try:
        _case_child_only(store, conn)
        conn.commit()
        _rebuild_inventory_objects(conn)
    finally:
        conn.close()
    monkeypatch.setattr(cache_mod, "_CODEX_INVENTORY_BOOTSTRAP_CHUNK", 1)
    states = []

    def interrupt_third(next_state):
        states.append(next_state)
        if len(states) == 3:
            raise RuntimeError("interrupted")

    monkeypatch.setattr(cache_mod, "_CODEX_INVENTORY_BOOTSTRAP_PRECOMMIT_HOOK",
                        interrupt_third)
    with pytest.raises(RuntimeError):
        cache_mod.run_codex_inventory_bootstrap(wait_for_requests=False)
    conn = ns["open_cache_db"]()
    try:
        # Two chunks committed with their cursor; the third rolled back.
        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key='codex_identity_inventory'"
        ).fetchone() == (states[1],)
        assert len(_inventory(conn)) == 2
    finally:
        conn.close()
    monkeypatch.setattr(cache_mod, "_CODEX_INVENTORY_BOOTSTRAP_PRECOMMIT_HOOK",
                        None)
    assert cache_mod.run_codex_inventory_bootstrap(wait_for_requests=False) > 0
    conn = ns["open_cache_db"]()
    try:
        assert cache_mod.codex_identity_inventory_ready(conn)
        assert _inventory(conn) == _legacy_identities(conn, relative=True)
        _rebuild_inventory_objects(conn)
    finally:
        conn.close()

    # A Codex --rebuild lands between two chunks: the bootstrap re-reads the
    # state after its next admission and stops at the rebuild's `ready`.
    import _lib_codex_drain_admission as adm
    rebuilt = []

    def rebuild_once():
        if rebuilt:
            return
        rebuilt.append(True)
        other = ns["open_cache_db"]()
        try:
            ns["sync_codex_cache"](other, rebuild=True, maintenance="caller")
        finally:
            other.close()

    monkeypatch.setattr(adm, "_after_finish", rebuild_once)
    chunks = cache_mod.run_codex_inventory_bootstrap(wait_for_requests=False)
    assert rebuilt and chunks == 1
    conn = ns["open_cache_db"]()
    try:
        assert cache_mod.codex_identity_inventory_ready(conn)
        assert _inventory(conn) == _legacy_identities(conn, relative=True)
    finally:
        conn.close()
