"""#901 Amendment 20 W1 (dc18 S2): one retained-identity collection per pass.

``sync_codex_cache``'s ordinary (non-targeted, non-rebuild) prune path needs
every retained Codex source identity twice: once to find the inactive orphans
and once to protect the paths whose prune it refuses. It collected them twice,
and each collection runs four history-sized family scans plus the root scan,
and the orphan partition scanned ``codex_session_files`` a third time for the
terminal identities. Measured in workload D, those scans were about half of
every Codex hook's SQL time, and the first of each pair, against a cold file
cache, made one hook 4.7 s.

Both collections run back to back under the cache writer and Codex provider
flocks, with only pure partitioning (and the filesystem prune-scope probe)
between them, and every writer of these families takes the same flocks
(``sync_codex_cache`` and the journal's cache leg). One collection therefore
sees exactly what two did.

(a) RED proof: a hook-shaped pass runs each family scan exactly once. On the
    unchanged tree the four family scans and the root scan each run twice and
    the terminal-file scan three times.
(b) Equivalence (G2-style): the new single-collection plan returns exactly
    what a frozen copy of the old code returns — orphan sources, orphan root
    keys, and the safe and refused partitions of both — on adversarial data:
    relative paths, NULL root keys, a family lacking its terminal file row, a
    vanished root, duplicate identities across families, a configured but
    unrecognizable root and an unreadable parent directory.
"""
from __future__ import annotations

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


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


# ── (a) statement count over one hook-shaped pass ───────────────────────────


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


def test_a_hook_shaped_pass_runs_each_retained_family_scan_once(
    tmp_path, monkeypatch,
):
    ns, cache, rollout = _seed(tmp_path, monkeypatch)
    try:
        # Every family is populated, so each scan has real rows to return.
        for family, sql in FAMILY_SCANS.items():
            assert cache.execute(sql).fetchall(), family
        statements: list[str] = []
        cache.set_trace_callback(statements.append)
        try:
            # The hook's call (bin/_cctally_record.py, the Codex quota leg).
            stats = ns["sync_codex_cache"](
                cache,
                lock_timeout=0,
                budget_seconds=600.0,
                active_transcript_path=str(rollout),
                quota_reconcile="defer",
            )
        finally:
            cache.set_trace_callback(None)
        assert not stats.lock_contended
        assert stats.deferred_reason is None
        assert stats.prune_refused is False
        counts = {
            family: sum(
                1 for statement in statements
                if _normalized(statement) == _normalized(sql)
            )
            for family, sql in FAMILY_SCANS.items()
        }
        assert counts == {family: 1 for family in FAMILY_SCANS}, counts
    finally:
        cache.close()


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


@pytest.fixture
def adversarial(tmp_path, monkeypatch):
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
