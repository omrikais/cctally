"""_prune_orphaned_cache_entries: safe whole-dir prune vs the three
residual gates (A session-id shared, B uuid-less coverage gap, C surviving
key overlap), degraded conversation_messages, and marker re-establishment."""
from __future__ import annotations
import json, os, pathlib, sqlite3
import pytest
from conftest import load_script, redirect_paths, rollup_fresh_mismatches


def _assistant(msg_id, req_id, *, uuid=None, out=10, ts="2026-07-01T00:00:00Z"):
    obj = {
        "type": "assistant", "timestamp": ts, "requestId": req_id,
        "sessionId": None,
        "message": {"id": msg_id, "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 0, "output_tokens": out,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
    }
    if uuid is not None:
        obj["uuid"] = uuid
        obj["parentUuid"] = None
    return obj


def _write(path, sid, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for ln in lines:
        ln = dict(ln); ln["sessionId"] = sid
        out.append(json.dumps(ln))
    path.write_text("\n".join(out) + "\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects"
    conn = ns["open_cache_db"]()
    conversations = ns["open_conversations_db"]()
    yield ns, conn, conversations, projects
    conversations.close()
    conn.close()


def _sync(ns, conn, conversations):
    ns["sync_cache"](conn)
    ns["sync_claude_conversations"](conversations)


def _counts(conn, conversations, path):
    return (
        conn.execute("SELECT count(*) FROM session_files WHERE path=?", (path,)).fetchone()[0],
        conn.execute("SELECT count(*) FROM session_entries WHERE source_path=?", (path,)).fetchone()[0],
        conversations.execute(
            "SELECT count(*) FROM conversation_messages WHERE source_path=?", (path,)
        ).fetchone()[0],
    )


def test_safe_whole_dir_prune(env):
    ns, conn, conversations, projects = env
    orphan = projects / "-proj-gone" / "s1.jsonl"
    _write(orphan, "S1", [_assistant("m1", "r1", uuid="u1"),
                          _assistant("m2", "r2", uuid="u2")])
    _sync(ns, conn, conversations)
    assert _counts(conn, conversations, str(orphan)) == (1, 2, 2)
    import shutil; shutil.rmtree(orphan.parent)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 1 and res.pruned_entries == 2 and res.pruned_messages == 2
    assert res.residual_paths == []
    assert _counts(conn, conversations, str(orphan)) == (0, 0, 0)
    assert conversations.execute(
        "SELECT count(*) FROM conversation_sessions WHERE session_id='S1'"
    ).fetchone()[0] == 0


def test_residual_gate_a_shared_session(env):
    ns, conn, conversations, projects = env
    live = projects / "-proj-live" / "a.jsonl"
    gone = projects / "-proj-gone" / "b.jsonl"
    _write(live, "S9", [_assistant("m1", "r1", uuid="u1")])
    _write(gone, "S9", [_assistant("m2", "r2", uuid="u2")])
    _sync(ns, conn, conversations)
    import os; os.remove(gone)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 0
    assert str(gone) in res.residual_paths


def test_residual_gate_c_surviving_key(env):
    ns, conn, conversations, projects = env
    # Different session ids (Gate A passes) but the surviving file physically
    # shares (m1,r1) — Gate C must catch it. Force ingest order so the shared
    # deduped session_entries row pins to `gone` (first inserter), making Gate
    # C's own-key scan deterministically find it under a surviving path.
    live = projects / "-proj-live" / "a.jsonl"
    gone = projects / "-proj-gone" / "b.jsonl"
    _write(gone, "GONE", [_assistant("m1", "r1", uuid="u1b")])
    _sync(ns, conn, conversations)              # pins (m1,r1) -> gone
    _write(live, "LIVE", [_assistant("m1", "r1", uuid="u1")])
    _sync(ns, conn, conversations)
    import os; os.remove(gone)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 0 and str(gone) in res.residual_paths
    # Safety invariant: the shared cost row survives (never dropped).
    assert conn.execute("SELECT count(*) FROM session_entries WHERE msg_id='m1' AND req_id='r1'").fetchone()[0] == 1


def test_residual_gate_b_uuidless_blind_spot(env):
    ns, conn, conversations, projects = env
    gone = projects / "-proj-gone" / "b.jsonl"
    _write(gone, "SOLO", [_assistant("m1", "r1", uuid=None)])
    _sync(ns, conn, conversations)
    assert _counts(conn, conversations, str(gone))[1] == 1
    assert _counts(conn, conversations, str(gone))[2] == 0
    import os; os.remove(gone)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 0 and str(gone) in res.residual_paths


def test_marker_reestablished_after_prune(env):
    ns, conn, conversations, projects = env
    keep = projects / "-proj-keep" / "k.jsonl"
    gone = projects / "-proj-gone" / "g.jsonl"
    _write(keep, "KEEP", [_assistant("m9", "r9", uuid="u9")])
    _write(gone, "GONE", [_assistant("m1", "r1", uuid="u1")])
    _sync(ns, conn, conversations)
    import os; os.remove(gone)
    _sync(ns, conn, conversations)
    assert conn.execute("SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'").fetchone() is None
    ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    _sync(ns, conn, conversations)
    assert conn.execute("SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'").fetchone() is not None


def test_null_session_id_residual(env):
    ns, conn, conversations, projects = env
    gone = projects / "-proj-gone" / "n.jsonl"
    _write(gone, "SID", [_assistant("m1", "r1", uuid="u1")])
    _sync(ns, conn, conversations)
    conn.execute("UPDATE session_files SET session_id=NULL WHERE path=?", (str(gone),))
    conn.commit()
    import os; os.remove(gone)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 0 and str(gone) in res.residual_paths


def test_contended_returns_without_mutating(env):
    ns, conn, conversations, projects = env
    import fcntl
    gone = projects / "-proj-gone" / "c.jsonl"
    _write(gone, "CID", [_assistant("m1", "r1", uuid="u1")])
    _sync(ns, conn, conversations)
    import os; os.remove(gone)
    lock_path = ns["_cctally_core"].CACHE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=0.1)
        assert res.contended is True
        assert res.pruned_files == 0
        # Untouched: the orphan row is still tracked.
        assert conn.execute("SELECT count(*) FROM session_files WHERE path=?", (str(gone),)).fetchone()[0] == 1
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN); holder.close()


# ---------------------------------------------------------------------------
# #195 review gate P2a. Cache migration 030 arms the cache-write-split re-walk
# by zeroing every per-file cursor. Both orphan gates read `size_bytes` as the
# "this path had ingested bytes" bit (`_prune_orphaned_cache_entries` and
# sync_cache's detect-only leg), and a path no longer on disk is never revisited
# by the re-walk — so a blanket `size_bytes = 0` would erase that bit
# PERMANENTLY and turn both gates into no-ops for every pre-upgrade orphan.
# ---------------------------------------------------------------------------

def _arm_030(ns, conn):
    handler = next(m.handler for m in ns["_CACHE_MIGRATIONS"]
                   if m.name == "030_session_entries_cache_creation_split")
    handler(conn)
    return handler


def test_030_leaves_a_deleted_path_prunable(env):
    ns, conn, conversations, projects = env
    orphan = projects / "-proj-gone" / "s1.jsonl"
    _write(orphan, "S1", [_assistant("m1", "r1", uuid="u1"),
                          _assistant("m2", "r2", uuid="u2")])
    _sync(ns, conn, conversations)
    assert _counts(conn, conversations, str(orphan)) == (1, 2, 2)
    import shutil; shutil.rmtree(orphan.parent)
    _arm_030(ns, conn)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 1 and res.pruned_entries == 2, (
        "030 destroyed the orphan evidence: --prune-orphans can never reclaim "
        "this path again")
    assert _counts(conn, conversations, str(orphan)) == (0, 0, 0)


def test_030_leaves_a_deleted_path_visible_to_sync_detection(env):
    """The D5a leg: an orphaned cache does not mirror disk, so sync_cache must
    still invalidate the walk-complete marker after 030 has armed the re-walk."""
    ns, conn, conversations, projects = env
    live = projects / "-proj-live" / "a.jsonl"
    gone = projects / "-proj-gone" / "b.jsonl"
    _write(live, "S8", [_assistant("m1", "r1", uuid="u1")])
    _write(gone, "S9", [_assistant("m2", "r2", uuid="u2")])
    _sync(ns, conn, conversations)
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'"
    ).fetchone() is not None, "guard: the clean walk must have set the marker"
    import os; os.remove(gone)
    _arm_030(ns, conn)
    _sync(ns, conn, conversations)
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'"
    ).fetchone() is None, "030 hid the orphan from sync_cache's detect-only leg"


