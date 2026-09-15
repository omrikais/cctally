"""One evidence generation across registered consumers (#769 S6, #716 Task A).

`DashboardIngestFrontier` and `ConversationSyncFrontier` each held their own
`_states` and their own byte cursor into one shared ticket ledger, and nothing
tied the two together. Two consumers could therefore rest on different bodies
of evidence while both reported themselves caught up, and neither one could
say whether the other had consumed the walk it was trusting.

A coordinator now issues ONE evidence generation per provider. Each consumer
keeps its own acknowledgement cursor — they plan and commit independently and
must be able to sit at different points — but a generation retires only after
every REGISTERED consumer has acknowledged it, and a new exhaustive walk joins
the live generation rather than replacing it.

Registration is explicit rather than assumed. The standalone TUI constructs
only an accounting frontier, so a coordinator that waited unconditionally for
a conversation acknowledgement would never retire a generation there at all.

The configured root set joins the certificate identity for a separate reason:
the saved state carries directory identities but not the normalized root set,
and plan-time roots are used only to validate targets, so adding or removing a
configured root with no filesystem change anywhere left an old certificate
looking caught up.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import threading

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_ingest_frontier as frontier  # noqa: E402


def _simulate_restart(app_dir) -> None:
    """Forget the coordinator for this data directory.

    A process restart is exactly this: the in-process generation is gone and
    the two stores' stamps are all that remain.  The module global is reached
    directly rather than through a production reset function, because #769
    S6's constraints forbid a production test hook — `tests/conftest.py`
    substitutes this dict for the duration of every test, so removing one key
    from it touches nothing outside this test.
    """
    frontier._FRONTIER_GENERATIONS.pop(str(pathlib.Path(app_dir)), None)


# ── the coordinator, on its own ────────────────────────────────────────────

@pytest.fixture
def coordinator(tmp_path):
    return frontier.FrontierGenerations(tmp_path / "app")


def test_one_registered_consumer_retires_its_own_generation(coordinator):
    """The standalone TUI's shape: accounting registers, conversations never."""
    first = coordinator.issue("codex", "accounting")
    assert not coordinator.retired("codex", first)
    coordinator.acknowledge("codex", "accounting", first)
    assert coordinator.retired("codex", first)


def test_a_generation_waits_for_every_registered_consumer(coordinator):
    first = coordinator.issue("codex", "accounting")
    joined = coordinator.issue("codex", "conversation")
    assert joined == first, "a second consumer joins the live generation"
    coordinator.acknowledge("codex", "accounting", first)
    assert not coordinator.retired("codex", first)
    coordinator.acknowledge("codex", "conversation", first)
    assert coordinator.retired("codex", first)


def test_a_new_walk_joins_a_live_generation_rather_than_replacing_it(
    coordinator,
):
    """Two consumers must converge, or the shared claim means nothing."""
    first = coordinator.issue("codex", "accounting")
    coordinator.issue("codex", "conversation")
    assert coordinator.issue("codex", "accounting") == first


def test_a_retired_generation_is_replaced_by_the_next_walk(coordinator):
    first = coordinator.issue("codex", "accounting")
    coordinator.acknowledge("codex", "accounting", first)
    assert coordinator.issue("codex", "accounting") != first


def test_providers_carry_independent_generations(coordinator):
    assert coordinator.issue("codex", "accounting") != coordinator.issue(
        "claude", "accounting")


def test_dropping_a_consumer_lets_the_generation_retire(coordinator):
    """`drop_provider` after a prune must not strand every later generation."""
    first = coordinator.issue("codex", "accounting")
    coordinator.issue("codex", "conversation")
    coordinator.acknowledge("codex", "accounting", first)
    assert not coordinator.retired("codex", first)
    coordinator.drop("codex", "conversation")
    assert coordinator.retired("codex", first)


def test_plan_acknowledge_and_drop_are_thread_safe(coordinator):
    """Two dashboard threads reach one coordinator; neither may corrupt it."""
    errors: list[BaseException] = []

    def worker(consumer):
        try:
            for _ in range(200):
                token = coordinator.issue("codex", consumer)
                coordinator.acknowledge("codex", consumer, token)
                coordinator.retired("codex", token)
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(name,))
        for name in ("accounting", "conversation")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


# ── the frontiers share it ─────────────────────────────────────────────────

def _cache_store(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE session_files (path TEXT);"
        "CREATE TABLE codex_session_files ("
        "  path TEXT PRIMARY KEY, size_bytes INTEGER, mtime_ns INTEGER,"
        "  last_byte_offset INTEGER, last_ingested_at TEXT,"
        "  ingest_complete INTEGER NOT NULL DEFAULT 1,"
        "  device_id INTEGER, inode INTEGER);"
        "INSERT INTO cache_meta VALUES "
        "('dashboard_codex_full_walk_complete','1');"
    )
    conn.commit()
    return conn


