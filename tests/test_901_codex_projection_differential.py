"""#901 G9: the Codex search projection is maintained differentially (Q12).

Spec ``docs/superpowers/specs/2026-10-03-901-dashboard-disk-writes.md`` §5.3b
(W6) and §6.2 G9. ``materialize_codex_find_projection`` builds a conversation's
complete projection in memory and writes only the difference against the stored
rows; ``codex_find_projection_generation`` advances exactly once per committing
transaction that changes the find-visible state (its own row changes, or a
writer's message deletions that cascade projection rows away) and never for a
true no-op. Every conversation here is built through the product's own ingest
(``sync_codex_cache`` + ``sync_codex_conversations`` over synthetic rollouts in a
temporary ``CODEX_HOME``). Projection mutations are counted by TEMP triggers that
exist only on the test's connection, and the generation's committed advances by
a TEMP log on ``cache_meta``; both roll back with the transaction they observe.

A rebuild reference is a fresh store that ingests the same rollout files from
byte zero (compared on every column, the surrogate ``message_id`` replaced by
the message's physical position), plus a same-store rebuild compared on every
column including ``message_id``.

RED proof (spec G9): (a), (b) and (g) fail on the pre-revision-11 projector,
which deletes and re-inserts every row of the conversation and advances the
generation on every call. Equivalence (c) alone passes there.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import pathlib
import re
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths_without_conversation_retention

BIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

SESSION_ID = "90190190-1901-4901-8901-901901901901"
THREAD_SOURCE = "g9-thread-source"
MODEL = "gpt-synthetic-codex"
CWD = "/synthetic/root-a/project-red"
BASE = dt.datetime(2026, 7, 20, 0, 0, 0, tzinfo=dt.timezone.utc)
#: Seconds between the starts of two consecutive synthetic turns. A turn uses
#: offsets 0..6; offsets 7..9 are free for a record placed between two turns.
TURN_SPACING = 10
#: Normalized messages one synthetic turn produces (user, reasoning, call,
#: output, assistant). The fixtures assert the actual count after ingest.
MESSAGES_PER_TURN = 5
SIZES = (50, 500, 2000)
QUERY = "needle"

_PROJECTION_COLUMNS = (
    "conversation_key,item_key,block_key,container_block_key,surface,"
    "render_order,projected_text,leaves_json,disclosure_json,projection_version"
)


# ── synthetic rollout records ────────────────────────────────────────────────


def _ts(seconds: int) -> str:
    return (BASE + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(kind: str, payload: dict, seconds: int) -> dict:
    return {"payload": payload, "timestamp": _ts(seconds), "type": kind}


def _session_meta(seconds: int) -> dict:
    return _record("session_meta", {
        "context_window": 272000,
        "cwd": CWD,
        "git": {"branch": "g9-branch", "repository": "g9-repository"},
        "id": SESSION_ID,
        "model": MODEL,
        "model_provider": "g9-provider",
        "session_id": SESSION_ID,
        "source": "codex",
        "thread_source": THREAD_SOURCE,
    }, seconds)


def _turn_context(turn_id: str, seconds: int) -> dict:
    return _record("turn_context", {
        "model": MODEL, "model_context_window": 272000, "turn_id": turn_id,
    }, seconds)


def _user(text: str, seconds: int) -> dict:
    return _record("response_item", {
        "content": [{"text": text, "type": "input_text"}],
        "phase": "input", "role": "user", "type": "message",
    }, seconds)


def _assistant(text: str, seconds: int) -> dict:
    return _record("response_item", {
        "content": [{"text": text, "type": "output_text"}],
        "phase": "output", "role": "assistant", "type": "message",
    }, seconds)


def _agent_message(text: str, seconds: int) -> dict:
    return _record("event_msg", {
        "memory_citation": None, "message": text, "phase": "final",
        "type": "agent_message",
    }, seconds)


def _reasoning(text: str, seconds: int) -> dict:
    return _record("response_item", {
        "content": [{"text": text, "type": "reasoning_text"}],
        "encrypted_content": "g9-encrypted",
        "summary": [{"text": f"Summary of {text}", "type": "summary_text"}],
        "type": "reasoning",
    }, seconds)


def _call(call_id: str, seconds: int) -> dict:
    return _record("response_item", {
        "arguments": json.dumps({"path": f"{call_id}.txt"}),
        "call_id": call_id, "name": "fixture_function", "type": "function_call",
    }, seconds)


def _output(call_id: str, text: str, seconds: int) -> dict:
    return _record("response_item", {
        "call_id": call_id, "output": {"ok": True, "text": text},
        "type": "function_call_output",
    }, seconds)


def _token_count(total: int, seconds: int) -> dict:
    return _record("event_msg", {
        "info": {
            "last_token_usage": {
                "cached_input_tokens": 60, "input_tokens": 240,
                "output_tokens": 80, "reasoning_output_tokens": 20,
                "total_tokens": 320,
            },
            "model_context_window": 272000,
            "rate_limits": {},
            "total_token_usage": {"total_tokens": total},
        },
        "type": "token_count",
    }, seconds)


def _task_complete(turn_id: str, seconds: int) -> dict:
    return _record("event_msg", {
        "completed_at": _ts(seconds), "duration_ms": 1000,
        "last_agent_message": "resumed answer", "turn_id": turn_id,
        "type": "task_complete",
    }, seconds)


def _turn(number: int, start: int, *, label: str = "a") -> list[dict]:
    """One complete turn: five normalized messages plus its token count."""
    turn_id = f"turn-{label}-{number:05d}"
    call_id = f"call-{label}-{number:05d}"
    return [
        _turn_context(turn_id, start),
        _user(f"Prompt {label}{number} asks about the {QUERY} {number}", start + 1),
        _reasoning(f"Reasoning {label}{number} weighs the {QUERY}", start + 2),
        _call(call_id, start + 3),
        _output(call_id, f"output {label}{number} {QUERY}", start + 4),
        _assistant(f"Answer {label}{number} names the {QUERY}", start + 5),
        _token_count(320 * (number + 1), start + 6),
    ]


def _conversation(turns: int, *, label: str = "a") -> list[dict]:
    records = [_session_meta(0)]
    for number in range(turns):
        records.extend(_turn(number, 1 + number * TURN_SPACING, label=label))
    return records


def _turns_for(messages: int) -> int:
    return -(-messages // MESSAGES_PER_TURN)


# ── harness ──────────────────────────────────────────────────────────────────


_COUNTERS = """
CREATE TEMP TABLE IF NOT EXISTS g9_projection_ops(op TEXT NOT NULL);
CREATE TEMP TABLE IF NOT EXISTS g9_generation_log(old_value TEXT, new_value TEXT);
CREATE TEMP TRIGGER IF NOT EXISTS g9_projection_ai
AFTER INSERT ON codex_find_projection BEGIN
    INSERT INTO g9_projection_ops(op) VALUES ('insert');