def test_030_does_not_invent_orphans_for_never_ingested_rows(env):
    """The inverse hazard: a `size_bytes = 0` row holds no session_entries, so
    its absence from disk leaves no orphan. Fixtures deliberately seed such
    rows; 030 must not promote them into orphan candidates."""
    ns, conn, conversations, projects = env
    live = projects / "-proj-live" / "a.jsonl"
    _write(live, "S8", [_assistant("m1", "r1", uuid="u1")])
    _sync(ns, conn, conversations)
    conn.execute(
        "INSERT INTO session_files(path, size_bytes, mtime_ns, last_byte_offset, "
        "last_ingested_at) VALUES('/nowhere/synthetic.jsonl', 0, 0, 0, '2026-07-25T00:00:00Z')")
    conn.commit()
    _arm_030(ns, conn)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 0 and res.residual_paths == []


# --- #729: a refused rollup recompute must reach the prune's caller ---------
# `_prune_orphaned_cache_entries` discarded `_recompute_conversation_sessions`'
# return inside its own BEGIN, so a prune whose deletions committed without an
# authorized rollup re-derive reported the same clean result as one that fully
# succeeded.


def _refuse_the_rollup(ns, monkeypatch):
    """Make the store's recorded pricing NEWER than this process's, which is
    exactly the production condition: an older binary against a store a newer
    one already wrote."""
    import _cctally_cache as cache
    monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2020-01-01")
    return cache


