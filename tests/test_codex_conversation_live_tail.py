"""#294 S7 — Codex targeted ingest + live-tail file-set resolution (spec §5.1/§5.3).

The Codex analogue of ``tests/test_conversation_live_tail.py``: proves
``sync_codex_cache(only_paths=...)`` + ``CodexIngestStats.targeted_clean`` port
the Claude targeted-ingest contract, and ``codex_conversation_source_paths``
resolves a conversation's own + child files. Every dirty/clean condition of
spec §5.1 is pinned, including the two-target zero-mutation preflight, the
post-preflight late-shrink race, the concurrent-writer handoff, and the
whole-tree-bypass survival of unrelated files and roots.

All fixtures synthetic, reusing the ``tests/fixtures/codex-parity/v1`` corpus.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys

import pytest

from conftest import load_script, redirect_paths

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_codex_conversation_query as q  # noqa: E402
import _lib_source_identity as identity  # noqa: E402

CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"


def _rollout_bytes(scenario: str) -> bytes:
    return (CORPUS / f"{scenario}.jsonl").read_bytes()


def _setup(tmp_path, monkeypatch):
    """A live Codex cache seam with one provider root at ``<tmp>/provider``."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    (provider_root / "sessions").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return ns, provider_root


def _place(provider_root, name, scenario, *, sub="2026/07/15"):
    """Write a corpus scenario as a rollout file under the sessions tree."""
    dst = provider_root / "sessions" / sub / f"{name}.jsonl"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CORPUS / f"{scenario}.jsonl", dst)
    return dst


def _entries(conn, path):
    return conn.execute(
        "SELECT COUNT(*) FROM codex_session_entries WHERE source_path=?",
        (str(path),)).fetchone()[0]


def _events(conn, path):
    """Legacy helper name; targeted core tests now observe the core cursor.

    Transcript-event targeting is covered against the independent store in
    ``test_conversation_db_split``.
    """
    return conn.execute(
        "SELECT COALESCE(MAX(last_byte_offset), 0) FROM codex_session_files "
        "WHERE path=?",
        (str(path),)).fetchone()[0]


# ── B1: targeted ingest ───────────────────────────────────────────────────────


def test_only_paths_ingests_only_the_named_file(tmp_path, monkeypatch):
    ns, root = _setup(tmp_path, monkeypatch)
    a = _place(root, "a", "modern-full")
    b = _place(root, "b", "modern-no-quota")
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](conn, only_paths={str(a)})
        assert stats.targeted_clean is True
        assert _events(conn, a) > 0          # A ingested
        assert _events(conn, b) == 0         # B untouched (no global walk)
    finally:
        conn.close()


def test_only_paths_with_rebuild_raises_value_error(tmp_path, monkeypatch):
    ns, root = _setup(tmp_path, monkeypatch)
    a = _place(root, "a", "modern-full")
    conn = ns["open_cache_db"]()
    try:
        with pytest.raises(ValueError):
            ns["sync_codex_cache"](conn, only_paths={str(a)}, rebuild=True)
    finally:
        conn.close()


def test_only_paths_bypasses_orphan_prune(tmp_path, monkeypatch):
    """A targeted call must never prune rows for a file it wasn't asked about,
    even after that file vanished from disk (whole-tree bypass, §5.1)."""
    ns, root = _setup(tmp_path, monkeypatch)
    a = _place(root, "a", "modern-full")
    b = _place(root, "b", "modern-no-quota")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)   # full: both tracked
        assert _events(conn, b) > 0
        b.unlink()                                    # B gone from disk
        ns["sync_codex_cache"](conn, only_paths={str(a)})
        assert _events(conn, b) > 0                   # B NOT pruned
    finally:
        conn.close()