END;
CREATE TEMP TRIGGER IF NOT EXISTS g9_projection_au
AFTER UPDATE ON codex_find_projection BEGIN
    INSERT INTO g9_projection_ops(op) VALUES ('update');
END;
CREATE TEMP TRIGGER IF NOT EXISTS g9_projection_ad
AFTER DELETE ON codex_find_projection BEGIN
    INSERT INTO g9_projection_ops(op) VALUES ('delete');
END;
CREATE TEMP TRIGGER IF NOT EXISTS g9_generation_ai
AFTER INSERT ON cache_meta WHEN new.key = 'codex_find_projection_generation'
BEGIN
    INSERT INTO g9_generation_log VALUES (NULL, new.value);
END;
CREATE TEMP TRIGGER IF NOT EXISTS g9_generation_au
AFTER UPDATE ON cache_meta WHEN new.key = 'codex_find_projection_generation'
BEGIN
    INSERT INTO g9_generation_log VALUES (old.value, new.value);
END;
"""


class Store:
    """One primary store over one synthetic ``CODEX_HOME``."""

    def __init__(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        self.ns = load_script()
        self.monkeypatch = monkeypatch
        self.tmp = tmp_path
        self.provider = tmp_path / "provider"
        self.sessions = self.provider / "sessions" / "2026" / "07" / "20"
        self.sessions.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CODEX_HOME", str(self.provider))
        self.primary = tmp_path / "primary"
        self._rebuilds = 0
        self._activate(self.primary)
        import _lib_codex_conversation_query as query
        import _cctally_cache as cache

        self.query = query
        self.cache = cache
        self.conn = self.ns["open_conversations_db"]()
        self.conn.executescript(_COUNTERS)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- files -----------------------------------------------------------------

    def path(self, name: str) -> pathlib.Path:
        return self.sessions / f"rollout-{name}.jsonl"

    def write(self, name: str, records: list[dict]) -> pathlib.Path:
        path = self.path(name)
        with path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
        return path

    def append(self, name: str, records: list[dict]) -> int:
        """Append records; return the byte offset where the append starts."""
        path = self.path(name)
        start = path.stat().st_size
        with path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
        return start

    def truncate_to_lines(self, name: str, lines: int) -> None:
        path = self.path(name)
        kept = path.read_text(encoding="utf-8").splitlines(keepends=True)[:lines]
        path.write_text("".join(kept), encoding="utf-8")

    # -- passes ----------------------------------------------------------------

    def _activate(self, data_dir: pathlib.Path) -> None:
        redirect_paths_without_conversation_retention(
            self.ns, self.monkeypatch, data_dir)

    def _sync_cache(self) -> None:
        core = self.ns["open_cache_db"]()
        try:
            self.ns["sync_codex_cache"](core)
        finally:
            core.close()

    def reset_counters(self) -> None:
        self.conn.execute("DELETE FROM g9_projection_ops")
        self.conn.execute("DELETE FROM g9_generation_log")
        self.conn.commit()

    def ops(self) -> dict[str, int]:
        return {
            op: count for op, count in self.conn.execute(
                "SELECT op, COUNT(*) FROM g9_projection_ops GROUP BY op")
        }

    def generation_log(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT old_value, new_value FROM g9_generation_log ORDER BY rowid"
        ).fetchall()

    def sync(self):
        """One ordinary conversation-sync pass, counters reset first."""
        self._sync_cache()
        self.reset_counters()
        stats = self.ns["sync_codex_conversations"](self.conn)
        assert stats.files_failed == 0, stats
        return stats

    def generation(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_find_projection_generation'"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def conversation_key(self) -> str:
        keys = [
            row[0] for row in self.conn.execute(
                "SELECT DISTINCT conversation_key FROM codex_conversation_messages")
        ]
        assert len(keys) == 1, keys
        return keys[0]

    def message_count(self, key: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages "
            "WHERE conversation_key=?", (key,),
        ).fetchone()[0]

    # -- projection views ------------------------------------------------------

    def exact(self, key: str) -> list[tuple]:
        return self.conn.execute(
            "SELECT message_id," + _PROJECTION_COLUMNS + " FROM codex_find_projection "
            "WHERE conversation_key=? ORDER BY message_id, surface", (key,),
        ).fetchall()

    def same_store_rebuild(self, key: str) -> list[tuple]:
        """The whole-conversation rebuild over the same message rows."""
        self.conn.commit()
        try:
            self.conn.execute(
                "DELETE FROM codex_find_projection WHERE conversation_key=?", (key,))
            self.query.materialize_codex_find_projection(self.conn, [key])
            return self.exact(key)
        finally:
            self.conn.rollback()

    def rebuild_reference(self, key: str) -> list[tuple]:
        """A fresh store that ingests the same rollout files from byte zero."""
        self._rebuilds += 1
        self._activate(self.tmp / f"rebuild-{self._rebuilds}")
        try:
            self._sync_cache()
            conn = self.ns["open_conversations_db"]()
            try:
                stats = self.ns["sync_codex_conversations"](conn)
                assert stats.files_failed == 0, stats
                return physical(conn, key)
            finally:
                conn.close()
        finally:
            self._activate(self.primary)

    def assert_equals_rebuild(self, key: str) -> None:
        assert self.exact(key) == self.same_store_rebuild(key)
        assert physical(self.conn, key) == self.rebuild_reference(key)

    # -- find ------------------------------------------------------------------

    def find(self, key: str, *, limit: int = 200, cursor: str | None = None):
        return self.query.find_occurrences_in_codex_conversation(
            self.conn, key, QUERY, regex=False, case_sensitive=False,
            kind="all", limit=limit, cursor=cursor,
        )

    def all_occurrences(self, key: str, *, limit: int = 7) -> list[list[dict]]:
        pages = []
        result = self.find(key, limit=limit)
        while True:
            pages.append(result["page"]["occurrences"])
            cursor = result["page"]["next_cursor"]
            if cursor is None:
                return pages
            result = self.find(key, limit=limit, cursor=cursor)


def physical(conn: sqlite3.Connection, key: str) -> list[tuple]:
    """Every projection column, with ``message_id`` replaced by its message's
    physical position (the rowid alias differs between two stores)."""
    return sorted(conn.execute(
        "SELECT m.source_path, m.line_offset, "
        "p." + _PROJECTION_COLUMNS.replace(",", ",p.") + " "
        "FROM codex_find_projection p "
        "JOIN codex_conversation_messages m ON m.id = p.message_id "
        "WHERE p.conversation_key=?", (key,),
    ).fetchall())


def assert_single_advance_or_none(store: Store, before: int) -> int:
    """(j): the observed transaction advanced the generation by 0 or 1."""
    log = store.generation_log()
    after = store.generation()
    assert len(log) in (0, 1), log
    assert after - before == len(log), (before, after, log)
    return len(log)


@pytest.fixture
def store(tmp_path, monkeypatch):
    built = Store(tmp_path, monkeypatch)
    try:
        yield built
    finally:
        built.close()


def _built(tmp_path, monkeypatch, messages: int) -> tuple[Store, str]:
    built = Store(tmp_path, monkeypatch)
    built.write("main", _conversation(_turns_for(messages)))
    built.sync()
    key = built.conversation_key()
    assert built.message_count(key) >= messages
    return built, key


# ── (a) no-op re-materialization ─────────────────────────────────────────────


@pytest.mark.parametrize("messages", SIZES)
def test_a_reprojecting_an_unchanged_conversation_writes_nothing(
    tmp_path, monkeypatch, messages,
):
    store, key = _built(tmp_path, monkeypatch, messages)
    try:
        before_rows = store.exact(key)
        before_generation = store.generation()
        assert before_rows, "the fixture must project rows"

        store.reset_counters()
        store.query.materialize_codex_find_projection(store.conn, [key])
        store.conn.commit()
        assert store.ops() == {}, (
            "re-projecting an unchanged conversation must change no row")
        assert store.generation() == before_generation
        assert assert_single_advance_or_none(store, before_generation) == 0
        assert store.exact(key) == before_rows

        # An ordinary pass with no appended bytes is a no-op as well.
        store.sync()
        assert store.ops() == {}
        assert assert_single_advance_or_none(store, before_generation) == 0
        assert store.exact(key) == before_rows
    finally:
        store.close()


# ── (b) a tail append writes rows in proportion to the append ────────────────


def test_b_a_tail_append_changes_only_the_appended_rows_at_every_size(
    tmp_path, monkeypatch,
):
    per_size: dict[int, dict[str, int]] = {}
    for messages in SIZES:
        store, key = _built(tmp_path / f"n{messages}", monkeypatch, messages)
        try:
            turns = _turns_for(messages)
            before = store.exact(key)
            before_generation = store.generation()
            appended_at = store.append(
                "main", _turn(turns, 1 + turns * TURN_SPACING))
            store.sync()
            ops = store.ops()
            appended_rows = store.conn.execute(
                "SELECT COUNT(*) FROM codex_find_projection p "
                "JOIN codex_conversation_messages m ON m.id = p.message_id "
                "WHERE m.source_path=? AND m.line_offset>=?",
                (str(store.path("main")), appended_at),
            ).fetchone()[0]
            assert appended_rows > 0
            assert ops == {"insert": appended_rows}, (messages, ops)
            # Every earlier row is untouched, column for column.
            assert set(before) <= set(store.exact(key))
            assert assert_single_advance_or_none(store, before_generation) == 1
            per_size[messages] = ops
        finally:
            store.close()
    assert len({tuple(sorted(ops.items())) for ops in per_size.values()}) == 1, (
        f"the append's row count grew with history: {per_size}")


# ── (c) equivalence to a whole rebuild after every shape of change ──────────


def _render_order_of(store: Store, key: str, text_fragment: str) -> int:
    row = store.conn.execute(
        "SELECT render_order FROM codex_find_projection "
        "WHERE conversation_key=? AND projected_text LIKE ?",
        (key, f"%{text_fragment}%"),
    ).fetchone()
    assert row is not None, text_fragment
    return row[0]


def test_c_a_fold_equals_a_rebuild_and_advances(store):
    records = _conversation(6)
    final = 1 + 6 * TURN_SPACING
    records.extend([
        _turn_context("turn-fold", final),
        _user(f"Fold prompt with {QUERY}", final + 1),
        _call("call-fold", final + 2),
    ])
    store.write("main", records)
    store.sync()
    key = store.conversation_key()
    before_generation = store.generation()

    store.append("main", [
        _output("call-fold", f"fold output {QUERY}", final + 3),
        _assistant(f"Fold answer {QUERY}", final + 4),
    ])
    store.sync()
    assert store.ops(), "the fold must change the projection"
    assert assert_single_advance_or_none(store, before_generation) == 1
    store.assert_equals_rebuild(key)


def test_c_a_mirror_pair_equals_a_rebuild_and_advances(store):
    records = _conversation(6)
    final = 1 + 6 * TURN_SPACING
    records.extend([
        _turn_context("turn-mirror", final),
        _user(f"Mirror prompt with {QUERY}", final + 1),
        # The event member arrives first and is projected on its own.
        _agent_message(f"Mirror answer {QUERY}", final + 2),
    ])
    store.write("main", records)
    store.sync()
    key = store.conversation_key()
    before_generation = store.generation()

    # The canonical response_item member arrives: the pair suppresses the
    # event row, so the projection swaps one surface for the other.
    store.append("main", [_assistant(f"Mirror answer {QUERY}", final + 3)])
    store.sync()
    assert store.ops().get("delete", 0) >= 1
    assert assert_single_advance_or_none(store, before_generation) == 1
    store.assert_equals_rebuild(key)


def test_c_a_late_turn_anchor_equals_a_rebuild_and_advances(store):
    records = _conversation(5)
    resume = 1 + 5 * TURN_SPACING
    records.extend([
        _session_meta(resume),
        _reasoning(f"Resumed reasoning {QUERY}", resume + 1),
        _call("call-resumed", resume + 2),
        _output("call-resumed", f"resumed output {QUERY}", resume + 3),
        _assistant(f"Resumed answer {QUERY}", resume + 4),
    ])
    store.write("main", records)
    store.sync()
    key = store.conversation_key()
    assert store.conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_messages "
        "WHERE conversation_key=? AND turn_id IS NULL", (key,),
    ).fetchone()[0] >= 4, "the resumed prefix starts unanchored"
    before_generation = store.generation()

    store.append("main", [_task_complete("turn-late", resume + 5)])
    store.sync()
    assert store.conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_messages "
        "WHERE conversation_key=? AND turn_id='turn-late'", (key,),
    ).fetchone()[0] >= 4, "the late anchor must repair the prefix"
    assert store.ops(), "the repaired turn must change the projection"
    assert assert_single_advance_or_none(store, before_generation) == 1
    store.assert_equals_rebuild(key)


def _interleaved_file(turn_after: int) -> list[dict]:
    """A second file of the same conversation whose one turn falls between
    the main file's turns ``turn_after`` and ``turn_after + 1``."""
    start = 1 + turn_after * TURN_SPACING + 7
    return [
        _session_meta(start),
        _turn_context("turn-side", start),
        _user(f"Side prompt {QUERY}", start + 1),
        _assistant(f"Side answer {QUERY}", start + 2),
    ]