def test_refused_recompute_is_reported_not_swallowed(env, monkeypatch):
    ns, conn, conversations, projects = env
    orphan = projects / "-proj-gone" / "s1.jsonl"
    _write(orphan, "S1", [_assistant("m1", "r1", uuid="u1")])
    _sync(ns, conn, conversations)
    import shutil
    shutil.rmtree(orphan.parent)
    _refuse_the_rollup(ns, monkeypatch)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.prune_refused is True
    assert res.prune_refused_files > 0
    # The deletions themselves still completed: the refusal is about the
    # re-derive that should have followed them, and reporting it as "nothing
    # was pruned" would be a second lie.
    assert res.pruned_files == 1


def test_an_authorized_prune_reports_no_refusal(env):
    ns, conn, conversations, projects = env
    orphan = projects / "-proj-gone" / "s1.jsonl"
    _write(orphan, "S1", [_assistant("m1", "r1", uuid="u1")])
    _sync(ns, conn, conversations)
    import shutil
    shutil.rmtree(orphan.parent)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.prune_refused is False
    assert res.prune_refused_files == 0


def test_the_shared_predicate_would_read_the_field_if_a_result_reached_it(env):
    """The field matches `CodexIngestStats`, and this pins WHY it is there —
    which is not what the spec first claimed (#769 S3, corrected premise).

    `provider_sync_certifiable` reads `prune_refused` through
    `getattr(stats, "prune_refused", False)`, so adding the field to
    `PruneResult` makes that read see it. But no production path routes a
    `PruneResult` there: it is constructed in `_prune_orphaned_cache_entries`
    and consumed only by `_dashboard_self_heal_orphans` and its startup caller,
    while the sole production caller of `provider_sync_certifiable` passes
    accounting sync stats that already carry the field. So this composition is
    synthetic and flips no production verdict; the behavioural consequence of a
    refused prune is `ConversationSyncFrontier.drop_provider`, pinned in
    `tests/test_dashboard_self_heal.py`.
    """
    ns, conn, conversations, projects = env
    import _lib_ingest_frontier as frontier
    import _cctally_cache as cache
    clean = cache.PruneResult(pruned_files=1, pruned_entries=1)
    refused = cache.PruneResult(pruned_files=1, pruned_entries=1,
                                prune_refused=True, prune_refused_files=1)
    assert hasattr(clean, "prune_refused"), (
        "the field must exist, or the shared predicate cannot read it"
    )
    assert frontier.provider_sync_certifiable("targeted", clean) is True
    assert frontier.provider_sync_certifiable("targeted", refused) is False