def _conversation_store(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE conversation_source_files (path TEXT);"
        "CREATE TABLE codex_conversation_source_files ("
        "  path TEXT PRIMARY KEY, size_bytes INTEGER, mtime_ns INTEGER,"
        "  last_byte_offset INTEGER, last_ingested_at TEXT,"
        "  device_id INTEGER, inode INTEGER);"
    )
    conn.commit()
    return conn


@pytest.fixture
def pair(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "rollout.jsonl").write_text('{"a":1}\n', encoding="utf-8")
    cache = _cache_store(tmp_path / "cache.db")
    conversations = _conversation_store(tmp_path / "conversations.db")
    accounting = frontier.DashboardIngestFrontier(app_dir)
    transcripts = frontier.ConversationSyncFrontier(app_dir)
    yield accounting, transcripts, cache, conversations, (sessions,)
    cache.close()
    conversations.close()


def test_both_frontiers_seed_onto_one_generation(pair):
    accounting, transcripts, cache, conversations, roots = pair
    assert accounting.seed_provider("codex", cache, roots=roots), (
        accounting.last_seed_failure)
    assert transcripts.seed_provider("codex", conversations, roots=roots), (
        transcripts.last_seed_failure)
    assert accounting.evidence_generation("codex") is not None
    assert (
        accounting.evidence_generation("codex")
        == transcripts.evidence_generation("codex")
    )


def test_an_accounting_only_process_still_retires_its_generation(pair):
    """No conversation frontier ever seeds, so none is registered."""
    accounting, _transcripts, cache, _conversations, roots = pair
    assert accounting.seed_provider("codex", cache, roots=roots)
    token = accounting.evidence_generation("codex")
    plan = accounting.plan_provider("codex", cache, roots=roots)
    accounting.commit_provider(plan, cache, roots=roots)
    assert accounting.generation_retired("codex", token)


def test_a_conversation_frontier_that_seeded_holds_the_generation_open(pair):
    accounting, transcripts, cache, conversations, roots = pair
    assert accounting.seed_provider("codex", cache, roots=roots)
    assert transcripts.seed_provider("codex", conversations, roots=roots)
    token = accounting.evidence_generation("codex")
    plan = accounting.plan_provider("codex", cache, roots=roots)
    accounting.commit_provider(plan, cache, roots=roots)
    assert not accounting.generation_retired("codex", token)
    conversation_plan = transcripts.plan_provider(
        "codex", conversations, roots=roots)
    transcripts.commit_provider(conversation_plan, conversations, roots=roots)
    assert accounting.generation_retired("codex", token)


def test_drop_provider_after_a_prune_still_works(pair):
    accounting, transcripts, cache, conversations, roots = pair
    assert accounting.seed_provider("codex", cache, roots=roots)
    assert transcripts.seed_provider("codex", conversations, roots=roots)
    token = accounting.evidence_generation("codex")
    plan = accounting.plan_provider("codex", cache, roots=roots)
    accounting.commit_provider(plan, cache, roots=roots)
    assert transcripts.drop_provider("codex") is True
    assert transcripts.plan_provider(
        "codex", conversations, roots=roots).mode == "full"
    assert accounting.generation_retired("codex", token), (
        "a dropped consumer must stop holding every later generation open")


def test_a_directly_constructed_accounting_frontier_still_works(tmp_path):
    """The benchmarks build one of these with no coordinator in sight."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    cache = _cache_store(tmp_path / "cache.db")
    try:
        state = frontier.DashboardIngestFrontier(app_dir)
        assert state.seed_provider("codex", cache, roots=(sessions,)), (
            state.last_seed_failure)
        assert state.plan_provider(
            "codex", cache, roots=(sessions,)).mode == "caught_up"
    finally:
        cache.close()


# ── the durable stamp ──────────────────────────────────────────────────────

def test_each_store_stamps_the_generation_it_acknowledged(pair):
    accounting, transcripts, cache, conversations, roots = pair
    assert accounting.seed_provider("codex", cache, roots=roots)
    assert transcripts.seed_provider("codex", conversations, roots=roots)
    token = accounting.evidence_generation("codex")
    for conn in (cache, conversations):
        stored = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            (frontier.frontier_generation_meta_key("codex"),)).fetchone()
        assert stored is not None, "no store stamped its generation"
        assert stored[0] == token


def test_a_restart_that_finds_disagreeing_stamps_fails_closed(tmp_path):
    """Two stores describing different walks cannot both be trusted."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    cache = _cache_store(tmp_path / "cache.db")
    conversations = _conversation_store(tmp_path / "conversations.db")
    try:
        accounting = frontier.DashboardIngestFrontier(app_dir)
        transcripts = frontier.ConversationSyncFrontier(app_dir)
        assert accounting.seed_provider("codex", cache, roots=(sessions,))
        assert transcripts.seed_provider(
            "codex", conversations, roots=(sessions,))

        # A restart: fresh objects, fresh coordinator, the two stores' stamps
        # deliberately disagreeing about which walk they rest on.
        conversations.execute(
            "UPDATE cache_meta SET value='a-different-walk' WHERE key=?",
            (frontier.frontier_generation_meta_key("codex"),))
        conversations.commit()
        _simulate_restart(app_dir)

        restarted_accounting = frontier.DashboardIngestFrontier(app_dir)
        restarted_transcripts = frontier.ConversationSyncFrontier(app_dir)
        assert restarted_accounting.seed_provider(
            "codex", cache, roots=(sessions,))
        assert not restarted_transcripts.seed_provider(
            "codex", conversations, roots=(sessions,))
        assert restarted_transcripts.last_seed_failure["codex"] == (
            "generation_disagreement")
    finally:
        cache.close()
        conversations.close()