def test_c_a_deleted_message_cascade_equals_a_rebuild_and_advances(store):
    store.write("main", _conversation(8))
    store.write("side", _interleaved_file(3))
    store.sync()
    key = store.conversation_key()
    later = _render_order_of(store, key, "Answer a7 names")
    before_generation = store.generation()

    store.path("side").unlink()
    stats = store.sync()
    assert stats.files_pruned == 1
    assert _render_order_of(store, key, "Answer a7 names") < later, (
        "removing the interleaved file must shift later ordinals")
    assert assert_single_advance_or_none(store, before_generation) == 1
    store.assert_equals_rebuild(key)


def test_c_an_insertion_that_shifts_later_ordinals_equals_a_rebuild(store):
    store.write("main", _conversation(8))
    store.sync()
    key = store.conversation_key()
    later = _render_order_of(store, key, "Answer a7 names")
    before_generation = store.generation()

    store.write("side", _interleaved_file(3))
    store.sync()
    assert _render_order_of(store, key, "Answer a7 names") > later, (
        "the interleaved file must shift later ordinals")
    assert assert_single_advance_or_none(store, before_generation) == 1
    store.assert_equals_rebuild(key)


# ── (d) atomicity with the source cursor ─────────────────────────────────────


def test_d_a_failure_after_the_projection_write_rolls_back_both(
    store, monkeypatch,
):
    store.write("main", _conversation(6))
    store.sync()
    key = store.conversation_key()
    before_rows = store.exact(key)
    before_generation = store.generation()
    before_cursor = store.conn.execute(
        "SELECT last_byte_offset FROM codex_conversation_source_files"
    ).fetchall()

    store.append("main", _turn(6, 1 + 6 * TURN_SPACING))
    real = store.query.materialize_codex_find_projection
    calls = {"n": 0}

    def fail_after_projection(conn, keys, **kwargs):
        real(conn, keys, **kwargs)
        calls["n"] += 1
        raise sqlite3.OperationalError("g9 injected failure after projection")

    monkeypatch.setattr(
        store.query, "materialize_codex_find_projection", fail_after_projection)
    store._sync_cache()
    store.reset_counters()
    stats = store.ns["sync_codex_conversations"](store.conn)
    monkeypatch.setattr(store.query, "materialize_codex_find_projection", real)
    assert calls["n"] == 1 and stats.files_failed == 1
    assert store.exact(key) == before_rows
    assert store.generation() == before_generation
    assert store.ops() == {}
    assert store.conn.execute(
        "SELECT last_byte_offset FROM codex_conversation_source_files"
    ).fetchall() == before_cursor

    store.sync()
    assert assert_single_advance_or_none(store, before_generation) == 1
    store.assert_equals_rebuild(key)