def test_only_paths_whole_tree_bypass_keeps_unrelated_root(tmp_path, monkeypatch):
    """Targeting a file in root A must not drop root B's ``codex_source_roots``
    row (the batch's per-file root prune is bypassed in targeted mode)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    root_a = tmp_path / "rootA"
    root_b = tmp_path / "rootB"
    (root_a / "sessions").mkdir(parents=True)
    (root_b / "sessions").mkdir(parents=True)
    a = root_a / "sessions" / "a.jsonl"
    b = root_b / "sessions" / "b.jsonl"
    shutil.copyfile(CORPUS / "modern-full.jsonl", a)
    shutil.copyfile(CORPUS / "modern-no-quota.jsonl", b)
    monkeypatch.setenv("CODEX_HOME", f"{root_a},{root_b}")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)
        roots_before = conn.execute(
            "SELECT COUNT(*) FROM codex_source_roots").fetchone()[0]
        assert roots_before == 2
        # Append to A so the targeted call has real work, then target only A.
        with open(a, "ab") as fh:
            fh.write(_rollout_bytes("modern-quota-payload"))
        ns["sync_codex_cache"](conn, only_paths={str(a)})
        roots_after = conn.execute(
            "SELECT COUNT(*) FROM codex_source_roots").fetchone()[0]
        assert roots_after == 2               # root B survived
    finally:
        conn.close()


def test_only_paths_declines_on_shrink_zero_mutation(tmp_path, monkeypatch):
    """Whole-call read-only preflight: any target shrank → dirty with zero
    mutations (no partial commit)."""
    ns, root = _setup(tmp_path, monkeypatch)
    a = _place(root, "a", "modern-full")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)
        before = _events(conn, a)
        a.write_bytes(_rollout_bytes("modern-full")[:20])   # shrank
        stats = ns["sync_codex_cache"](conn, only_paths={str(a)})
        assert stats.targeted_clean is False
        assert stats.deferred_reason == "truncation"
        assert _events(conn, a) == before                   # zero mutation
    finally:
        conn.close()


def test_two_target_preflight_shrink_is_zero_mutation(tmp_path, monkeypatch):
    """Two targets; if EITHER shrank in the preflight, the WHOLE call returns
    dirty with zero mutations — the healthy target's cursor must NOT advance."""
    ns, root = _setup(tmp_path, monkeypatch)
    a = _place(root, "a", "modern-full")
    b = _place(root, "b", "modern-no-quota")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)
        a_events = _events(conn, a)
        # A grows (healthy), B shrinks (unhealthy).
        with open(a, "ab") as fh:
            fh.write(_rollout_bytes("modern-quota-payload"))
        b.write_bytes(_rollout_bytes("modern-no-quota")[:20])
        stats = ns["sync_codex_cache"](conn, only_paths={str(a), str(b)})
        assert stats.targeted_clean is False
        # Zero mutation: A's healthy growth was NOT ingested (no partial commit).
        assert _events(conn, a) == a_events
    finally:
        conn.close()


def test_post_preflight_late_shrink_race(tmp_path, monkeypatch):
    """A shrink landing AFTER the preflight but before a later target's write
    turn declines that file only — the earlier target's commit stands, the call
    is dirty, and a later full sync recovers clean cycles."""
    ns, root = _setup(tmp_path, monkeypatch)
    a = _place(root, "a_first", "modern-full")
    b = _place(root, "b_second", "modern-no-quota")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)
        # Grow both so both pass the preflight (no shrink at preflight time).
        with open(a, "ab") as fh:
            fh.write(_rollout_bytes("modern-quota-payload"))
        with open(b, "ab") as fh:
            fh.write(_rollout_bytes("modern-quota-payload"))
        a_before = _events(conn, a)

        def _shrink_b(path_str):
            # Fires after each file commits. When A (sorted first) commits,
            # truncate B below its committed cursor to simulate the race.
            if path_str == str(a):
                b.write_bytes(_rollout_bytes("modern-no-quota")[:20])

        stats = ns["sync_codex_cache"](
            conn, only_paths={str(a), str(b)}, _on_file_committed=_shrink_b)
        assert stats.targeted_clean is False        # call dirty
        assert _events(conn, a) > a_before          # A's commit STANDS
        assert stats.files_failed >= 1              # B declined at its turn
        # A later full sync recovers: B re-ingests from offset 0 (truncation).
        full = ns["sync_codex_cache"](conn, rebuild=True)
        assert full.targeted_clean is True
    finally:
        conn.close()