def test_no_production_path_routes_a_prune_result_to_the_certifier(env):
    """The corrected premise, asserted rather than asserted-in-prose.

    `_prune_orphaned_cache_entries` is the only constructor of `PruneResult`,
    and neither of its two consumers hands the result to
    `provider_sync_certifiable`. If a future change routes one there, this
    fails and the test above stops being synthetic — which is the moment to
    reword it.

    SCOPE, stated so the docstring does not read as a general guarantee. The
    walk covers `ast.FunctionDef` only, so an `async def` consumer would be
    invisible, and it inspects exactly the three named consumers rather than
    every function that could construct or receive a `PruneResult`. Widening
    either is the right response to a fourth consumer; reading this as a
    whole-tree proof is not.
    """
    import ast
    import pathlib as _pathlib
    for module, consumers in (
        ("bin/_cctally_cache.py", ("_prune_orphaned_cache_entries", "cmd_cache_sync")),
        ("bin/_cctally_dashboard.py", ("_dashboard_self_heal_orphans",)),
    ):
        tree = ast.parse((_pathlib.Path(__file__).resolve().parent.parent
                          / module).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in consumers:
                continue
            called = {
                c.func.attr if isinstance(c.func, ast.Attribute) else
                getattr(c.func, "id", None)
                for c in ast.walk(node) if isinstance(c, ast.Call)
            }
            assert "provider_sync_certifiable" not in called, (
                f"{module}:{node.name} now routes a PruneResult to the "
                f"certifier; the synthetic test above must be reworded"
            )


# --- #769 S3 A2: the refusal message must name the cause that occurred ------
# A refused rollup re-derive had exactly one cause when the message was
# written: this process's pricing table being older than the store's. #728 made
# that non-exclusive — MALFORMED and DEGRADED refuse too — so the message
# states a cause that may not have happened.


def _degrade_the_fingerprint_read(monkeypatch):
    import _cctally_cache as cache
    import _lib_pricing
    obs = _lib_pricing.classify_pricing_fingerprint(
        found=False, raw=None, error_kind="operational_error")
    monkeypatch.setattr(
        cache, "_read_pricing_fingerprint_observation",
        lambda conn, key=None: obs)
    return cache


@pytest.mark.parametrize("degrade,expected_state", [
    (False, "present"),
    (True, "degraded"),
])
def test_the_prune_result_carries_the_state_that_refused(
        env, monkeypatch, degrade, expected_state):
    ns, conn, conversations, projects = env
    orphan = projects / "-proj-gone" / "s1.jsonl"
    _write(orphan, "S1", [_assistant("m1", "r1", uuid="u1")])
    _sync(ns, conn, conversations)
    import shutil
    shutil.rmtree(orphan.parent)
    if degrade:
        _degrade_the_fingerprint_read(monkeypatch)
    else:
        _refuse_the_rollup(ns, monkeypatch)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.prune_refused is True
    assert res.prune_refused_state == expected_state


def test_each_refusal_state_gets_its_own_operator_phrase():
    import _cctally_cache as cache
    phrases = {
        state: cache.pricing_refusal_cause_phrase(state)
        for state in ("present", "malformed", "degraded", None)
    }
    assert len(set(phrases.values())) == len(phrases), (
        "two states sharing a phrase re-creates the single-cause claim"
    )
    assert "older" in phrases["present"]
    assert "older" not in phrases["degraded"], (
        "a read that failed says nothing about which pricing table is older"
    )
    assert "older" not in phrases["malformed"]


# ---------------------------------------------------------------------------
# #944 L2 / spec 5.6: the prune deletes accounting rows, so it links every
# unlinked one (an old writer's stamp) to a journaled decision first. The
# dashboard's automatic self-heal calls the function directly; the operator's
# form is `cache-sync --prune-orphans`.
# ---------------------------------------------------------------------------

def _old_writer_orphan(ns, conn, conversations, projects):
    """Sync one transcript as a writer that knows nothing of attribution
    decisions (its row is stamped A and unlinked), then remove it."""
    import _cctally_claude_attribution as G
    from test_claude_account_attribution import _login
    gone = projects / "-proj-gone" / "o.jsonl"
    _write(gone, "OLD", [_assistant("m1", "r1", uuid="u1")])
    text = gone.read_text()
    a = _login("A")
    with pytest.MonkeyPatch.context() as m:
        m.setattr(G, "schema_present", lambda _conn: False)
        _sync(ns, conn, conversations)
    assert conn.execute(
        "SELECT account_key, attribution_decision_key FROM session_entries"
    ).fetchall() == [(a, None)]
    import shutil; shutil.rmtree(gone.parent)
    return gone, text, a


def _prune(ns, conn, how):
    if how == "direct":
        return ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    args = ns["build_parser"]().parse_args(
        ["cache-sync", "--source", "claude", "--prune-orphans"])
    return args.func(args)


@pytest.mark.parametrize("how", ["direct", "cli"])
def test_prune_links_unlinked_rows_before_deleting_them(env, how):
    """Detects: a prune deleting an old writer's stamped row with no decision,
    so the restored transcript re-ingests under whoever is logged in then."""
    from test_claude_account_attribution import _login
    ns, conn, conversations, projects = env
    gone, text, a = _old_writer_orphan(ns, conn, conversations, projects)
    _login("B")
    _prune(ns, conn, how)
    assert _counts(conn, conversations, str(gone))[:2] == (0, 0)
    assert conn.execute(
        "SELECT account_key, basis FROM claude_cost_decisions"
    ).fetchall() == [(a, "preserved_history")]
    gone.parent.mkdir(parents=True)
    gone.write_text(text)
    _sync(ns, conn, conversations)
    assert conn.execute(
        "SELECT e.account_key FROM session_entries e JOIN claude_cost_decisions"
        " d ON d.decision_key = e.attribution_decision_key"
    ).fetchall() == [(a,)]


def test_prune_with_an_unwritable_journal_prunes_nothing(env, monkeypatch,
                                                          capsys):
    """Detects: a prune deleting rows whose decisions it could not journal."""
    import _cctally_journal as jr
    ns, conn, conversations, projects = env
    gone, _text, _a = _old_writer_orphan(ns, conn, conversations, projects)

    def boom(*_a, **_k):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(jr, "append_records", boom)
    capsys.readouterr()
    assert _prune(ns, conn, "cli") == 1
    assert ("[cache-sync] prune-orphans skipped: the cctally journal could not"
            " be written.") in capsys.readouterr().err
    assert _counts(conn, conversations, str(gone))[:2] == (1, 1)


# ---------------------------------------------------------------------------
# #916: a full walk whose only defect is residual orphan rows writes the
# `claude_ingest_walk_orphan_residual` token instead of the walk-complete
# marker. The accounting frontier accepts either; every other reader of the
# marker (the upgrade gate, the dashboard debug state) still sees it absent.
# ---------------------------------------------------------------------------

_MARKER = "claude_ingest_walk_complete"
_TOKEN = "claude_ingest_walk_orphan_residual"


def _meta(conn, key):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def _residual_store(ns, conn, conversations, projects, *, prunable=False):
    """Two surviving transcripts and one deleted one. Unless ``prunable``, the
    deleted one has no ``uuid``, so gate B keeps it a residual."""
    keep = projects / "-proj-keep" / "k.jsonl"
    other = projects / "-proj-keep" / "o.jsonl"
    gone = projects / "-proj-gone" / "g.jsonl"
    _write(keep, "KEEP", [_assistant("mk", "rk", uuid="uk")])
    _write(other, "OTHER", [_assistant("mo", "ro", uuid="uo")])
    _write(gone, "GONE", [_assistant("mg", "rg", uuid="ug" if prunable else None)])
    _sync(ns, conn, conversations)
    assert _meta(conn, _MARKER) is not None, "guard: the store starts complete"
    import os; os.remove(gone)
    return keep, other, gone


def _append(path, sid, *lines):
    with path.open("a") as fh:
        for ln in lines:
            ln = dict(ln); ln["sessionId"] = sid
            fh.write(json.dumps(ln) + "\n")


def test_a_residual_only_walk_leaves_the_marker_absent(env):
    """#916 A7. Detects: certifying the frontier by writing the walk-complete
    marker over orphan rows, which would let the upgrade gate stamp the
    008/009/010 recomputes over data that no longer has a source."""
    ns, conn, conversations, projects = env
    keep, other, gone = _residual_store(ns, conn, conversations, projects)
    stats = ns["sync_cache"](conn)

    assert _meta(conn, _TOKEN) is not None
    assert _meta(conn, _MARKER) is None
    assert stats.full_walk_complete is False
    assert stats.full_walk_residual_only is True
    assert stats.orphan_residual_count == 1
    assert json.loads(_meta(conn, _TOKEN))["orphans"] == 1
    import _cctally_db as db
    with pytest.raises(db.MigrationGateNotMet):
        db._gate_001_post_ingest_completed(conn, [projects], data_present=True)


def test_identical_residual_walks_write_nothing_and_a_changed_set_rewrites_once(env):
    """#916 A8 (#901 W8). Detects: a residual-only walk rewriting its token on
    every dashboard tick, and a changed orphan set leaving a stale token."""
    ns, conn, conversations, projects = env
    keep, other, gone = _residual_store(ns, conn, conversations, projects)
    ns["sync_cache"](conn)
    first = _meta(conn, _TOKEN)
    assert first is not None

    before = conn.total_changes
    ns["sync_cache"](conn)
    assert conn.total_changes == before, "an identical residual-only walk wrote"
    assert _meta(conn, _TOKEN) == first

    import os; os.remove(other)
    ns["sync_cache"](conn)
    second = _meta(conn, _TOKEN)
    assert second != first
    assert json.loads(second)["orphans"] == 2
    assert json.loads(second)["fingerprint"] != json.loads(first)["fingerprint"]

    before = conn.total_changes
    ns["sync_cache"](conn)
    assert conn.total_changes == before, "the rewritten token was rewritten again"
    assert _meta(conn, _TOKEN) == second


def test_residual_token_lifecycle(env):
    """#916 A9. Detects: a targeted live-tail walk disturbing the token, and a
    cleared residual leaving the token beside the marker it replaces."""
    ns, conn, conversations, projects = env
    keep, other, gone = _residual_store(
        ns, conn, conversations, projects, prunable=True)
    ns["sync_cache"](conn)
    token = _meta(conn, _TOKEN)
    assert token is not None

    _append(keep, "KEEP", _assistant("mk2", "rk2", uuid="uk2"))
    targeted = ns["sync_cache"](conn, only_paths={str(keep)})
    assert targeted.targeted_clean and targeted.files_processed == 1, targeted
    assert _meta(conn, _TOKEN) == token, "a targeted walk touched the token"
    assert _meta(conn, _MARKER) is None

    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 1 and res.residual_paths == [], res
    stats = ns["sync_cache"](conn)
    assert stats.full_walk_complete and not stats.full_walk_residual_only
    assert _meta(conn, _MARKER) is not None
    assert _meta(conn, _TOKEN) is None, "the marker and the token are exclusive"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="chmod-0 unreadable injection has no effect when running as root",
)
def test_an_on_disk_defect_beside_an_orphan_withdraws_the_token(env):
    """#916 A9. Detects: a walk that skipped an on-disk file keeping a token
    that vouches every on-disk source was ingested."""
    import _lib_ingest_frontier as frontier
    ns, conn, conversations, projects = env
    keep, other, gone = _residual_store(ns, conn, conversations, projects)
    ns["sync_cache"](conn)
    assert _meta(conn, _TOKEN) is not None

    _append(other, "OTHER", _assistant("mo2", "ro2", uuid="uo2"))
    other.chmod(0)
    try:
        stats = ns["sync_cache"](conn)
    finally:
        other.chmod(0o644)
    assert stats.files_failed == 1, "guard: the unreadable file must fail its read"
    assert _meta(conn, _TOKEN) is None and _meta(conn, _MARKER) is None
    assert not frontier.provider_sync_certifiable("full", stats), stats

    recovered = ns["sync_cache"](conn)
    assert recovered.full_walk_residual_only and _meta(conn, _TOKEN) is not None