# ── (e) render invalidation with equal aggregates ───────────────────────────


def test_e_equal_aggregates_with_changed_render_inputs_still_advance_render_revision(
    store,
):
    records = _conversation(4)
    last = 1 + 3 * TURN_SPACING
    store.write("main", records)
    store.sync()
    key = store.conversation_key()

    def rollup():
        return store.conn.execute(
            "SELECT source_root_key,parent_thread_id,item_count,started_utc,"
            "last_activity_utc,project_key,project_label,models_json,title,"
            "render_revision FROM codex_conversation_rollups "
            "WHERE conversation_key=?", (key,),
        ).fetchone()

    before = rollup()
    messages_before = store.message_count(key)
    # The suppressed event mirror of the final answer: a new retained render
    # input whose aggregates (items, times, title, models) are all unchanged.
    store.append("main", [_agent_message("Answer a3 names the needle", last + 5)])
    store.sync()
    assert store.message_count(key) == messages_before + 1
    after = rollup()
    assert after[:-1] == before[:-1], "aggregate values are unchanged"
    assert after[-1] > before[-1], "render_revision must still advance"


def test_e_a_suppressed_mirror_of_a_projected_answer_is_a_find_no_op(store):
    store.write("main", _conversation(4))
    store.sync()
    key = store.conversation_key()
    before_rows = store.exact(key)
    before_generation = store.generation()
    store.append("main", [
        _agent_message("Answer a3 names the needle", 1 + 3 * TURN_SPACING + 5)])
    store.sync()
    assert store.ops() == {}
    assert assert_single_advance_or_none(store, before_generation) == 0
    assert store.exact(key) == before_rows


