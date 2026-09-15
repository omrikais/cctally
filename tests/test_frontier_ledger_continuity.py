"""The activity ledger proves gap-free consumption (#769 S6, #716 Task A).

The ticket ledger is rotated at 4 MiB by replacement with a file holding only
the latest payload, and the existing inode and byte-offset checks fail closed
within one process but cannot prove that a consumer saw every ticket across a
rotation or a restart. An inode number is reusable, a sidecar can be lost, and
two consumers can drift onto different ledger generations without either one
noticing.

So each ticket takes the next durable sequence under the activity-marker lock,
rotation advances a ledger epoch while the sequence keeps counting globally,
and each consumer persists its acknowledged ``(epoch, sequence)``. SEVEN named
discontinuities force exhaustive recovery, and each one is asserted below to
produce ``full`` with its own reason rather than ``caught_up``.

The seventh is the cross-consumer one. Two consumers legitimately sit at
different sequences inside one generation, because they plan and commit
independently, but two different GENERATIONS are irreconcilable: neither one
can tell which tickets the other already consumed, so only an exhaustive walk
restores a shared frontier.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_ingest_frontier as frontier  # noqa: E402


# ── the pure kernel ────────────────────────────────────────────────────────

def _classify(**kwargs):
    base = dict(
        ack_epoch="e1", ack_sequence=10, epoch="e1", tail_sequence=10,
        sequences=(),
    )
    base.update(kwargs)
    return frontier.classify_ledger_continuity(**base)


def test_an_unchanged_epoch_with_no_new_tickets_is_continuous():
    assert _classify() is None


def test_a_contiguous_run_from_the_acknowledged_sequence_is_continuous():
    assert _classify(sequences=(11, 12, 13), tail_sequence=13) is None


def test_a_changed_epoch_forces_recovery():
    assert _classify(epoch="e2", tail_sequence=11, sequences=(11,)) == (
        "ledger_epoch_changed")


def test_a_first_sequence_beyond_acknowledged_plus_one_is_a_gap():
    assert _classify(sequences=(12,), tail_sequence=12) == "ledger_sequence_gap"


def test_a_gap_inside_the_run_is_a_gap():
    assert _classify(sequences=(11, 13), tail_sequence=13) == (
        "ledger_sequence_gap")


def test_a_repeated_sequence_is_a_duplicate():
    assert _classify(sequences=(11, 11), tail_sequence=11) == (
        "ledger_sequence_duplicated")


def test_a_sequence_that_goes_backwards_regresses():
    assert _classify(sequences=(11, 12, 11), tail_sequence=12) == (
        "ledger_sequence_regressed")


def test_a_first_sequence_below_acknowledged_plus_one_regresses():
    assert _classify(sequences=(5,), tail_sequence=12) == (
        "ledger_sequence_regressed")


def test_an_acknowledgement_beyond_the_ledger_tail_forces_recovery():
    assert _classify(ack_sequence=40, tail_sequence=10) == (
        "ledger_cursor_beyond_tail")


def test_a_ticket_without_a_sequence_is_legacy_and_forces_recovery():
    """A marker written before this protocol carries no sequence at all."""
    assert _classify(sequences=(None,), tail_sequence=11) == (
        "ledger_legacy_record")


def test_an_unknown_ledger_state_forces_recovery():
    """An absent or unreadable sidecar reports no epoch, which never matches."""
    assert _classify(epoch=None, tail_sequence=0) == "ledger_epoch_changed"


# ── the two consumers must agree ───────────────────────────────────────────

def test_two_consumers_on_one_epoch_reconcile():
    assert frontier.reconcile_acknowledgements(("e1", 10), ("e1", 4)) is None


def test_two_consumers_on_different_epochs_cannot_reconcile():
    assert frontier.reconcile_acknowledgements(("e1", 10), ("e2", 10)) == (
        "ledger_acknowledgements_irreconcilable")


def test_a_missing_acknowledgement_reconciles_with_anything():
    assert frontier.reconcile_acknowledgements(None, ("e1", 4)) is None
    assert frontier.reconcile_acknowledgements(("e1", 4), None) is None


# ── the writer ─────────────────────────────────────────────────────────────

def _tickets(app_dir: pathlib.Path):
    raw = frontier.activity_marker_path(app_dir).read_bytes()
    return [json.loads(line) for line in raw.splitlines()]


def test_each_ticket_takes_the_next_global_sequence(tmp_path):
    app_dir = tmp_path / "data"
    for index in range(3):
        assert frontier.record_activity(app_dir, "codex", f"/a/{index}.jsonl")
    assert [item["seq"] for item in _tickets(app_dir)] == [1, 2, 3]
    epochs = {item["epoch"] for item in _tickets(app_dir)}
    assert len(epochs) == 1


def test_the_sequence_is_global_across_providers(tmp_path):
    """One byte cursor is shared, so one counter has to cover both."""
    app_dir = tmp_path / "data"
    frontier.record_activity(app_dir, "codex", "/a/0.jsonl")
    frontier.record_activity(app_dir, "claude", "/b/0.jsonl")
    frontier.record_activity(app_dir, "codex", "/a/1.jsonl")
    assert [item["seq"] for item in _tickets(app_dir)] == [1, 2, 3]


def test_rotation_advances_the_epoch_and_keeps_the_sequence_counting(
    tmp_path, monkeypatch,
):
    app_dir = tmp_path / "data"
    frontier.record_activity(app_dir, "codex", "/a/0.jsonl")
    first = _tickets(app_dir)[0]
    monkeypatch.setattr(frontier, "_MARKER_ROTATE_BYTES", 1)
    frontier.record_activity(app_dir, "codex", "/a/1.jsonl")
    rotated = _tickets(app_dir)
    assert len(rotated) == 1, "rotation keeps only the latest payload"
    assert rotated[0]["seq"] == first["seq"] + 1
    assert rotated[0]["epoch"] != first["epoch"]


def test_a_lost_ledger_state_mints_a_fresh_epoch(tmp_path):
    """A sequence that restarts under the OLD epoch would look continuous."""
    app_dir = tmp_path / "data"
    frontier.record_activity(app_dir, "codex", "/a/0.jsonl")
    before = _tickets(app_dir)[0]["epoch"]
    frontier.ledger_state_path(app_dir).unlink()
    frontier.record_activity(app_dir, "codex", "/a/1.jsonl")
    assert _tickets(app_dir)[-1]["epoch"] != before


# ── the reader, end to end ─────────────────────────────────────────────────

def _store(tmp_path) -> pathlib.Path:
    """The smallest cache store `seed_provider` will certify."""
    path = tmp_path / "cache.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE codex_session_files("
            "path TEXT PRIMARY KEY, size_bytes INTEGER, mtime_ns INTEGER,"
            " last_byte_offset INTEGER, last_ingested_at TEXT,"
            " ingest_complete INTEGER NOT NULL DEFAULT 1,"
            " device_id INTEGER, inode INTEGER)")
        conn.execute(
            "INSERT INTO cache_meta VALUES(?, '1')",
            (frontier.CODEX_FULL_WALK_COMPLETE_KEY,))
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def seeded(tmp_path):
    """A seeded Codex certificate over a store and a root that already exist.

    The rollout is created BEFORE the seed on purpose: creating a file inside
    a tracked root moves that directory's mtime, and `filesystem_changed`
    would then answer every plan before the ledger check this module is
    about ever ran.
    """
    app_dir = tmp_path / "data"
    app_dir.mkdir(parents=True, exist_ok=True)
    roots = (tmp_path / "sessions",)
    roots[0].mkdir(parents=True, exist_ok=True)
    (roots[0] / "rollout.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    conn = sqlite3.connect(_store(tmp_path))
    state = frontier.DashboardIngestFrontier(app_dir)
    assert state.seed_provider("codex", conn, roots=roots), (
        state.last_seed_failure)
    yield state, conn, app_dir, roots
    conn.close()


def _rollout(roots) -> str:
    """The already-created rollout, appended to rather than replaced."""
    path = pathlib.Path(roots[0]) / "rollout.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"a": 1}\n')
    return str(path)


def test_a_plan_over_an_unchanged_ledger_is_caught_up(seeded):
    state, conn, _app_dir, roots = seeded
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("caught_up", "unchanged")


def test_a_plan_over_one_new_ticket_is_targeted(seeded):
    state, conn, app_dir, roots = seeded
    frontier.record_activity(app_dir, "codex", _rollout(roots))
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("targeted", "activity")
    assert plan.ledger_sequence == 1


def test_a_rotation_the_inode_check_cannot_see_still_forces_recovery(seeded):
    """The epoch is what closes the inode-number-reuse hole.

    A filesystem may hand a replacement file the same inode number the old one
    held, and then ``marker_replaced`` never fires. Rotating the epoch under
    the writer's lock is evidence that does not depend on the inode at all, so
    the test simulates exactly that: the marker keeps its recorded identity
    while the ledger generation moves underneath it.
    """
    state, conn, app_dir, roots = seeded
    frontier.record_activity(app_dir, "codex", _rollout(roots))
    marker = frontier.activity_marker_path(app_dir)
    st = marker.stat()
    provider_state = state._states["codex"]
    provider_state.marker_identity = (st.st_dev, st.st_ino)
    written = json.loads(frontier.ledger_state_path(app_dir).read_text())
    frontier.ledger_state_path(app_dir).write_text(
        json.dumps({**written, "epoch": "a-different-generation"}))
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("full", "ledger_epoch_changed")


def test_a_missing_sequence_forces_recovery(seeded):
    state, conn, app_dir, roots = seeded
    target = _rollout(roots)
    frontier.record_activity(app_dir, "codex", target)
    plan = state.plan_provider("codex", conn, roots=roots)
    state.commit_provider(plan, conn, roots=roots)
    # A ticket the consumer never saw: the ledger counter advances while the
    # record itself never reaches the file.
    written = json.loads(frontier.ledger_state_path(app_dir).read_text())
    frontier.ledger_state_path(app_dir).write_text(
        json.dumps({**written, "sequence": written["sequence"] + 5}))
    frontier.record_activity(app_dir, "codex", target)
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("full", "ledger_sequence_gap")


def test_an_acknowledgement_beyond_the_tail_forces_recovery(seeded):
    state, conn, app_dir, roots = seeded
    frontier.record_activity(app_dir, "codex", _rollout(roots))
    plan = state.plan_provider("codex", conn, roots=roots)
    state.commit_provider(plan, conn, roots=roots)
    written = json.loads(frontier.ledger_state_path(app_dir).read_text())
    frontier.ledger_state_path(app_dir).write_text(
        json.dumps({**written, "sequence": 0}))
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("full", "ledger_cursor_beyond_tail")


def test_a_truncated_ledger_forces_recovery(seeded):
    state, conn, app_dir, roots = seeded
    frontier.record_activity(app_dir, "codex", _rollout(roots))
    plan = state.plan_provider("codex", conn, roots=roots)
    state.commit_provider(plan, conn, roots=roots)
    marker = frontier.activity_marker_path(app_dir)
    with marker.open("r+b") as fh:
        fh.truncate(0)
    plan = state.plan_provider("codex", conn, roots=roots)
    assert plan.mode == "full"
    assert plan.reason == "ledger_cursor_beyond_tail", plan.reason


def test_a_malformed_ledger_record_forces_recovery(seeded):
    state, conn, app_dir, roots = seeded
    marker = frontier.activity_marker_path(app_dir)
    with marker.open("ab") as fh:
        fh.write(b"not json\n")
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("full", "malformed")


def test_a_legacy_ticket_forces_one_recovery(seeded):
    """An upgraded install still holds pre-protocol tickets ahead of its cursor."""
    state, conn, app_dir, roots = seeded
    marker = frontier.activity_marker_path(app_dir)
    with marker.open("ab") as fh:
        fh.write(json.dumps(
            {"provider": "codex", "path": _rollout(roots)},
            separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n")
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("full", "ledger_legacy_record")


def test_committing_a_plan_advances_the_acknowledged_sequence(seeded):
    state, conn, app_dir, roots = seeded
    frontier.record_activity(app_dir, "codex", _rollout(roots))
    plan = state.plan_provider("codex", conn, roots=roots)
    state.commit_provider(plan, conn, roots=roots)
    assert state._states["codex"].ack_sequence == 1
    frontier.record_activity(app_dir, "codex", _rollout(roots))
    second = state.plan_provider("codex", conn, roots=roots)
    assert (second.mode, second.reason) == ("targeted", "activity")


def test_another_providers_ticket_still_advances_the_acknowledgement(seeded):
    """One shared byte cursor means one shared counter.

    A Claude ticket consumes a sequence the Codex consumer must acknowledge,
    or the Codex consumer's next plan sees a hole that is not a hole.
    """
    state, conn, app_dir, roots = seeded
    frontier.record_activity(app_dir, "claude", "/elsewhere/a.jsonl")
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("caught_up", "unchanged")
    state.commit_provider(plan, conn, roots=roots)
    assert state._states["codex"].ack_sequence == 1
    frontier.record_activity(app_dir, "codex", _rollout(roots))
    second = state.plan_provider("codex", conn, roots=roots)
    assert (second.mode, second.reason) == ("targeted", "activity")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── the seventh discontinuity: the two consumers cannot be reconciled ───────

def _conversation_store(path: pathlib.Path) -> pathlib.Path:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            "CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE conversation_source_files(path TEXT);"
            "CREATE TABLE codex_conversation_source_files("
            "  path TEXT PRIMARY KEY, size_bytes INTEGER, mtime_ns INTEGER,"
            "  last_byte_offset INTEGER, last_ingested_at TEXT,"
            "  device_id INTEGER, inode INTEGER);"
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def both_consumers(tmp_path):
    """Both frontiers seeded over one app dir, one ledger and one root."""
    app_dir = tmp_path / "data"
    app_dir.mkdir(parents=True, exist_ok=True)
    roots = (tmp_path / "sessions",)
    roots[0].mkdir(parents=True, exist_ok=True)
    (roots[0] / "rollout.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    cache = sqlite3.connect(_store(tmp_path))
    conversations = sqlite3.connect(
        _conversation_store(tmp_path / "conversations.db"))
    accounting = frontier.DashboardIngestFrontier(app_dir)
    transcripts = frontier.ConversationSyncFrontier(app_dir)
    assert accounting.seed_provider("codex", cache, roots=roots), (
        accounting.last_seed_failure)
    assert transcripts.seed_provider("codex", conversations, roots=roots), (
        transcripts.last_seed_failure)
    yield accounting, transcripts, cache, conversations, app_dir, roots
    cache.close()
    conversations.close()


def _rotate_ledger_generation(app_dir: pathlib.Path) -> None:
    """Move the ledger onto a new generation without touching the marker."""
    written = json.loads(frontier.ledger_state_path(app_dir).read_text())
    frontier.ledger_state_path(app_dir).write_text(json.dumps(
        {**written, "epoch": "a-generation-nobody-acknowledged"}))


def test_two_consumers_on_different_generations_force_recovery(both_consumers):
    """One consumer recovering alone is not a shared frontier.

    After a rotation the accounting consumer's own continuity check answers
    ``ledger_epoch_changed`` and its recovery walk reseeds it onto the new
    generation. The transcript consumer has not planned since, so it is still
    acknowledging the generation before the rotation. At that point the
    accounting consumer's OWN evidence is self-consistent and every other
    guard is satisfied, so nothing but the cross-consumer comparison can see
    that the two of them no longer describe one ledger.
    """
    accounting, _transcripts, cache, _conv, app_dir, roots = both_consumers
    _rotate_ledger_generation(app_dir)

    recovery = accounting.plan_provider("codex", cache, roots=roots)
    assert (recovery.mode, recovery.reason) == ("full", "ledger_epoch_changed")
    accounting.commit_provider(recovery, cache, roots=roots)

    plan = accounting.plan_provider("codex", cache, roots=roots)
    assert (plan.mode, plan.reason) == (
        "full", "ledger_acknowledgements_irreconcilable")


def test_the_transcript_consumer_sees_it_from_its_own_side(both_consumers):
    _accounting, transcripts, _cache, conversations, app_dir, roots = (
        both_consumers)
    _rotate_ledger_generation(app_dir)

    recovery = transcripts.plan_provider("codex", conversations, roots=roots)
    assert (recovery.mode, recovery.reason) == ("full", "ledger_epoch_changed")
    transcripts.commit_provider(recovery, conversations, roots=roots)

    plan = transcripts.plan_provider("codex", conversations, roots=roots)
    assert (plan.mode, plan.reason) == (
        "full", "ledger_acknowledgements_irreconcilable")


def test_the_escalation_clears_once_both_consumers_recover(both_consumers):
    """Fail-closed, and self-correcting — not a permanent full-walk loop."""
    accounting, transcripts, cache, conversations, app_dir, roots = (
        both_consumers)
    _rotate_ledger_generation(app_dir)
    for owner, conn in ((accounting, cache), (transcripts, conversations)):
        recovery = owner.plan_provider("codex", conn, roots=roots)
        assert recovery.mode == "full"
        owner.commit_provider(recovery, conn, roots=roots)

    plan = accounting.plan_provider("codex", cache, roots=roots)
    assert (plan.mode, plan.reason) == ("caught_up", "unchanged")


def test_two_consumers_at_different_sequences_still_reconcile(both_consumers):
    """They plan and commit independently, so they MUST be allowed to differ."""
    accounting, _transcripts, cache, _conv, app_dir, roots = both_consumers
    frontier.record_activity(app_dir, "codex", _rollout(roots))
    plan = accounting.plan_provider("codex", cache, roots=roots)
    accounting.commit_provider(plan, cache, roots=roots)
    assert accounting._states["codex"].ack_sequence == 1

    caught_up = accounting.plan_provider("codex", cache, roots=roots)
    assert caught_up.mode in {"caught_up", "targeted"}
    assert caught_up.reason != "ledger_acknowledgements_irreconcilable"


def test_a_dropped_consumer_stops_being_reconciled_against(both_consumers):
    """`drop_provider` abandons the certificate, so its cursor is not evidence."""
    accounting, transcripts, cache, _conv, app_dir, roots = both_consumers
    _rotate_ledger_generation(app_dir)
    assert transcripts.drop_provider("codex") is True

    recovery = accounting.plan_provider("codex", cache, roots=roots)
    assert recovery.mode == "full"
    accounting.commit_provider(recovery, cache, roots=roots)

    plan = accounting.plan_provider("codex", cache, roots=roots)
    assert (plan.mode, plan.reason) == ("caught_up", "unchanged")