def test_residual_walks_leave_orphan_rows_and_ingest_appends_once(env):
    """#916 A5. Detects: residual-only walks (the walks a certified orphan
    store runs: targeted for a ticketed append, full at expiry) losing or
    double-counting accounting rows, either the orphan's retained rows or
    bytes appended to a surviving transcript."""
    ns, conn, conversations, projects = env
    keep, other, gone = _residual_store(ns, conn, conversations, projects)

    def orphan_rows():
        return conn.execute(
            "SELECT * FROM session_entries WHERE source_path=? ORDER BY line_offset",
            (str(gone),)).fetchall()

    first = ns["sync_cache"](conn)
    retained = orphan_rows()
    assert len(retained) == 1
    second = ns["sync_cache"](conn)
    assert first.full_walk_residual_only and second.full_walk_residual_only
    assert orphan_rows() == retained

    _append(keep, "KEEP", _assistant("mk2", "rk2", uuid="uk2"),
            _assistant("mk3", "rk3", uuid="uk3"))
    targeted = ns["sync_cache"](conn, only_paths={str(keep)})
    assert targeted.targeted_clean and targeted.files_processed == 1, targeted
    full = ns["sync_cache"](conn)
    assert full.full_walk_residual_only and full.files_processed == 0, full

    # Each logical message exactly once: a re-read under a fresh physical key
    # or a lost append changes this multiset (the physical key itself is
    # UNIQUE, so it cannot show a double count).
    assert sorted(conn.execute(
        "SELECT msg_id, req_id FROM session_entries WHERE source_path=?",
        (str(keep),)).fetchall()) == [("mk", "rk"), ("mk2", "rk2"), ("mk3", "rk3")]
    assert orphan_rows() == retained