# ── (f) find results, pages and cursors ──────────────────────────────────────


def test_f_find_pages_survive_a_no_op_and_a_visible_change_invalidates_cursors(
    store,
):
    store.write("main", _conversation(8))
    store.sync()
    key = store.conversation_key()
    first = store.find(key, limit=3)
    cursor = first["page"]["next_cursor"]
    assert cursor is not None
    second = store.find(key, limit=3, cursor=cursor)
    pages = store.all_occurrences(key)

    # A no-op pass and a forced re-projection keep every page and cursor.
    store.sync()
    store.query.materialize_codex_find_projection(store.conn, [key])
    store.conn.commit()
    assert store.find(key, limit=3) == first
    assert store.find(key, limit=3, cursor=cursor) == second
    assert store.all_occurrences(key) == pages

    # A visible change invalidates the cursor taken before it.
    store.append("main", _turn(8, 1 + 8 * TURN_SPACING))
    store.sync()
    with pytest.raises(store.query.StaleFindCursor):
        store.find(key, limit=3, cursor=cursor)

    # The maintained projection pages exactly as a from-zero rebuild does.
    maintained = store.all_occurrences(key)
    store.conn.commit()
    store.conn.execute("DELETE FROM codex_find_projection WHERE conversation_key=?",
                       (key,))
    store.query.materialize_codex_find_projection(store.conn, [key])
    try:
        assert store.all_occurrences(key) == maintained
    finally:
        store.conn.rollback()