def test_concurrent_writer_handoff_is_clean(tmp_path, monkeypatch):
    """A file whose committed cursor already covers the observed size (a
    concurrent writer ingested it) is unchanged → clean, never dirty."""
    ns, root = _setup(tmp_path, monkeypatch)
    a = _place(root, "a", "modern-full")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)   # cursor covers full size
        stats = ns["sync_codex_cache"](conn, only_paths={str(a)})
        assert stats.targeted_clean is True
        assert stats.files_skipped_unchanged == 1
    finally:
        conn.close()


def test_vanished_file_is_clean_and_dropped(tmp_path, monkeypatch):
    ns, root = _setup(tmp_path, monkeypatch)
    missing = root / "sessions" / "2026" / "07" / "15" / "gone.jsonl"
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](conn, only_paths={str(missing)})
        assert stats.targeted_clean is True
        assert stats.files_total == 0
    finally:
        conn.close()


def test_outside_root_path_is_dropped_and_clean(tmp_path, monkeypatch):
    ns, root = _setup(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere" / "x.jsonl"
    outside.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", outside)
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](conn, only_paths={str(outside)})
        assert stats.targeted_clean is True
        assert stats.files_total == 0                # not under any root → dropped
        assert _events(conn, outside) == 0
    finally:
        conn.close()