_WATERMARK = "conversation_sessions_input_watermark"
_PENDING = "conversation_sessions_rederive_pending"


def _conv_meta(conversations, key):
    row = conversations.execute(
        "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _generation(conn):
    row = conn.execute("SELECT value FROM cache_meta "
                       "WHERE key='claude_accounting_generation'").fetchone()
    return row[0] if row else None


def _residual_sibling_store(ns, conn, conversations, projects, *,
                            residual_session="S"):
    """Session S spans two removed files: p1 (prunable) inserted the turn key
    (m2, r2) first, and p2 (residual: m9 has no transcript row) holds the same
    turn, under S or under another session (a fork). Both directories are
    gone on return."""
    p1 = projects / "-proj-gone" / "a.jsonl"
    p2 = projects / "-proj-gone2" / "b.jsonl"
    _write(p1, "S", [_assistant("m1", "r1", uuid="u1"),
                     _assistant("m2", "r2", uuid="u2", out=40)])
    _sync(ns, conn, conversations)              # pins (m2,r2) -> p1
    _write(p2, residual_session, [_assistant("m2", "r2", uuid="u2b", out=40),
                                  _assistant("m9", "r9", uuid=None)])
    _sync(ns, conn, conversations)
    assert rollup_fresh_mismatches(conversations) == []
    import shutil
    shutil.rmtree(p1.parent)
    shutil.rmtree(p2.parent)
    return p1, p2


@pytest.mark.parametrize("residual_session", ["S", "T"])
def test_prune_leaves_a_residual_session_rollup_fresh(env, residual_session):
    """#1072 input path 4: the prune deletes accounting rows a residual session
    still prices (its retained transcript holds the same turn key, whose row
    the pruned file inserted first), either the pruned session itself or
    another session holding the turn under the residual path (a fork, T).
    Stale-cost failure: that session keeps its pre-prune cost and
    cache-rebuild count in the unscoped Viewer. The prune also acknowledges
    its own generation rotation, so the next pass needs no full re-derive."""
    ns, conn, conversations, projects = env
    _p1, p2 = _residual_sibling_store(ns, conn, conversations, projects,
                                      residual_session=residual_session)
    before = json.loads(_conv_meta(conversations, _WATERMARK))
    assert before["generation"] == _generation(conn)

    def residual_cost():
        return conversations.execute(
            "SELECT cost_usd FROM conversation_sessions WHERE session_id=?",
            (residual_session,)).fetchone()[0]

    cost_before = residual_cost()
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 1 and str(p2) in res.residual_paths
    assert rollup_fresh_mismatches(conversations) == []
    assert residual_cost() < cost_before
    after = json.loads(_conv_meta(conversations, _WATERMARK))
    assert _generation(conn) != before["generation"]
    assert after == {**before, "generation": _generation(conn)}
    assert _conv_meta(conversations, _PENDING) is None


def test_prune_of_a_multi_session_path_rederives_every_session(env):
    """A pruned file holding two sessions' messages: ``session_files`` names
    only one of them, so re-deriving by that id left the other session's row
    (its messages now deleted) in the Viewer with its old cost and count."""
    ns, conn, conversations, projects = env
    orphan = projects / "-proj-gone" / "mixed.jsonl"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for sid, (mid, rid, uid) in (("S1", ("m1", "r1", "u1")),
                                 ("S2", ("m2", "r2", "u2"))):
        ln = _assistant(mid, rid, uuid=uid)
        ln["sessionId"] = sid
        lines.append(json.dumps(ln))
    orphan.write_text("\n".join(lines) + "\n")
    _sync(ns, conn, conversations)
    assert {r[0] for r in conversations.execute(
        "SELECT session_id FROM conversation_sessions")} == {"S1", "S2"}
    import shutil
    shutil.rmtree(orphan.parent)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 1
    assert rollup_fresh_mismatches(conversations) == []
    assert conversations.execute(
        "SELECT count(*) FROM conversation_sessions").fetchone()[0] == 0


@pytest.mark.parametrize("crash", ["after_step2", "step4_raises"])
def test_prune_dies_after_transcript_step(env, monkeypatch, crash):
    """A prune that dies after deleting the transcript rows (before the
    accounting delete), or whose re-derive step fails, must leave the affected
    sessions recorded durably, so the next steady pass repairs them; nothing
    else would ever touch a session whose files are gone."""
    import _cctally_cache as cache
    ns, conn, conversations, projects = env
    _residual_sibling_store(ns, conn, conversations, projects)
    if crash == "after_step2":
        def die(*_args, **_kwargs):
            raise RuntimeError("the prune died before its accounting delete")
        real = cache._delete_claude_session_entries
        monkeypatch.setattr(cache, "_delete_claude_session_entries", die)
        with pytest.raises(RuntimeError):
            ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
        monkeypatch.setattr(cache, "_delete_claude_session_entries", real)
    else:
        def fail(*_args, **_kwargs):
            raise sqlite3.OperationalError("step 4 failed")
        monkeypatch.setattr(cache, "_acknowledge_generation_rotation", fail)
        res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
        assert res.pruned_files == 1
    assert "S" in json.loads(_conv_meta(conversations, _PENDING))["sessions"]
    assert rollup_fresh_mismatches(conversations) != []
    ns["sync_claude_conversations"](conversations)
    assert rollup_fresh_mismatches(conversations) == []
    assert _conv_meta(conversations, _PENDING) is None


def test_restored_pre_prune_cache_converges(env, tmp_path):
    """A cache.db copy taken before a prune and restored after the prune's
    acknowledged pass carries the pre-prune generation; the rollup must treat
    that as a discontinuity and converge in one pass instead of keeping the
    post-prune costs of the rows the copy brought back."""
    import _cctally_core as core_mod
    ns, conn, conversations, projects = env
    _residual_sibling_store(ns, conn, conversations, projects)
    copy = tmp_path / "pre-prune-cache.db"
    src = sqlite3.connect(str(core_mod.CACHE_DB_PATH))
    dst = sqlite3.connect(str(copy))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 1
    ns["sync_claude_conversations"](conversations)
    assert rollup_fresh_mismatches(conversations) == []
    src = sqlite3.connect(str(copy))
    dst = sqlite3.connect(str(core_mod.CACHE_DB_PATH))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    assert rollup_fresh_mismatches(conversations) != []
    ns["sync_claude_conversations"](conversations)
    assert rollup_fresh_mismatches(conversations) == []


def test_prune_transcript_step_holds_the_write_lock_before_reading(
        env, monkeypatch):
    """The transcript transaction reads (the affected sessions, the key
    holders, the pending set) before its first write. Deferred, it holds only
    a read snapshot, so a Codex conversation write committed in that window
    makes the prune's first write fail at once with `database is locked`
    (SQLITE_BUSY_SNAPSHOT, which no busy timeout retries) and the prune
    raises. Holding the write lock from the start makes that writer wait
    instead."""
    import shutil
    import _cctally_cache as cache
    import _cctally_core as core
    ns, conn, conversations, projects = env
    orphan = projects / "-proj-gone" / "s1.jsonl"
    _write(orphan, "S1", [_assistant("m1", "r1", uuid="u1")])
    _sync(ns, conn, conversations)
    shutil.rmtree(orphan.parent)
    real = cache._sessions_holding_turn_key
    attempts = []

    def interleave(conv, mid, rid):
        other = sqlite3.connect(str(core.CONVERSATIONS_DB_PATH), timeout=0)
        try:
            other.execute(
                "INSERT INTO cache_meta(key, value) VALUES('test_1072_probe', "
                "'x') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            other.commit()
            attempts.append("committed")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
            attempts.append("locked")
        finally:
            other.close()
        return real(conv, mid, rid)

    monkeypatch.setattr(cache, "_sessions_holding_turn_key", interleave)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 1
    assert attempts == ["locked"]
    assert _counts(conn, conversations, str(orphan)) == (0, 0, 0)


@pytest.mark.parametrize("stored", ["pruned_generation", "other_generation"])
def test_acknowledge_rotation_rewrites_only_the_pruned_generation(env, stored):
    """The prune may rewrite only the generation it read and rotated itself.
    Rewriting a watermark or cursor target that carries another generation (an
    earlier rotation nothing has consumed yet) would acknowledge deletes no
    pass re-derived, leaving their sessions' costs stale."""
    import _cctally_cache as cache
    _ns, _conn, conversations, _projects = env
    held = "G-old" if stored == "pruned_generation" else "G-earlier"
    target = {"v": 1, "generation": held, "seq": 5, "message_id": 7,
              "migrations": []}
    cursor = {"v": 1, "target": dict(target), "after": "s1",
              "pricing": "2026-01-01", "derivation": 1}
    for key, value in ((_WATERMARK, target),
                       ("conversation_sessions_rederive_cursor", cursor)):
        conversations.execute(
            "INSERT INTO cache_meta(key,value) VALUES(?,?) ON CONFLICT(key) "
            "DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
    conversations.commit()
    cache._acknowledge_generation_rotation(conversations, "G-old", "G-new")
    conversations.commit()
    expected = "G-new" if stored == "pruned_generation" else "G-earlier"
    assert json.loads(_conv_meta(conversations, _WATERMARK)) == {
        **target, "generation": expected}
    assert json.loads(_conv_meta(
        conversations, "conversation_sessions_rederive_cursor")) == {
        **cursor, "target": {**target, "generation": expected}}