# ── (g) cascade-only changes: the generation still advances exactly once ────


def _two_file_conversation(store: Store) -> tuple[str, int]:
    """A main file plus a later file whose only projected message is the
    conversation's final one."""
    store.write("main", _conversation(6))
    tail = 1 + 6 * TURN_SPACING + 100
    store.write("tail", [
        _session_meta(tail),
        _turn_context("turn-tail", tail + 1),
        _user(f"Tail prompt with the {QUERY}", tail + 2),
    ])
    store.sync()
    key = store.conversation_key()
    tail_rows = store.conn.execute(
        "SELECT COUNT(*) FROM codex_find_projection p "
        "JOIN codex_conversation_messages m ON m.id = p.message_id "
        "WHERE m.source_path=?", (str(store.path("tail")),),
    ).fetchone()[0]
    assert tail_rows == 1, "the tail file projects exactly the final message"
    return key, tail_rows


def _mutate_tail(store: Store, mutation: str) -> None:
    if mutation == "truncation":
        store.truncate_to_lines("tail", 2)
    else:
        store.path("tail").unlink()


@pytest.mark.parametrize("mutation", ("truncation", "stale-source-prune"))
def test_g_a_cascade_only_change_advances_once_and_invalidates_cursors(
    store, mutation,
):
    key, tail_rows = _two_file_conversation(store)
    surviving = [
        row for row in store.exact(key)
        if row[7] != f"Tail prompt with the {QUERY}"
    ]
    cursor = store.find(key, limit=2)["page"]["next_cursor"]
    assert cursor is not None
    before_generation = store.generation()

    _mutate_tail(store, mutation)
    stats = store.sync()
    if mutation == "truncation":
        assert stats.files_reset_truncated == 1
    else:
        assert stats.files_pruned == 1
    # Only the cascade removed rows: the builder found no difference.
    assert store.ops() == {"delete": tail_rows}
    assert store.exact(key) == surviving
    assert assert_single_advance_or_none(store, before_generation) == 1
    with pytest.raises(store.query.StaleFindCursor):
        store.find(key, limit=2, cursor=cursor)
    store.assert_equals_rebuild(key)