def test_a_disagreeing_restart_repairs_itself_on_the_next_walk(tmp_path):
    """Fail closed ONCE, not forever.

    The two stores disagreeing is not a rare corruption: a generation retires
    when every registered consumer acknowledges it, the next walk mints a new
    one, and only the store of the consumer that walked is stamped with it —
    so two consumers whose walks land in different rounds leave two different
    stamps behind as an ordinary outcome. Measured on the sealed #786 current
    corpus, both providers' stamps differed after one ordinary dashboard run.

    A refusal that never lifts turns that into a permanent full walk on EVERY
    tick, which is worse than the disagreement it is protecting against and is
    the exact cost the whole frontier exists to avoid. The refusal is therefore
    one-shot: this consumer walks exhaustively once, and that walk stamps its
    store, which is what makes the two agree again.
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    cache = _cache_store(tmp_path / "cache.db")
    conversations = _conversation_store(tmp_path / "conversations.db")
    key = frontier.frontier_generation_meta_key("codex")
    try:
        accounting = frontier.DashboardIngestFrontier(app_dir)
        transcripts = frontier.ConversationSyncFrontier(app_dir)
        assert accounting.seed_provider("codex", cache, roots=(sessions,))
        assert transcripts.seed_provider(
            "codex", conversations, roots=(sessions,))
        conversations.execute(
            "UPDATE cache_meta SET value='a-different-walk' WHERE key=?",
            (key,))
        conversations.commit()
        _simulate_restart(app_dir)

        restarted_accounting = frontier.DashboardIngestFrontier(app_dir)
        restarted_transcripts = frontier.ConversationSyncFrontier(app_dir)
        assert restarted_accounting.seed_provider(
            "codex", cache, roots=(sessions,))
        assert not restarted_transcripts.seed_provider(
            "codex", conversations, roots=(sessions,))

        # The recovery walk this refusal forced now commits, and its seed is
        # the thing that repairs the stamp.
        assert restarted_transcripts.seed_provider(
            "codex", conversations, roots=(sessions,)), (
            restarted_transcripts.last_seed_failure)
        stamps = {
            conn.execute("SELECT value FROM cache_meta WHERE key=?",
                         (key,)).fetchone()[0]
            for conn in (cache, conversations)
        }
        assert len(stamps) == 1, (
            "the recovery walk left the two stores still disagreeing, so the "
            "next restart refuses again and every tick walks the whole estate")
        assert restarted_accounting.seed_provider(
            "codex", cache, roots=(sessions,)), (
            restarted_accounting.last_seed_failure)
    finally:
        cache.close()
        conversations.close()


def test_the_repair_survives_the_peer_retiring_the_generation(tmp_path):
    """The refusal registers the consumer, so the peer cannot retire past it.

    `test_a_disagreeing_restart_repairs_itself_on_the_next_walk` above passes
    for a reason narrower than the guarantee it asserts: its restarted
    accounting consumer never commits a clean plan, so nothing retires between
    the refusal and the second seed. Let the peer commit one, and — unless the
    refusal itself registers this consumer — the accounting consumer is the
    only registered one, the generation retires, and the second seed MINTS a
    fresh generation and stamps this store with it, leaving the two stores
    still disagreeing and the next restart refusing again.
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    cache = _cache_store(tmp_path / "cache.db")
    conversations = _conversation_store(tmp_path / "conversations.db")
    key = frontier.frontier_generation_meta_key("codex")
    try:
        accounting = frontier.DashboardIngestFrontier(app_dir)
        transcripts = frontier.ConversationSyncFrontier(app_dir)
        assert accounting.seed_provider("codex", cache, roots=(sessions,))
        assert transcripts.seed_provider(
            "codex", conversations, roots=(sessions,))
        conversations.execute(
            "UPDATE cache_meta SET value='a-different-walk' WHERE key=?",
            (key,))
        conversations.commit()
        _simulate_restart(app_dir)

        restarted_accounting = frontier.DashboardIngestFrontier(app_dir)
        restarted_transcripts = frontier.ConversationSyncFrontier(app_dir)
        assert restarted_accounting.seed_provider(
            "codex", cache, roots=(sessions,))
        assert not restarted_transcripts.seed_provider(
            "codex", conversations, roots=(sessions,))
        minted = restarted_accounting.evidence_generation("codex")

        # The peer commits a clean plan. That is its acknowledgement, and it
        # is the ordinary next thing a running dashboard does.
        plan = restarted_accounting.plan_provider(
            "codex", cache, roots=(sessions,))
        assert plan.mode == "caught_up", plan.reason
        restarted_accounting.commit_provider(plan, cache, roots=(sessions,))
        assert not restarted_accounting.generation_retired("codex", minted), (
            "the refused consumer is unregistered, so its peer retired the "
            "generation on its own")

        assert restarted_transcripts.seed_provider(
            "codex", conversations, roots=(sessions,)), (
            restarted_transcripts.last_seed_failure)
        stamps = {
            conn.execute("SELECT value FROM cache_meta WHERE key=?",
                         (key,)).fetchone()[0]
            for conn in (cache, conversations)
        }
        assert stamps == {minted}, (
            "the recovery walk minted a NEW generation instead of joining the "
            "live one, so the two stores still disagree")
    finally:
        cache.close()
        conversations.close()