def test_only_paths_overlapping_roots_first_match_physical_dedup(tmp_path, monkeypatch):
    """Two configured roots aliasing the SAME physical rollout (root B holds a
    symlink into root A). Targeting BOTH spellings in one call ingests the file
    exactly ONCE — first-match physical dedup keeps the alias out — with the
    surviving row carrying the FIRST matching root's source-root key, clean."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    root_a = tmp_path / "rootA"
    root_b = tmp_path / "rootB"
    (root_a / "sessions").mkdir(parents=True)
    (root_b / "sessions").mkdir(parents=True)
    a_real = root_a / "sessions" / "a.jsonl"
    shutil.copyfile(CORPUS / "modern-full.jsonl", a_real)
    alias = root_b / "sessions" / "alias.jsonl"
    os.symlink(a_real, alias)                       # root B spelling → same physical
    monkeypatch.setenv("CODEX_HOME", f"{root_a},{root_b}")
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](
            conn, only_paths={str(a_real), str(alias)})
        assert stats.targeted_clean is True
        # Ingested exactly once, under root A's spelling (sorted first).
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_files").fetchone()[0] == 1
        assert _events(conn, a_real) > 0            # root A file ingested
        assert _events(conn, alias) == 0            # alias never ingested separately
        path, srk = conn.execute(
            "SELECT path, source_root_key FROM codex_session_files").fetchone()
        assert path == str(a_real)                  # first matching root's spelling
        assert srk == identity.source_root_key(str(root_a.resolve()))
    finally:
        conn.close()


def test_only_paths_dotdot_escape_is_dropped_and_clean(tmp_path, monkeypatch):
    """A requested path that ``..``-escapes the configured root's walk boundary to
    a REAL file outside every root is DROPPED (clean, never ingested), while a
    sibling in-root path in the SAME call ingests — proving the call did real work
    and the drop is containment, not a missing target."""
    ns, root = _setup(tmp_path, monkeypatch)
    inroot = _place(root, "inroot", "modern-full")
    outside = tmp_path / "outside" / "evil.jsonl"
    outside.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-no-quota.jsonl", outside)   # real file, outside
    # ``<provider>/../outside/evil.jsonl`` resolves to ``outside`` but lexically
    # leaves the ``<provider>/sessions`` walk root, so containment drops it.
    escape = root / ".." / "outside" / "evil.jsonl"
    assert escape.resolve() == outside.resolve()                # really reaches it
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](
            conn, only_paths={str(escape), str(inroot)})
        assert stats.targeted_clean is True         # escape drop is clean, no raise
        assert stats.files_total == 1               # only the in-root sibling qualified
        assert _events(conn, inroot) > 0            # sibling ingested (real work)
        assert _events(conn, outside) == 0          # escaped file NOT ingested
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_files WHERE path=?",
            (str(outside),)).fetchone()[0] == 0
    finally:
        conn.close()


def test_malformed_tail_growth_is_clean(tmp_path, monkeypatch):
    """Growth that is tolerated malformed/incomplete-tail content commits the
    cursor without new conversation rows — a documented clean (spurious) cycle."""
    ns, root = _setup(tmp_path, monkeypatch)
    a = _place(root, "a", "modern-full")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)
        with open(a, "ab") as fh:
            fh.write(b"{ this is not valid json at all\n")
        stats = ns["sync_codex_cache"](conn, only_paths={str(a)})
        assert stats.targeted_clean is True
    finally:
        conn.close()


def test_targeted_does_not_invoke_global_quota_reconciler(tmp_path, monkeypatch):
    """Targeted mode never runs the global quota reconciler (§5.1)."""
    ns, root = _setup(tmp_path, monkeypatch)
    a = _place(root, "a", "modern-quota-payload")
    conn = ns["open_cache_db"]()
    calls = []
    import _cctally_quota as quota
    monkeypatch.setattr(
        quota, "reconcile_codex_quota_projection",
        lambda *a, **k: calls.append(1))
    try:
        stats = ns["sync_codex_cache"](conn, only_paths={str(a)})
        assert stats.targeted_clean is True
        assert calls == []                            # reconciler NOT called
    finally:
        conn.close()


def test_full_sync_still_invokes_reconciler(tmp_path, monkeypatch):
    """Guard: the reconciler-not-called assertion is non-vacuous — a full sync
    over the same quota-bearing rollout DOES invoke it."""
    ns, root = _setup(tmp_path, monkeypatch)
    _place(root, "a", "modern-quota-payload")
    conn = ns["open_cache_db"]()
    calls = []
    import _cctally_quota as quota
    monkeypatch.setattr(
        quota, "reconcile_codex_quota_projection",
        lambda *a, **k: calls.append(1))
    try:
        ns["sync_codex_cache"](conn)
        assert calls != []
    finally:
        conn.close()


def test_targeted_parity_with_full_sync_for_that_file(tmp_path, monkeypatch):
    ns, root = _setup(tmp_path, monkeypatch)
    a = _place(root, "a", "modern-full")
    core = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](core)
    finally:
        core.close()
    conn = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conn, only_paths={str(a)})
        targeted = conn.execute(
            "SELECT line_offset, record_type, event_type, turn_id "
            "FROM codex_conversation_events WHERE source_path=? ORDER BY line_offset",
            (str(a),)).fetchall()
        ns["sync_codex_conversations"](conn, rebuild=True)
        full = conn.execute(
            "SELECT line_offset, record_type, event_type, turn_id "
            "FROM codex_conversation_events WHERE source_path=? ORDER BY line_offset",
            (str(a),)).fetchall()
    finally:
        conn.close()
    assert targeted == full


# ── B2: codex_conversation_source_paths ───────────────────────────────────────


def test_source_paths_own_plus_children(tmp_path, monkeypatch):
    ns, root = _setup(tmp_path, monkeypatch)
    parent = _place(root, "parent", "nested-parent")
    child = _place(root, "child", "nested-child")
    core = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](core, rebuild=True)
        # Resolve the parent conversation key from its thread row.
        parent_key = core.execute(
            "SELECT conversation_key FROM codex_conversation_threads "
            "WHERE native_thread_id='parent-thread-fixture'").fetchone()[0]
    finally:
        core.close()
    conn = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conn, rebuild=True)
        paths = set(q.codex_conversation_source_paths(conn, parent_key))
        assert str(parent) in paths          # own file
        assert str(child) in paths           # child's file (widened set)
    finally:
        conn.close()


def test_source_paths_unknown_is_empty(tmp_path, monkeypatch):
    ns, root = _setup(tmp_path, monkeypatch)
    conn = ns["open_conversations_db"]()
    try:
        assert q.codex_conversation_source_paths(conn, "v1.nope") == []
    finally:
        conn.close()


def test_codex_conversation_exists(tmp_path, monkeypatch):
    ns, root = _setup(tmp_path, monkeypatch)
    _place(root, "a", "modern-full")
    core = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](core, rebuild=True)
    finally:
        core.close()
    conn = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conn, rebuild=True)
        key = conn.execute(
            "SELECT DISTINCT conversation_key FROM codex_conversation_messages "
            "LIMIT 1").fetchone()[0]
        assert q.codex_conversation_exists(conn, key) is True
        assert q.codex_conversation_exists(conn, "v1.nope") is False
    finally:
        conn.close()