@pytest.mark.parametrize("mutation", ("truncation", "stale-source-prune"))
def test_g_the_same_cascade_rolled_back_changes_nothing(
    store, monkeypatch, mutation,
):
    key, _tail_rows = _two_file_conversation(store)
    before_rows = store.exact(key)
    first = store.find(key, limit=2)
    cursor = first["page"]["next_cursor"]
    second = store.find(key, limit=2, cursor=cursor)
    before_generation = store.generation()

    real = store.query.materialize_codex_find_projection

    def fail_after_projection(conn, keys, **kwargs):
        real(conn, keys, **kwargs)
        raise sqlite3.OperationalError("g9 injected failure after projection")

    _mutate_tail(store, mutation)
    monkeypatch.setattr(
        store.query, "materialize_codex_find_projection", fail_after_projection)
    store._sync_cache()
    store.reset_counters()
    try:
        store.ns["sync_codex_conversations"](store.conn)
    except sqlite3.OperationalError:
        # The prune transaction does not catch; its caller rolls back.
        store.conn.rollback()
    finally:
        monkeypatch.setattr(store.query, "materialize_codex_find_projection", real)
    assert store.exact(key) == before_rows
    assert store.generation() == before_generation
    assert store.generation_log() == []
    assert store.find(key, limit=2, cursor=cursor) == second


