"""Dashboard self-heal prunes orphans via _prune_orphaned_cache_entries and
is a no-op under skip_sync."""
from __future__ import annotations
import json, os, pathlib, shutil, sys
import pytest
from conftest import load_script, redirect_paths


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    return ns


def _orphan(ns):
    p = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-gone" / "s.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"type": "assistant", "timestamp": "2026-07-01T00:00:00Z",
        "requestId": "r", "sessionId": "S", "uuid": "u", "parentUuid": None,
        "message": {"id": "m", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 0, "output_tokens": 1,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
    conn = ns["open_cache_db"](); ns["sync_cache"](conn); conn.close()
    conn = ns["open_conversations_db"]()
    ns["sync_claude_conversations"](conn)
    conn.close()
    shutil.rmtree(p.parent)


def test_self_heal_prunes(env):
    ns = env
    _orphan(ns)
    res = ns["_dashboard_self_heal_orphans"](skip_sync=False)
    assert res is not None and res.pruned_files == 1
    conn = ns["open_cache_db"]()
    assert conn.execute("SELECT count(*) FROM session_files WHERE size_bytes>0").fetchone()[0] == 0


def test_self_heal_noop_under_skip_sync(env):
    ns = env
    _orphan(ns)
    assert ns["_dashboard_self_heal_orphans"](skip_sync=True) is None
    conn = ns["open_cache_db"]()
    assert conn.execute("SELECT count(*) FROM session_files WHERE size_bytes>0").fetchone()[0] == 1


# --- #729: a refused rollup re-derive must reach the dashboard's heal path ---


def _refused(ns, monkeypatch):
    monkeypatch.setattr(
        ns["_cctally_cache"], "PRICING_SNAPSHOT_DATE", "2020-01-01")


def test_self_heal_surfaces_a_refused_rollup_rederive(env, monkeypatch, capsys):
    ns = env
    _orphan(ns)
    _refused(ns, monkeypatch)
    res = ns["_dashboard_self_heal_orphans"](skip_sync=False)
    assert res is not None
    assert res.prune_refused is True
    assert "refused" in capsys.readouterr().err


def test_self_heal_still_invalidates_caches_for_completed_deletions(
        env, monkeypatch):
    """The deletions committed, so the caches keyed on them are stale whether
    or not the re-derive was refused. Surfacing the refusal must not suppress
    the invalidation the prune has always performed."""
    ns = env
    _orphan(ns)
    _refused(ns, monkeypatch)
    dash = ns["_cctally_dashboard"]
    bumped = []
    monkeypatch.setattr(dash, "bump_generation", lambda: bumped.append(1))
    res = ns["_dashboard_self_heal_orphans"](skip_sync=False)
    assert res.pruned_files == 1
    assert bumped == [1]


def test_self_heal_drops_the_claude_conversation_certificate_on_refusal(
        env, monkeypatch):
    """A certificate minted before the prune would let the next pass skip the
    work the refusal left undone.

    This is the BEHAVIOURAL consequence of `PruneResult.prune_refused`, and the
    only one: no production path routes a `PruneResult` to
    `provider_sync_certifiable`, so the certification predicate never sees the
    field in production (#769 S3, corrected premise). The drop is load-bearing
    rather than redundant, because `_database_identity` is `(st_dev, st_ino)`
    only — committed deletions do not change it — and the pending-identity
    guard cannot fire on a store whose flag could not be written.
    """
    ns = env
    _orphan(ns)
    _refused(ns, monkeypatch)
    dash = ns["_cctally_dashboard"]
    import _lib_ingest_frontier as frontier_mod
    frontier = frontier_mod.ConversationSyncFrontier(ns["_cctally_core"].APP_DIR)
    frontier._states["claude"] = object()
    frontier._states["codex"] = object()
    # #769 S3 A9: through monkeypatch, so the attribute is restored by the
    # fixture teardown rather than by a `finally` this test has to get right.
    monkeypatch.setattr(dash._conversation_sync_pass, "_frontier", frontier,
                        raising=False)
    ns["_dashboard_self_heal_orphans"](skip_sync=False)
    assert "claude" not in frontier._states
    assert "codex" in frontier._states, (
        "the prune surface is Claude-only; Codex certifies independently"
    )


def test_self_heal_names_the_refusal_cause_that_occurred(env, monkeypatch,
                                                         capsys):
    """#769 S3 A2, on the dashboard's own message."""
    ns = env
    _orphan(ns)
    cache = ns["_cctally_cache"]
    import _lib_pricing
    obs = _lib_pricing.classify_pricing_fingerprint(
        found=False, raw=None, error_kind="operational_error")
    monkeypatch.setattr(
        cache, "_read_pricing_fingerprint_observation",
        lambda conn, key=None: obs)
    res = ns["_dashboard_self_heal_orphans"](skip_sync=False)
    assert res.prune_refused is True
    err = capsys.readouterr().err
    assert "older pricing" not in err
    assert cache.pricing_refusal_cause_phrase("degraded") in err