# ── root membership is part of the certificate ─────────────────────────────

def test_adding_a_configured_root_forces_recovery(pair, tmp_path):
    accounting, _transcripts, cache, _conversations, roots = pair
    assert accounting.seed_provider("codex", cache, roots=roots)
    extra = tmp_path / "second-sessions"
    extra.mkdir()
    plan = accounting.plan_provider("codex", cache, roots=(*roots, extra))
    assert (plan.mode, plan.reason) == ("full", "root_membership_changed")


def test_removing_a_configured_root_forces_recovery(pair, tmp_path):
    accounting, _transcripts, cache, _conversations, roots = pair
    extra = tmp_path / "second-sessions"
    extra.mkdir()
    assert accounting.seed_provider("codex", cache, roots=(*roots, extra))
    plan = accounting.plan_provider("codex", cache, roots=roots)
    assert (plan.mode, plan.reason) == ("full", "root_membership_changed")


def test_reordering_the_same_roots_is_not_a_change(pair, tmp_path):
    """Membership, not order. A reordered `$CODEX_HOME` changes nothing."""
    accounting, _transcripts, cache, _conversations, roots = pair
    extra = tmp_path / "second-sessions"
    extra.mkdir()
    assert accounting.seed_provider("codex", cache, roots=(*roots, extra))
    plan = accounting.plan_provider("codex", cache, roots=(extra, *roots))
    assert plan.mode == "caught_up", plan.reason


def test_the_conversation_frontier_checks_root_membership_too(pair, tmp_path):
    _accounting, transcripts, _cache, conversations, roots = pair
    assert transcripts.seed_provider("codex", conversations, roots=roots)
    extra = tmp_path / "second-sessions"
    extra.mkdir()
    plan = transcripts.plan_provider(
        "codex", conversations, roots=(*roots, extra))
    assert (plan.mode, plan.reason) == ("full", "root_membership_changed")


# ── the registry substitution survives a test body's own monkeypatch.undo ──

def test_the_registry_isolation_is_not_undone_by_an_in_body_undo(monkeypatch):
    """`tests/conftest.py`'s substitution must outlive `monkeypatch.undo()`.

    `monkeypatch` is ONE function-scoped instance shared by every fixture and
    by the test function of an item, so a body that calls `undo()` reverts
    every substitution the conftest fixtures made as well as its own. About
    fifteen modules in this estate call it. When the frontier registry was
    substituted through `monkeypatch`, such a body restored the REAL
    coordinator registry mid-test, and anything it registered afterwards
    leaked into the next test and was reported against that test rather than
    against the one that wrote it.

    The substitution therefore must not go through `monkeypatch`. The property
    the substitution has to keep is separate and is asserted by
    `tests/_pytest_isolation_plugin.py`: the real registry object is never
    mutated, so its identity and its length both survive the item.
    """
    import _lib_ingest_frontier as isolated_frontier

    substituted = isolated_frontier._FRONTIER_GENERATIONS
    monkeypatch.undo()
    assert isolated_frontier._FRONTIER_GENERATIONS is substituted
    # Whatever this test registers must stay inside the substituted registry.
    isolated_frontier._FRONTIER_GENERATIONS["written-by-this-test"] = object()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