# ── (h) retention deletes a whole conversation ───────────────────────────────


def test_h_a_retention_deletion_leaves_no_find_result_and_no_cursor_rows(store):
    store.write("main", _conversation(6))
    store.sync()
    key = store.conversation_key()
    cursor = store.find(key, limit=2)["page"]["next_cursor"]
    assert cursor is not None

    import _lib_conversation_retention as retention

    retention._delete_codex_conversation_derived(store.conn, key)
    store.conn.commit()
    assert store.conn.execute(
        "SELECT COUNT(*) FROM codex_find_projection WHERE conversation_key=?",
        (key,),
    ).fetchone()[0] == 0
    gone = store.find(key, limit=2)
    assert gone["status"] == "not_found" and "page" not in gone
    after_cursor = store.find(key, limit=2, cursor=cursor)
    assert after_cursor["status"] == "not_found" and "page" not in after_cursor


# ── (i) the find reader's one joined filter is immutable ────────────────────


_KIND_UPDATE = re.compile(
    r"\bUPDATE\s+(?:OR\s+\w+\s+)?(?:\w+\.)?codex_conversation_messages\s+SET\s+"
    r"(?P<set>.*?)(?:\bWHERE\b|\bFROM\b|\bRETURNING\b|$)",
    re.IGNORECASE | re.DOTALL,
)


def _string_fragments(tree: ast.AST) -> list[str]:
    """Every string a module can hand SQLite: literals, implicit and ``+``
    concatenations of literals, and f-strings' literal parts."""
    out: list[str] = []

    def flatten(node) -> "str | None":
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(
                part.value if isinstance(part, ast.Constant) else " ? "
                for part in node.values
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = flatten(node.left), flatten(node.right)
            if left is not None or right is not None:
                return (left or " ? ") + (right or " ? ")
        return None

    for node in ast.walk(tree):
        text = flatten(node)
        if text is not None:
            out.append(text)
    return out


def kind_updates(source: str) -> list[str]:
    hits = []
    for text in _string_fragments(ast.parse(source)):
        for match in _KIND_UPDATE.finditer(" ".join(text.split())):
            if re.search(r"(?<![\w.])kind\s*=", match.group("set")):
                hits.append(match.group(0))
    return hits


def test_i_no_writer_in_bin_updates_a_message_kind():
    # Non-vacuity: the scanner recognises every spelling a writer could use.
    assert kind_updates('X = "UPDATE codex_conversation_messages SET kind=?"')
    assert kind_updates(
        'X = ("UPDATE codex_conversation_messages "\n "SET text=?, kind = ? "'
        '\n "WHERE id=?")')
    assert kind_updates(
        'X = "UPDATE main.codex_conversation_messages SET " + "kind=?"')
    assert not kind_updates(
        'X = "UPDATE codex_conversation_messages SET turn_id=? WHERE kind=?"')
    assert not kind_updates(
        'X = "UPDATE codex_conversation_messages SET meta_kind=?"')

    sources = sorted(BIN_DIR.glob("*.py")) + [BIN_DIR / "cctally"]
    assert len(sources) > 50
    offenders = {
        path.name: hits
        for path in sources
        if (hits := kind_updates(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}
