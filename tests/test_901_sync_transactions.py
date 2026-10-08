"""#901 G10: sync transactions, metadata writes and checkpoints (Q13).

Spec ``docs/superpowers/specs/2026-10-03-901-dashboard-disk-writes.md`` §4.9,
§5.3c (W7–W9) and §6.2 G10. Every store here is built through the product's
own ingest (``sync_codex_cache`` + ``sync_codex_conversations``) over synthetic
rollouts in a temporary ``CODEX_HOME``; nothing reads a real data directory.

Measurement. A transaction is what the connection's trace shows between two
``COMMIT`` statements. A committed file is one cursor upsert inside a committed
transaction, matched in order to a TEMP cursor log that only committed
transactions keep (a rolled-back savepoint takes its log row with it). WAL
frames are counted from the ``-wal`` file of a store whose connection runs
``wal_autocheckpoint = 0`` with the writer-owned checkpoint policy switched off
through its test seam, so nothing checkpoints during a measured pass and the WAL
only grows.

RED proof (spec G10): (a) fails on the per-file-transaction tree, where nine
changed files commit nine transactions, exactly as the one-file control does.
(b) is a correctness guard.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import sqlite3
import struct
import subprocess
import sys
import time
from dataclasses import dataclass

import pytest

from conftest import load_script, redirect_paths_without_conversation_retention

BIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "bin"
TESTS_DIR = pathlib.Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

KIB = 1024
BASE = dt.datetime(2026, 7, 20, 0, 0, 0, tzinfo=dt.timezone.utc)
MODEL = "gpt-synthetic-codex"
QUERY = "needle"
#: Seconds between two synthetic turns of one file.
TURN_SPACING = 10


# ── synthetic rollout records ────────────────────────────────────────────────


def _ts(seconds: int) -> str:
    return (BASE + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(kind: str, payload: dict, seconds: int) -> dict:
    return {"payload": payload, "timestamp": _ts(seconds), "type": kind}


def _session_id(conversation: str) -> str:
    digest = hashlib.sha256(conversation.encode("utf-8")).hexdigest()
    return (
        f"{digest[:8]}-{digest[8:12]}-4{digest[13:16]}-8{digest[17:20]}-"
        f"{digest[20:32]}"
    )


def _session_meta(conversation: str, seconds: int) -> dict:
    session_id = _session_id(conversation)
    return _record("session_meta", {
        "context_window": 272000,
        "cwd": f"/synthetic/g10/{conversation}",
        "git": {"branch": "g10-branch", "repository": "g10-repository"},
        "id": session_id,
        "model": MODEL,
        "model_provider": "g10-provider",
        "session_id": session_id,
        "source": "codex",
        "thread_source": "g10-thread-source",
    }, seconds)


def _turn(conversation: str, number: int, start: int, *, pad: str = "") -> list[dict]:
    """One complete turn: five normalized messages plus its token count."""
    label = f"{conversation}-{number:04d}"
    turn_id = f"turn-{label}"
    call_id = f"call-{label}"
    return [
        _record("turn_context", {
            "model": MODEL, "model_context_window": 272000, "turn_id": turn_id,
        }, start),
        _record("response_item", {
            "content": [{
                "text": f"Prompt {label} asks about the {QUERY} {pad}",
                "type": "input_text",
            }],
            "phase": "input", "role": "user", "type": "message",
        }, start + 1),
        _record("response_item", {
            "content": [{"text": f"Reasoning {label} weighs the {QUERY}",
                         "type": "reasoning_text"}],
            "encrypted_content": "g10-encrypted",
            "summary": [{"text": f"Summary of {label}", "type": "summary_text"}],
            "type": "reasoning",
        }, start + 2),
        _record("response_item", {
            "arguments": json.dumps({"path": f"{call_id}.txt"}),
            "call_id": call_id, "name": "fixture_function",
            "type": "function_call",
        }, start + 3),
        _record("response_item", {
            "call_id": call_id, "output": {"ok": True, "text": f"output {label}"},
            "type": "function_call_output",
        }, start + 4),
        _record("response_item", {
            "content": [{"text": f"Answer {label} names the {QUERY}",
                         "type": "output_text"}],
            "phase": "output", "role": "assistant", "type": "message",
        }, start + 5),
        _record("event_msg", {
            "info": {
                "last_token_usage": {
                    "cached_input_tokens": 60, "input_tokens": 240,
                    "output_tokens": 80, "reasoning_output_tokens": 20,
                    "total_tokens": 320,
                },
                "model_context_window": 272000,
                "rate_limits": {},
                "total_token_usage": {"total_tokens": 320 * (number + 1)},
            },
            "type": "token_count",
        }, start + 6),
    ]


def _lines(records: list[dict]) -> str:
    return "".join(json.dumps(record) + "\n" for record in records)


@dataclass
class Rollout:
    """One synthetic rollout file: a conversation and its next turn number."""

    name: str
    conversation: str
    path: pathlib.Path
    next_turn: int
    clock: int


# ── stores ───────────────────────────────────────────────────────────────────


_PROBES = """
CREATE TEMP TABLE IF NOT EXISTS g10_generation_log(old_value TEXT, new_value TEXT);
CREATE TEMP TABLE IF NOT EXISTS g10_cursor_log(
    path TEXT NOT NULL, old_offset INTEGER, new_offset INTEGER NOT NULL);
CREATE TEMP TRIGGER IF NOT EXISTS g10_generation_ai
AFTER INSERT ON cache_meta WHEN new.key = 'codex_find_projection_generation'
BEGIN
    INSERT INTO g10_generation_log VALUES (NULL, new.value);
END;
CREATE TEMP TRIGGER IF NOT EXISTS g10_generation_au
AFTER UPDATE ON cache_meta WHEN new.key = 'codex_find_projection_generation'
BEGIN
    INSERT INTO g10_generation_log VALUES (old.value, new.value);
END;
CREATE TEMP TRIGGER IF NOT EXISTS g10_cursor_ai
AFTER INSERT ON codex_conversation_source_files BEGIN
    INSERT INTO g10_cursor_log VALUES (new.path, NULL, new.last_byte_offset);
END;
CREATE TEMP TRIGGER IF NOT EXISTS g10_cursor_au
AFTER UPDATE ON codex_conversation_source_files BEGIN
    INSERT INTO g10_cursor_log VALUES (
        new.path, old.last_byte_offset, new.last_byte_offset);
END;
"""

_CURSOR_UPSERT = "INSERT INTO CODEX_CONVERSATION_SOURCE_FILES"
_GENERATION_KEY = "codex_find_projection_generation"


def wal_frames(db_path) -> int:
    """Frames in a ``-wal`` file that only grows (``wal_autocheckpoint = 0``)."""
    wal = pathlib.Path(f"{db_path}-wal")
    try:
        data = wal.read_bytes()[:32]
        size = wal.stat().st_size
    except OSError:
        return 0
    if size < 32 or len(data) < 32:
        return 0
    page_size = struct.unpack(">I", data[8:12])[0]
    return (size - 32) // (page_size + 24)


@dataclass
class Transaction:
    statements: list[str]

    @property
    def cursor_upserts(self) -> int:
        return sum(
            1 for s in self.statements
            if s.lstrip().upper().startswith(_CURSOR_UPSERT)
        )

    @property
    def generation_writes(self) -> int:
        return sum(
            1 for s in self.statements
            if _GENERATION_KEY in s and not s.lstrip().startswith("--")
            and s.lstrip().upper().startswith("INSERT")
        )


def split_transactions(statements: list[str]) -> list[Transaction]:
    """Committed transactions, in order; a full ROLLBACK discards its own.

    CPython traces the expanded SQL of the running statement, and SQLite calls
    the trace again at the start of every trigger program that statement fires,
    so one execution can appear several times, with the nested statements of a
    virtual table (FTS5's ``-- REPLACE INTO ...``) in between. Nested
    statements are dropped, and consecutive duplicates are one execution: every
    traced write that matters here is either parameterized with a fresh value
    (the cursor's ingest time) or separated from its next execution by other
    top-level statements.
    """
    committed: list[Transaction] = []
    current: list[str] = []
    previous = None
    for statement in statements:
        if statement.lstrip().startswith("--") or statement == previous:
            continue
        previous = statement
        head = statement.lstrip().upper()
        if head.startswith("COMMIT") or head.startswith("END"):
            committed.append(Transaction(current))
            current = []
        elif head.startswith("ROLLBACK") and not head.startswith("ROLLBACK TO"):
            current = []
        else:
            current.append(statement)
    return committed


@dataclass
class PassTrace:
    stats: object
    transactions: list[Transaction]
    #: Committed cursor advances, in commit order: (path, consumed bytes).
    cursors: list[tuple[str, int]]
    frames: int
    generation_advances: int
    #: (files_processed, in_transaction) at every "ingest" progress report.
    progress: list[tuple[int, bool]]

    @property
    def ingest_transactions(self) -> list[Transaction]:
        return [t for t in self.transactions if t.cursor_upserts]

    def files_per_transaction(self) -> list[list[tuple[str, int]]]:
        grouped = []
        remaining = list(self.cursors)
        for transaction in self.ingest_transactions:
            grouped.append(remaining[:transaction.cursor_upserts])
            remaining = remaining[transaction.cursor_upserts:]
        assert not remaining, "every committed cursor belongs to a transaction"
        return grouped


class Env:
    """One synthetic ``CODEX_HOME`` shared by several independent stores."""

    def __init__(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        self.ns = load_script()
        self.mp = monkeypatch
        self.tmp = tmp_path
        self.provider = tmp_path / "provider"
        self.sessions = self.provider / "sessions" / "2026" / "07" / "20"
        self.sessions.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CODEX_HOME", str(self.provider))
        # Frames are read from the -wal file's length, which is only a frame
        # count while nothing checkpoints; the stores below run
        # wal_autocheckpoint = 0 themselves.
        monkeypatch.setenv("CCTALLY_TEST_W9_DISABLE", "1")
        import _cctally_cache as cache
        import _lib_codex_conversation_query as query

        self.cache = cache
        self.query = query
        self.rollouts: dict[str, Rollout] = {}

    # -- files -----------------------------------------------------------------

    def rollout(self, name: str, conversation: str, turns: int, *,
                start: int = 0, first_turn: int = 0,
                exact_bytes: "int | None" = None) -> Rollout:
        path = self.sessions / f"rollout-{name}.jsonl"
        rollout = Rollout(name, conversation, path, first_turn, start)

        def render(pad: str) -> str:
            records = [_session_meta(conversation, start)]
            for index in range(turns):
                records.extend(_turn(
                    conversation, first_turn + index,
                    start + 1 + index * TURN_SPACING,
                    pad=pad if index == 0 else "",
                ))
            return _lines(records)

        text = render("")
        if exact_bytes is not None:
            missing = exact_bytes - len(text.encode("utf-8"))
            assert missing >= 0, (name, exact_bytes, len(text))
            text = render("x" * missing)
            assert len(text.encode("utf-8")) == exact_bytes
        path.write_text(text, encoding="utf-8")
        rollout.next_turn = first_turn + turns
        rollout.clock = start + 1 + turns * TURN_SPACING
        self.rollouts[name] = rollout
        return rollout

    def append(self, name: str, turns: int = 1) -> int:
        """Append whole turns to a rollout; return the bytes appended."""
        rollout = self.rollouts[name]
        records = []
        for index in range(turns):
            records.extend(_turn(
                rollout.conversation, rollout.next_turn + index,
                rollout.clock + index * TURN_SPACING))
        text = _lines(records)
        with rollout.path.open("a", encoding="utf-8") as fh:
            fh.write(text)
        rollout.next_turn += turns
        rollout.clock += turns * TURN_SPACING
        return len(text.encode("utf-8"))

    def truncate_lines(self, name: str, lines: int) -> None:
        path = self.rollouts[name].path
        kept = path.read_text(encoding="utf-8").splitlines(keepends=True)[:lines]
        path.write_text("".join(kept), encoding="utf-8")

    def store(self, name: str) -> "Store":
        return Store(self, self.tmp / name)


class Store:
    def __init__(self, env: Env, data_dir: pathlib.Path) -> None:
        self.env = env
        self.data_dir = data_dir
        self.activate()
        self.conn = env.ns["open_conversations_db"]()
        self.conn.execute("PRAGMA wal_autocheckpoint = 0")
        self.conn.executescript(_PROBES)
        self.conn.commit()

    @property
    def db_path(self) -> pathlib.Path:
        return self.data_dir / ".local" / "share" / "cctally" / "conversations.db"

    def activate(self) -> None:
        redirect_paths_without_conversation_retention(
            self.env.ns, self.env.mp, self.data_dir)

    def close(self) -> None:
        self.conn.close()

    def sync_cache(self) -> None:
        self.activate()
        core = self.env.ns["open_cache_db"]()
        try:
            self.env.cache.sync_codex_cache(core)
        finally:
            core.close()

    def run_pass(self, *, batch_files: "int | None" = None,
                 expect_failed: int = 0) -> PassTrace:
        self.sync_cache()
        self.activate()
        conn = self.conn
        conn.execute("DELETE FROM g10_cursor_log")
        conn.execute("DELETE FROM g10_generation_log")
        conn.commit()
        statements: list[str] = []
        progress: list[tuple[int, bool]] = []

        def on_progress(phase, stats):
            if phase == "ingest":
                progress.append((stats.files_processed, conn.in_transaction))

        frames_before = wal_frames(self.db_path)
        with pytest.MonkeyPatch.context() as patch:
            if batch_files is not None:
                patch.setattr(
                    self.env.cache, "CODEX_CONVERSATION_BATCH_FILES",
                    batch_files, raising=False)
            conn.set_trace_callback(statements.append)
            try:
                stats = self.env.cache.sync_codex_conversations(
                    conn, progress=on_progress)
            finally:
                conn.set_trace_callback(None)
        frames = wal_frames(self.db_path) - frames_before
        assert stats.files_failed == expect_failed, stats
        cursors = [
            (path, int(new) - int(old or 0))
            for path, old, new in conn.execute(
                "SELECT path, old_offset, new_offset FROM g10_cursor_log "
                "ORDER BY rowid")
        ]
        advances = conn.execute(
            "SELECT COUNT(*) FROM g10_generation_log").fetchone()[0]
        return PassTrace(
            stats, split_transactions(statements), cursors, frames, advances,
            progress)

    # -- views -----------------------------------------------------------------

    def keys(self) -> list[str]:
        return [row[0] for row in self.conn.execute(
            "SELECT conversation_key FROM codex_conversation_rollups "
            "ORDER BY conversation_key")]

    def key_for(self, rollout: Rollout) -> str:
        rows = {row[0] for row in self.conn.execute(
            "SELECT DISTINCT conversation_key FROM codex_conversation_messages "
            "WHERE source_path=?", (str(rollout.path),))}
        assert len(rows) == 1, rows
        return rows.pop()

    def render_revisions(self) -> dict[str, int]:
        return dict(self.conn.execute(
            "SELECT conversation_key, render_revision "
            "FROM codex_conversation_rollups"))

    def generation(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (_GENERATION_KEY,)
        ).fetchone()
        return int(row[0]) if row else 0

    def cursor(self, rollout: Rollout):
        return self.conn.execute(
            "SELECT size_bytes, last_byte_offset, last_conversation_key "
            "FROM codex_conversation_source_files WHERE path=?",
            (str(rollout.path),),
        ).fetchone()

    def rows_for(self, rollout: Rollout) -> dict[str, list]:
        path = str(rollout.path)
        return {
            "events": self.conn.execute(
                "SELECT line_offset, payload_json FROM codex_conversation_events "
                "WHERE source_path=? ORDER BY line_offset", (path,)).fetchall(),
            "messages": self.conn.execute(
                "SELECT id, line_offset, kind, text FROM codex_conversation_messages "
                "WHERE source_path=? ORDER BY line_offset", (path,)).fetchall(),
        }

    def rollup(self, key: str):
        return self.conn.execute(
            "SELECT * FROM codex_conversation_rollups WHERE conversation_key=?",
            (key,)).fetchone()

    def projection(self, key: str) -> list[tuple]:
        return self.conn.execute(
            "SELECT * FROM codex_find_projection WHERE conversation_key=? "
            "ORDER BY message_id, surface", (key,)).fetchall()

    def find(self, key: str, *, limit: int = 2, cursor: "str | None" = None):
        return self.env.query.find_occurrences_in_codex_conversation(
            self.conn, key, QUERY, regex=False, case_sensitive=False,
            kind="all", limit=limit, cursor=cursor)

    def outline(self, key: str):
        return self.env.query.get_codex_conversation_outline(
            self.conn, key, effective_speed="standard")


def _columns(conn: sqlite3.Connection, table: str, drop: set[str]) -> str:
    return ",".join(
        row[1] for row in conn.execute(f"PRAGMA table_info({table})")
        if row[1] not in drop)


def semantic(conn: sqlite3.Connection) -> dict[str, list]:
    """Every stored row on its semantic columns (surrogate ids, ingest times
    and render revisions removed; message ids replaced by physical position)."""
    out: dict[str, list] = {}
    for table, drop in (
        ("codex_conversation_events", {"id"}),
        ("codex_conversation_messages", {"id"}),
        ("codex_conversation_source_files", {"last_ingested_at"}),
        ("codex_conversation_rollups", {"render_revision"}),
    ):
        out[table] = sorted(
            conn.execute(
                f"SELECT {_columns(conn, table, drop)} FROM {table}").fetchall(),
            key=repr)
    out["codex_conversation_file_touches"] = sorted(conn.execute(
        "SELECT m.source_path, m.line_offset, t.conversation_key, t.source_path, "
        "t.file_path, t.tool FROM codex_conversation_file_touches t "
        "JOIN codex_conversation_messages m ON m.id = t.message_id"
    ).fetchall(), key=repr)
    projection_columns = _columns(conn, "codex_find_projection", {"message_id"})
    out["codex_find_projection"] = sorted(conn.execute(
        "SELECT m.source_path, m.line_offset, "
        + ",".join(f"p.{name}" for name in projection_columns.split(","))
        + " FROM codex_find_projection p "
        "JOIN codex_conversation_messages m ON m.id = p.message_id"
    ).fetchall(), key=repr)
    return out


def assert_same(actual: dict[str, list], expected: dict[str, list]) -> None:
    """Per-table equality that names only the rows that differ."""
    assert sorted(actual) == sorted(expected)
    for table in sorted(expected):
        missing = [row for row in expected[table] if row not in actual[table]]
        extra = [row for row in actual[table] if row not in expected[table]]
        if missing or extra or len(actual[table]) != len(expected[table]):
            pytest.fail(
                f"{table} differs\nmissing: {missing!r}\nextra: {extra!r}",
                pytrace=False)


def from_zero(env: Env, name: str) -> dict[str, list]:
    """A fresh store ingesting the same rollout files from byte zero."""
    store = env.store(name)
    try:
        store.run_pass()
        return semantic(store.conn)
    finally:
        store.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# ── (a) bounded batches inside one Codex conversation-sync pass ─────────────


def _assert_bounded(trace: PassTrace) -> None:
    for files in trace.files_per_transaction():
        consumed = sum(size for _path, size in files)
        if len(files) == 1:
            continue
        assert len(files) <= 4, files
        assert consumed <= 256 * KIB, files


def _nine_file_corpus(env: Env) -> list[Rollout]:
    """Ten small rollout files over four conversations, two to three files
    each, so batches cross conversation boundaries."""
    layout = (
        ("01", "alpha", 0), ("02", "alpha", 1000), ("03", "bravo", 0),
        ("04", "charlie", 0), ("05", "bravo", 1000), ("06", "alpha", 2000),
        ("07", "delta", 0), ("08", "charlie", 1000), ("09", "delta", 1000),
        ("10", "charlie", 2000),
    )
    return [
        env.rollout(name, conversation, 2, start=start,
                    first_turn=start // 10)
        for name, conversation, start in layout
    ]


def test_a_small_changed_files_share_bounded_transactions(env):
    rollouts = _nine_file_corpus(env)
    batched = env.store("batched")
    control = env.store("control")
    try:
        first = batched.run_pass()
        first_control = control.run_pass(batch_files=1)
        _assert_bounded(first)
        assert len(first_control.ingest_transactions) == len(rollouts)
        assert len(first.ingest_transactions) < len(rollouts), (
            "ten new small files must not commit ten transactions")

        keys = batched.keys()
        assert len(keys) == 4
        stale_cursor = batched.find(keys[0])["page"]["next_cursor"]
        assert stale_cursor is not None
        for key in keys:
            batched.outline(key)
        revisions_before = batched.render_revisions()
        generation_before = batched.generation()

        for rollout in rollouts:
            env.append(rollout.name)
        trace = batched.run_pass()
        control_trace = control.run_pass(batch_files=1)

        # Bounded, batched, and cheaper than one file per transaction.
        _assert_bounded(trace)
        assert [len(files) for files in trace.files_per_transaction()] == [4, 4, 2]
        assert len(control_trace.ingest_transactions) == len(rollouts)
        assert len(trace.ingest_transactions) < len(control_trace.ingest_transactions)
        assert trace.frames < control_trace.frames, (trace.frames, control_trace.frames)
        assert trace.stats.files_processed == len(rollouts)
        assert control_trace.stats.files_processed == len(rollouts)

        # The generation advanced exactly once per committed transaction that
        # changed find-visible state (every appended turn is find-visible).
        assert all(t.generation_writes == 1 for t in trace.ingest_transactions)
        assert trace.generation_advances == len(trace.ingest_transactions)
        assert batched.generation() - generation_before == trace.generation_advances
        assert trace.generation_advances < control_trace.generation_advances

        # Progress is reported only once a batch has committed.
        assert trace.progress and all(
            not in_transaction for _processed, in_transaction in trace.progress)
        assert [p for p, _ in trace.progress if p] == [4, 8, 10]

        # Render, find and outline invalidation for every changed conversation.
        revisions_after = batched.render_revisions()
        assert all(revisions_after[k] > revisions_before[k] for k in keys)
        with pytest.raises(env.query.StaleFindCursor):
            batched.find(keys[0], cursor=stale_cursor)
        real = env.query._outline_envelope
        misses: list[str] = []

        def spy(conn, conversation_key, **kwargs):
            misses.append(conversation_key)
            return real(conn, conversation_key, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(env.query, "_outline_envelope", spy)
            for key in keys:
                batched.outline(key)
        assert sorted(misses) == keys

        # Same stored content as the control and as a from-zero rebuild.
        batched_rows = semantic(batched.conn)
        assert_same(batched_rows, semantic(control.conn))
        assert_same(batched_rows, from_zero(env, "rebuild"))
    finally:
        batched.close()
        control.close()


def test_a_exactly_four_small_files_commit_together(env):
    for index in range(4):
        env.rollout(f"0{index}", f"conv{index}", 1)
    store = env.store("store")
    try:
        trace = store.run_pass()
        assert [len(f) for f in trace.files_per_transaction()] == [4]
    finally:
        store.close()


def test_a_four_64_kib_files_commit_together_and_four_80_kib_files_do_not(env):
    for index in range(4):
        env.rollout(f"a{index}", f"exact{index}", 1, exact_bytes=64 * KIB)
    store = env.store("store")
    try:
        trace = store.run_pass()
        groups = trace.files_per_transaction()
        assert [len(f) for f in groups] == [4]
        assert sum(size for _p, size in groups[0]) == 256 * KIB

        for index in range(4):
            env.rollout(f"b{index}", f"over{index}", 1, exact_bytes=80 * KIB)
        trace = store.run_pass()
        groups = trace.files_per_transaction()
        assert [len(f) for f in groups] == [3, 1]
        _assert_bounded(trace)
    finally:
        store.close()


def test_a_an_oversized_file_between_small_ones_runs_alone(env):
    env.rollout("01", "small1", 1)
    env.rollout("02", "small2", 1)
    big = env.rollout("03", "big", 1, exact_bytes=300 * KIB)
    env.rollout("04", "small3", 1)
    env.rollout("05", "small4", 1)
    store = env.store("store")
    try:
        trace = store.run_pass()
        groups = trace.files_per_transaction()
        assert [len(f) for f in groups] == [2, 1, 2]
        assert groups[1] == [(str(big.path), 300 * KIB)]
        _assert_bounded(trace)
    finally:
        store.close()


# ── (b) a failing file, and a crash before the batch's COMMIT ───────────────


def _four_conversations(env: Env) -> list[Rollout]:
    return [env.rollout(f"0{i}", f"conv{i}", 3, start=0) for i in range(4)]


def test_b_an_error_in_the_third_file_rolls_back_that_file_alone(env):
    rollouts = _four_conversations(env)
    store = env.store("store")
    # The same input without the injected error: "converges" means the retried
    # pass ends where an uninterrupted pass does. (A truncation reset seeds the
    # cursor's token total from the replaced generation, so a from-zero rebuild
    # is not the reference for that one column; that is today's behaviour.)
    control = env.store("control")
    try:
        store.run_pass()
        control.run_pass()
        third = rollouts[2]
        third_key = store.key_for(third)
        before_rows = store.rows_for(third)
        before_cursor = store.cursor(third)
        before_rollup = store.rollup(third_key)
        before_projection = store.projection(third_key)
        before_others = {r.name: store.cursor(r) for r in rollouts}

        env.append(rollouts[0].name)
        env.append(rollouts[1].name)
        # The third file is truncated, so its pass deletes rows before the
        # injected error: the savepoint must restore them and must not count
        # the reset.
        env.truncate_lines(third.name, 8)
        env.append(rollouts[3].name)

        real = env.cache._insert_codex_normalized_rows

        def failing(conn, rows, touches, account_by_physical):
            real(conn, rows, touches, account_by_physical)
            if any(row.source_path == str(third.path) for row in rows):
                raise sqlite3.OperationalError("g10 injected failure")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(env.cache, "_insert_codex_normalized_rows", failing)
            trace = store.run_pass(expect_failed=1)

        assert trace.stats.files_processed == 3
        assert trace.stats.files_reset_truncated == 0
        groups = trace.files_per_transaction()
        assert [len(f) for f in groups] == [3], "the batch's other files commit"
        assert {path for path, _ in groups[0]} == {
            str(r.path) for i, r in enumerate(rollouts) if i != 2}
        assert store.rows_for(third) == before_rows
        assert store.cursor(third) == before_cursor
        assert store.rollup(third_key) == before_rollup
        assert store.projection(third_key) == before_projection
        for index, rollout in enumerate(rollouts):
            if index != 2:
                assert store.cursor(rollout) != before_others[rollout.name]
                assert store.cursor(rollout)[1] == rollout.path.stat().st_size

        # The next pass converges.
        retry = store.run_pass()
        assert retry.stats.files_processed == 1
        assert retry.stats.files_reset_truncated == 1
        uninterrupted = control.run_pass()
        assert uninterrupted.stats.files_processed == 4
        assert_same(semantic(store.conn), semantic(control.conn))
    finally:
        store.close()
        control.close()


_CRASH_CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import pytest
from conftest import load_script, redirect_paths_without_conversation_retention
import pathlib
os.environ["CODEX_HOME"] = sys.argv[4]
os.environ["CCTALLY_TEST_W9_DISABLE"] = "1"
ns = load_script()
patch = pytest.MonkeyPatch()
redirect_paths_without_conversation_retention(ns, patch, pathlib.Path(sys.argv[3]))
import _lib_codex_conversation_query as query
real = query.materialize_codex_find_projection


def crash(conn, keys, **kwargs):
    real(conn, keys, **kwargs)
    os._exit(17)


query.materialize_codex_find_projection = crash
conn = ns["open_conversations_db"]()
ns["sync_codex_conversations"](conn)
os._exit(0)
"""


def test_b_a_crash_before_the_batch_commit_leaves_no_file_committed(env):
    rollouts = _four_conversations(env)
    store = env.store("store")
    try:
        store.run_pass()
        for rollout in rollouts[:3]:
            env.append(rollout.name)
        store.sync_cache()
        before = {r.name: (store.cursor(r), store.rows_for(r)) for r in rollouts}
        snapshot = semantic(store.conn)
    finally:
        store.close()

    child = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD, str(TESTS_DIR), str(BIN_DIR),
         str(store.data_dir), str(env.provider)],
        capture_output=True, text=True, timeout=60,
    )
    assert child.returncode == 17, child.stderr[-2000:]

    store = env.store("store")
    try:
        assert {
            r.name: (store.cursor(r), store.rows_for(r)) for r in rollouts
        } == before, "no file of the crashed batch committed"
        assert_same(semantic(store.conn), snapshot)
        retry = store.run_pass()
        assert retry.stats.files_processed == 3
        assert_same(semantic(store.conn), from_zero(env, "rebuild"))
    finally:
        store.close()


# ── (c) a pass with no new data writes nothing ──────────────────────────────


_READ_HEADS = ("SELECT", "PRAGMA", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT",
               "RELEASE", "WITH", "ATTACH", "DETACH", "EXPLAIN", "--")


def _claude_line(session: str, number: int, text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "uuid": f"{session}-u{number}",
        "sessionId": session,
        "requestId": f"{session}-r{number}",
        "timestamp": _ts(number),
        "cwd": "/synthetic/g10/claude",
        "message": {
            "role": "assistant",
            "id": f"{session}-m{number}",
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }) + "\n"


class Pinned:
    """Hold a read transaction on each store, so no close or automatic
    checkpoint can reset its WAL during a measured window: the ``-wal`` file
    then only grows, and its growth is the frames the window wrote."""

    def __init__(self, *paths: pathlib.Path) -> None:
        self.paths = paths
        self.conns = []
        for path in paths:
            conn = sqlite3.connect(path)
            conn.execute("BEGIN")
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            self.conns.append(conn)
        self.before = [wal_frames(path) for path in paths]

    def frames(self) -> dict[str, int]:
        return {
            path.name: wal_frames(path) - before
            for path, before in zip(self.paths, self.before)
        }

    def close(self) -> None:
        for conn in self.conns:
            conn.rollback()
            conn.close()


class Writes:
    """Every write statement any store connection runs in a window."""

    def __init__(self, env: Env) -> None:
        import _cctally_store as store_mod

        self.store_mod = store_mod
        self.statements: list[str] = []
        self.previous = store_mod._TRACE_HOOK

    def __enter__(self) -> "Writes":
        self.store_mod._TRACE_HOOK = self.statements.append
        return self

    def __exit__(self, *exc) -> None:
        self.store_mod._TRACE_HOOK = self.previous

    def writes(self) -> list[str]:
        return sorted({
            s.strip()[:160] for s in self.statements
            if not s.lstrip().upper().startswith(_READ_HEADS)
        })


def _tick_cache_sync(env: Env) -> None:
    """One dashboard tick's cache sync: a fresh connection, both legs."""
    conn = env.ns["open_cache_db"]()
    try:
        env.cache.sync_cache(conn)
        env.cache.sync_codex_cache(conn)
    finally:
        conn.close()


def _conversation_pass(env: Env) -> None:
    """One whole conversation-sync pass: open, both providers, close."""
    conn = env.ns["open_conversations_db"]()
    try:
        env.cache.sync_claude_conversations(conn)
        env.cache.sync_codex_conversations(conn)
    finally:
        conn.close()


def _no_op_fixture(env: Env, tmp_path: pathlib.Path) -> pathlib.Path:
    for index in range(3):
        env.rollout(f"0{index}", f"conv{index}", 2)
    data = tmp_path / "store"
    redirect_paths_without_conversation_retention(env.ns, env.mp, data)
    projects = data / ".claude" / "projects" / "-synthetic-g10-claude"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "session-g10.jsonl").write_text(
        "".join(_claude_line("session-g10", n, f"claude {QUERY} {n}")
                for n in range(4)),
        encoding="utf-8")
    return data / ".local" / "share" / "cctally"


def test_c_a_tick_and_a_conversation_pass_with_no_new_data_write_no_frames(
    env, tmp_path,
):
    share = _no_op_fixture(env, tmp_path)
    cache_db = share / "cache.db"
    conversations_db = share / "conversations.db"
    _tick_cache_sync(env)
    _conversation_pass(env)
    stats = []
    conn = env.ns["open_conversations_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_source_files").fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_source_files").fetchone()[0] == 1
    finally:
        conn.close()
    # One more round settles state that a first pass legitimately completes
    # (backfills, first-observation markers).
    _tick_cache_sync(env)
    _conversation_pass(env)

    # Measured through the product's own open–sync–close cycle on the now
    # current-schema stores, each opening its connections through the full
    # writable opener as the dashboard does, never on an already-open
    # connection (Q14): an opener that rewrites page 1 on every open writes a
    # frame per pass however little the pass itself writes.
    pinned = Pinned(cache_db, conversations_db)
    try:
        with Writes(env) as tick:
            _tick_cache_sync(env)
        tick_frames = pinned.frames()
        with Writes(env) as conversation:
            _conversation_pass(env)
        pass_frames = {
            name: pinned.frames()[name] - tick_frames[name]
            for name in tick_frames
        }
    finally:
        pinned.close()
    failures = []
    if tick_frames != {"cache.db": 0, "conversations.db": 0}:
        failures.append(
            f"a no-op tick wrote {tick_frames}:\n" + "\n".join(tick.writes()))
    if pass_frames != {"cache.db": 0, "conversations.db": 0}:
        failures.append(
            f"a no-op conversation pass wrote {pass_frames}:\n"
            + "\n".join(conversation.writes()))
    if failures:
        pytest.fail("\n\n".join(failures), pytrace=False)
    del stats


# ── (d)-(f) cache.db: the accounting cursor, incarnations, one finalization ─


def _quota_record(seconds: int, used: float) -> dict:
    """A quota-only ``token_count``: a weekly window and no spend."""
    reset = int((BASE + dt.timedelta(days=5)).timestamp())
    return _record("event_msg", {
        "type": "token_count",
        "info": {
            "rate_limits": {
                "limit_id": "codex",
                "limit_name": "codex",
                "plan_type": "pro",
                "primary": {
                    "used_percent": float(used),
                    "window_minutes": 10080,
                    "resets_at": reset,
                },
            },
        },
    }, seconds)


def _messages_only(conversation: str, number: int, start: int) -> list[dict]:
    """A user prompt and an answer with no token count: no accounting row."""
    label = f"{conversation}-m{number:04d}"
    return [
        _record("response_item", {
            "content": [{"text": f"Prompt {label}", "type": "input_text"}],
            "phase": "input", "role": "user", "type": "message",
        }, start),
        _record("response_item", {
            "content": [{"text": f"Answer {label}", "type": "output_text"}],
            "phase": "output", "role": "assistant", "type": "message",
        }, start + 1),
    ]


class CacheStore:
    """One cache.db over the shared synthetic ``CODEX_HOME``."""

    _PROBES = """
    CREATE TEMP TABLE IF NOT EXISTS g10_incarnation_writes(op TEXT NOT NULL);
    CREATE TEMP TRIGGER IF NOT EXISTS g10_incarnation_ai
    AFTER INSERT ON codex_file_incarnations BEGIN
        INSERT INTO g10_incarnation_writes VALUES ('insert');
    END;
    CREATE TEMP TRIGGER IF NOT EXISTS g10_incarnation_au
    AFTER UPDATE ON codex_file_incarnations BEGIN
        INSERT INTO g10_incarnation_writes VALUES ('update');
    END;
    """

    def __init__(self, env: Env, name: str) -> None:
        self.env = env
        self.data_dir = env.tmp / name
        self.activate()
        self.conn = env.ns["open_cache_db"]()
        self.conn.executescript(self._PROBES)
        self.conn.commit()

    def activate(self) -> None:
        redirect_paths_without_conversation_retention(
            self.env.ns, self.env.mp, self.data_dir)

    def close(self) -> None:
        self.conn.close()

    def sync(self, **kwargs):
        self.activate()
        self.conn.execute("DELETE FROM g10_incarnation_writes")
        self.conn.commit()
        stats = self.env.cache.sync_codex_cache(self.conn, **kwargs)
        assert stats.files_failed == 0, stats
        return stats

    def change_log(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT mutation_seq, change_kind, source_root_key, source_path "
            "FROM codex_accounting_change_log ORDER BY mutation_seq").fetchall()

    def accounting_seq(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_accounting_mutation_seq'").fetchone()
        return int(row[0]) if row else 0

    def file_row(self, rollout: Rollout):
        return self.conn.execute(
            "SELECT size_bytes, last_byte_offset, last_native_thread_id "
            "FROM codex_session_files WHERE path=?", (str(rollout.path),),
        ).fetchone()

    def incarnation_writes(self) -> list[str]:
        return [row[0] for row in self.conn.execute(
            "SELECT op FROM g10_incarnation_writes ORDER BY rowid")]

    def accounting(self) -> dict[str, list]:
        """Accounting and quota results on their semantic columns."""
        out: dict[str, list] = {}
        for table, drop in (
            ("codex_session_entries", {"id"}),
            ("quota_window_snapshots", {"id"}),
            ("codex_session_files", {"last_ingested_at"}),
            ("codex_file_incarnations", {"updated_at_utc"}),
            ("codex_file_accounts", {"decided_at_utc"}),
            ("codex_conversation_threads", {"first_seen_utc", "last_seen_utc"}),
        ):
            out[table] = sorted(self.conn.execute(
                f"SELECT {_columns(self.conn, table, drop)} FROM {table}"
            ).fetchall(), key=repr)
        quota = sys.modules["_cctally_quota"]
        out["observations"] = sorted(
            (repr(obs) for obs in quota.load_codex_quota_observations(
                cache_conn=self.conn)))
        return out


def _accounting_rollout(env: Env, name: str, conversation: str) -> Rollout:
    rollout = env.rollout(name, conversation, 2)
    with rollout.path.open("a", encoding="utf-8") as fh:
        fh.write(_lines([_quota_record(rollout.clock, 10.0)]))
    rollout.clock += 1
    return rollout


def test_d_an_append_advances_the_cursor_without_an_accounting_log_entry(env):
    store = CacheStore(env, "store")
    try:
        main = _accounting_rollout(env, "01", "main")
        store.sync()
        before_log = store.change_log()
        before_seq = store.accounting_seq()
        before_row = store.file_row(main)

        # Records with no accounting row: only the cursor moves.
        with main.path.open("a", encoding="utf-8") as fh:
            fh.write(_lines(_messages_only("main", 0, main.clock)))
        main.clock += 2
        store.sync()
        after_row = store.file_row(main)
        assert after_row[1] == main.path.stat().st_size > before_row[1]
        assert store.change_log() == before_log, "no artificial invalidation"
        assert store.accounting_seq() == before_seq

        # A new file still logs.
        other = _accounting_rollout(env, "02", "other")
        store.sync()
        logged = {row[3] for row in store.change_log()[len(before_log):]}
        assert str(other.path) in logged
        after_new = store.change_log()

        # An identity change still logs: the file's terminal thread changes
        # with no accounting row appended.
        with main.path.open("a", encoding="utf-8") as fh:
            fh.write(_lines([_session_meta("main-forked", main.clock)]))
        main.clock += 1
        store.sync()
        assert store.file_row(main)[2] != after_row[2], "thread identity moved"
        logged = [row for row in store.change_log()[len(after_new):]]
        assert [row[3] for row in logged] == [str(main.path)]
        assert store.accounting_seq() > before_seq

        # Accounting and quota results equal a from-zero ingest of the input.
        quota_append = _quota_record(main.clock, 20.0)
        with main.path.open("a", encoding="utf-8") as fh:
            fh.write(_lines(_turn("main", 50, main.clock) + [quota_append]))
        store.sync()
        reference = CacheStore(env, "reference")
        try:
            reference.sync()
            assert_same(store.accounting(), reference.accounting())
        finally:
            reference.close()
    finally:
        store.close()


def test_d_the_cursor_upsert_sets_every_column_of_the_table(env):
    """``INSERT OR REPLACE`` rewrote every column; the upsert must set every
    non-key column too, so no column keeps a stale value it used to reset."""
    store = CacheStore(env, "store")
    try:
        columns = {
            row[1] for row in store.conn.execute(
                "PRAGMA table_info(codex_session_files)")}
        import inspect
        source = inspect.getsource(env.cache._write_codex_file_batch)
        statement = source[source.index("INTO codex_session_files"):]
        statement = statement[:statement.index('"""', 3)]
        assert "ON CONFLICT(path) DO UPDATE SET" in statement
        updated = {
            part.split("=")[0].strip()
            for part in statement.split("DO UPDATE SET", 1)[1].split(",")
        }
        assert updated == columns - {"path"}
    finally:
        store.close()


def test_e_re_observing_an_unchanged_identity_writes_no_incarnation_row(env):
    store = CacheStore(env, "store")
    try:
        main = _accounting_rollout(env, "01", "main")
        store.sync()
        incarnations = store.conn.execute(
            "SELECT * FROM codex_file_incarnations").fetchall()
        assert incarnations
        env.append(main.name)
        store.sync()
        assert store.file_row(main)[1] == main.path.stat().st_size
        assert store.incarnation_writes() == []
        assert store.conn.execute(
            "SELECT * FROM codex_file_incarnations").fetchall() == incarnations
    finally:
        store.close()


def test_f_a_failing_maintenance_stage_keeps_the_earlier_stages(
    env, monkeypatch, capsys,
):
    store = CacheStore(env, "store")
    try:
        _accounting_rollout(env, "01", "main")
        store.sync()
        stages: list[str] = []
        db = env.cache._cctally_db_sib
        real_resolution = db.backfill_codex_quota_observed_model
        real_reconcile = env.cache.reconcile_codex_window_attribution_spend
        real_adoption = env.cache.apply_codex_window_spend_adoption

        def resolution(conn):
            stages.append("resolution")
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value) VALUES('g10_stage_1','1')")
            return real_resolution(conn)

        def adoption(conn, *, touched):
            stages.append("adoption")
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value) VALUES('g10_stage_2','1')")
            raise sqlite3.OperationalError("g10 injected adoption failure")

        def reconcile(conn):
            stages.append("reconcile")
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value) VALUES('g10_stage_3','1')")
            return real_reconcile(conn)

        monkeypatch.setattr(db, "backfill_codex_quota_observed_model", resolution)
        monkeypatch.setattr(env.cache, "apply_codex_window_spend_adoption", adoption)
        monkeypatch.setattr(
            env.cache, "reconcile_codex_window_attribution_spend", reconcile)
        env.append("01")
        capsys.readouterr()
        stats = store.sync()
        err = capsys.readouterr().err
        assert stages == ["resolution", "adoption", "reconcile"]
        assert stats.maintenance_failed is True
        assert stats.full_walk_complete is False
        assert "[cache-sync] could not adopt Codex window spend: " \
               "g10 injected adoption failure" in err
        meta = dict(store.conn.execute(
            "SELECT key, value FROM cache_meta WHERE key LIKE 'g10_stage_%'"))
        assert meta == {"g10_stage_1": "1", "g10_stage_3": "1"}
        assert store.conn.execute(
            "SELECT 1 FROM cache_meta "
            "WHERE key='dashboard_codex_full_walk_complete'").fetchone() is None
        assert store.conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='codex_ingest_backlog'"
        ).fetchone() is None
        assert not store.conn.in_transaction

        # Recovery: the next clean sync completes the walk again.
        monkeypatch.setattr(
            env.cache, "apply_codex_window_spend_adoption", real_adoption)
        stats = store.sync()
        assert stats.maintenance_failed is False
        assert stats.full_walk_complete is True
        assert store.conn.execute(
            "SELECT 1 FROM cache_meta "
            "WHERE key='dashboard_codex_full_walk_complete'").fetchone() is not None
    finally:
        store.close()


def test_f_the_finalization_commits_once_after_the_last_file(env):
    """W8: resume and backlog state, parse health, the walk's markers, the
    maintenance stages and the completion marker share one commit, and W11
    (Q14) puts the last batch's files in that same commit."""
    store = CacheStore(env, "store")
    try:
        main = _accounting_rollout(env, "01", "main")
        store.sync()
        env.append(main.name)
        statements: list[str] = []
        store.conn.set_trace_callback(statements.append)
        try:
            stats = store.sync()
        finally:
            store.conn.set_trace_callback(None)
        assert stats.files_processed == 1 and stats.full_walk_complete
        heads = [s.lstrip().upper() for s in statements
                 if not s.lstrip().startswith("--")]
        last_cursor = max(
            index for index, head in enumerate(heads)
            if head.startswith("INSERT") and "INTO CODEX_SESSION_FILES" in head)
        first_cursor = min(
            index for index, head in enumerate(heads)
            if head.startswith("INSERT") and "INTO CODEX_SESSION_FILES" in head)
        # A commit before the walk (the attribution rehydration's, when the
        # journal grew) is not the walk's.
        commits = [index for index, head in enumerate(heads)
                   if head.startswith("COMMIT") and index > first_cursor]
        assert len(commits) == 1 and commits[0] > last_cursor, commits
        finalization = [
            index for index, head in enumerate(heads)
            if "CODEX_FINALIZATION_STAGE" in head]
        assert finalization and last_cursor < min(finalization) < commits[0]
    finally:
        store.close()


# ── (l) one accounting transaction per unbudgeted sync (W11) ────────────────


def _commits(statements: list[str]) -> int:
    return sum(
        1 for s in statements
        if s.lstrip().upper().startswith(("COMMIT", "END"))
        and not s.lstrip().startswith("--"))


def _traced_sync(store: CacheStore, **kwargs):
    """One `sync_codex_cache` on the store's connection with every statement
    traced; returns the stats and the trace."""
    store.activate()
    store.conn.execute("DELETE FROM g10_incarnation_writes")
    store.conn.commit()
    statements: list[str] = []
    store.conn.set_trace_callback(statements.append)
    try:
        stats = store.env.cache.sync_codex_cache(store.conn, **kwargs)
    finally:
        store.conn.set_trace_callback(None)
    return stats, statements


def _per_file_control(env: Env, monkeypatch, name: str):
    """A store whose unbudgeted sync commits per file, as before W11: the
    pre-change path kept as the test seam `_CODEX_W11_ENABLED`."""
    store = CacheStore(env, name)
    real = store.sync

    def sync(**kwargs):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(env.cache, "_CODEX_W11_ENABLED", False,
                          raising=False)
            return real(**kwargs)

    store.sync = sync
    return store


def _ledgers(store: CacheStore) -> dict[str, list]:
    """The two change ledgers on their semantic columns (no sequence)."""
    out = {}
    for table in ("codex_accounting_change_log", "quota_window_change_log"):
        columns = _columns(store.conn, table, {"seq", "mutation_seq"})
        out[table] = sorted(store.conn.execute(
            f"SELECT {columns} FROM {table}").fetchall(), key=repr)
    return out


def _three_changed_files(env: Env) -> list[Rollout]:
    return [_accounting_rollout(env, f"l{index}", f"conv-l{index}")
            for index in range(3)]


def _settle(*stores: CacheStore) -> None:
    """Sync twice: the second sync replays the attribution decisions the
    first journaled (its pre-walk rehydration commit), so a measured sync
    afterwards is the steady state."""
    for store in stores:
        store.sync()
        store.sync()


def _append_turn_and_quota(env: Env, rollout: Rollout, used: float) -> None:
    with rollout.path.open("a", encoding="utf-8") as fh:
        fh.write(_lines(
            _turn(rollout.conversation, rollout.next_turn, rollout.clock)
            + [_quota_record(rollout.clock + 7, used)]))
    rollout.next_turn += 1
    rollout.clock += TURN_SPACING


def test_l_an_unbudgeted_sync_over_three_changed_files_commits_once(env):
    store = CacheStore(env, "store")
    try:
        rollouts = _three_changed_files(env)
        _settle(store)
        for rollout in rollouts:
            _append_turn_and_quota(env, rollout, 30.0)
        stats, statements = _traced_sync(store)
        assert stats.files_processed == 3 and stats.files_failed == 0
        assert _commits(statements) == 1, (
            f"{_commits(statements)} commits to cache.db for one tick")
        assert stats.full_walk_complete
    finally:
        store.close()


def test_l_the_hook_budgeted_path_still_commits_per_file(env):
    store = CacheStore(env, "store")
    try:
        rollouts = _three_changed_files(env)
        _settle(store)
        for rollout in rollouts:
            _append_turn_and_quota(env, rollout, 30.0)
        stats, statements = _traced_sync(store, budget_seconds=1e6)
        assert stats.files_processed == 3
        # One commit per file plus the finalization, exactly as before W11.
        assert _commits(statements) == 4, _commits(statements)
    finally:
        store.close()


def test_l_one_transaction_equals_the_per_file_control(env, monkeypatch):
    store = CacheStore(env, "store")
    control = _per_file_control(env, monkeypatch, "control")
    try:
        rollouts = _three_changed_files(env)
        for target in (store, control):
            target.sync()
        steps = (
            lambda: [_append_turn_and_quota(env, r, 40.0) for r in rollouts],
            lambda: rollouts.append(
                _accounting_rollout(env, "l9", "conv-l9")),
            lambda: [env.append(r.name) for r in rollouts[:2]],
        )
        for step in steps:
            step()
            mine = store.sync()
            theirs = control.sync()
            assert (mine.files_processed, mine.rows_changed,
                    mine.files_skipped_unchanged) == (
                theirs.files_processed, theirs.rows_changed,
                theirs.files_skipped_unchanged)
            assert_same(store.accounting(), control.accounting())
            assert_same(_ledgers(store), _ledgers(control))
    finally:
        store.close()
        control.close()


def _inject_second_file_failure(env: Env, patch, failing: str) -> None:
    """Fail the named file's apply after its every DML, on both attempts: the
    late failure G10 (l) injects (before its savepoint is released, or, per
    file, before its commit)."""
    current = {"path": None}
    real_write = env.cache._write_codex_file_batch
    real_bump = env.cache._bump_codex_physical_mutation_seq

    def write(conn, **kwargs):
        current["path"] = kwargs["path_str"]
        try:
            return real_write(conn, **kwargs)
        finally:
            current["path"] = None

    def bump(conn):
        if current["path"] == failing:
            raise sqlite3.OperationalError("g10 injected file failure")
        return real_bump(conn)

    patch.setattr(env.cache, "_write_codex_file_batch", write)
    patch.setattr(env.cache, "_bump_codex_physical_mutation_seq", bump)


def test_l_an_error_in_the_second_file_rolls_back_that_file_alone(
    env, monkeypatch,
):
    store = CacheStore(env, "store")
    control = _per_file_control(env, monkeypatch, "control")
    try:
        rollouts = _three_changed_files(env)
        _settle(store, control)
        for rollout in rollouts:
            _append_turn_and_quota(env, rollout, 50.0)
        before = store.file_row(rollouts[1])
        results = {}
        for name, target in (("store", store), ("control", control)):
            with pytest.MonkeyPatch.context() as patch:
                _inject_second_file_failure(env, patch, str(rollouts[1].path))
                if name == "control":
                    patch.setattr(env.cache, "_CODEX_W11_ENABLED", False,
                                  raising=False)
                target.activate()
                results[name] = env.cache.sync_codex_cache(target.conn)
        mine, theirs = results["store"], results["control"]
        assert mine.files_failed == 1 and mine.files_processed == 2, mine
        assert store.file_row(rollouts[1]) == before, (
            "the failed file's cursor moved")
        for rollout in (rollouts[0], rollouts[2]):
            assert store.file_row(rollout)[1] == rollout.path.stat().st_size
        assert not store.conn.in_transaction
        # Its resolver and statistics state as a per-file walk leaves them.
        for field in ("files_failed", "files_processed", "rows_changed",
                      "lines_seen", "lines_malformed", "token_events_skipped",
                      "skip_reasons"):
            assert getattr(mine, field) == getattr(theirs, field), field
        assert_same(store.accounting(), control.accounting())
        assert_same(_ledgers(store), _ledgers(control))

        # The next clean sync converges with the control's.
        store.sync()
        control.sync()
        assert_same(store.accounting(), control.accounting())
        assert_same(_ledgers(store), _ledgers(control))
    finally:
        store.close()
        control.close()


@pytest.mark.parametrize("failure", ["corruption", "non_database"])
def test_l_a_targeted_batch_that_raises_rolls_back_and_releases_its_flocks(
    env, failure,
):
    """#901 Amendment 19 PR-5. A targeted (``only_paths``) W11 batch that
    raises — SQLite corruption, which the batch re-raises untouched, or a
    non-database exception, which rolls back only the failing file — used to
    leave the batch's transaction open on the caller's connection: a targeted
    call owes no walk marker, so nothing in ``finally`` rolled back, and the
    writer flocks were released over an uncommitted write. The ``finally``
    now rolls back whatever is still open when it is not finalizing: no
    transaction remains, the flocks are free, and the committed state is the
    state before the call."""
    import _cctally_core as core
    import _lib_cache_writer_lock as writer_lock

    store = CacheStore(env, "store")
    try:
        rollouts = _three_changed_files(env)
        _settle(store)
        for rollout in rollouts:
            _append_turn_and_quota(env, rollout, 60.0)
        before_rows = [store.file_row(rollout) for rollout in rollouts]
        before = store.accounting()
        failing = str(rollouts[1].path)
        current = {"path": None}
        real_write = env.cache._write_codex_file_batch
        real_bump = env.cache._bump_codex_physical_mutation_seq
        applied = []

        def write(conn, **kwargs):
            current["path"] = kwargs["path_str"]
            try:
                return real_write(conn, **kwargs)
            finally:
                applied.append(kwargs["path_str"])
                current["path"] = None

        def bump(conn):
            if current["path"] == failing:
                if failure == "corruption":
                    raise sqlite3.DatabaseError(
                        "database disk image is malformed")
                raise RuntimeError("pr5 injected non-database failure")
            return real_bump(conn)

        expected = (sqlite3.DatabaseError if failure == "corruption"
                    else RuntimeError)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(env.cache, "_write_codex_file_batch", write)
            patch.setattr(env.cache, "_bump_codex_physical_mutation_seq", bump)
            store.activate()
            with pytest.raises(expected):
                env.cache.sync_codex_cache(
                    store.conn,
                    only_paths={str(rollout.path) for rollout in rollouts})
        assert applied[:1] == [str(rollouts[0].path)] and failing in applied, (
            "non-vacuity: a file applied in the batch before the failure",
            applied)
        assert not store.conn.in_transaction, "the batch was left open"
        held = writer_lock.acquire_cache_writer_flocks(
            core.CACHE_LOCK_PATH, core.CACHE_LOCK_CODEX_PATH)
        assert held, "the writer flocks are still held"
        writer_lock.release_cache_writer_flocks(held)
        assert [store.file_row(rollout) for rollout in rollouts] == before_rows
        assert_same(store.accounting(), before)
        # The next targeted call applies all three files cleanly.
        stats = store.sync(
            only_paths={str(rollout.path) for rollout in rollouts})
        assert stats.files_processed == 3
        assert all(store.file_row(rollout)[1] == rollout.path.stat().st_size
                   for rollout in rollouts)
    finally:
        store.close()


def _inject_whole_transaction_loss(env: Env, patch, failing: str, *,
                                   times: "int | None") -> dict:
    """G10 (n): lose the whole transaction while the named file is applied —
    a real ``ROLLBACK`` on the batch connection, then a ``DatabaseError`` —
    on its first ``times`` attempts (every attempt for None). Records, per
    loss, whether a transaction was open and how many files had been applied
    before it in that transaction."""
    seen = {"path": None, "applied": [], "losses": []}
    real_write = env.cache._write_codex_file_batch
    real_bump = env.cache._bump_codex_physical_mutation_seq

    def write(conn, **kwargs):
        if not conn.in_transaction:
            seen["applied"] = []
        seen["path"] = kwargs["path_str"]
        try:
            result = real_write(conn, **kwargs)
        finally:
            seen["path"] = None
        seen["applied"].append(kwargs["path_str"])
        return result

    def bump(conn):
        if seen["path"] == failing and (
                times is None or len(seen["losses"]) < times):
            seen["losses"].append(
                (conn.in_transaction, len(seen["applied"])))
            conn.execute("ROLLBACK")
            raise sqlite3.OperationalError(
                "g10 injected whole-transaction loss")
        return real_bump(conn)

    patch.setattr(env.cache, "_write_codex_file_batch", write)
    patch.setattr(env.cache, "_bump_codex_physical_mutation_seq", bump)
    return seen


def _journal(store: CacheStore) -> list:
    """The store's journal records on their stable fields: what replay sees,
    without the content id and with an attribution decision's wall-clock
    stamp dropped (a quota observation's stamp is its capture time)."""
    journal = store.data_dir / ".local" / "share" / "cctally" / "journal"
    out = []
    for segment in sorted(journal.glob("*.jsonl")):
        for line in segment.read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            record.pop("id", None)
            if record.get("t") == "op":
                record.pop("at", None)
            out.append(json.dumps(record, sort_keys=True))
    return sorted(out)


@pytest.mark.parametrize("repeat", ("re-preparation-succeeds",
                                    "same-file-fails-again"))
def test_n_a_whole_transaction_loss_in_a_later_file_converges_with_the_control(
    env, monkeypatch, repeat,
):
    store = CacheStore(env, "store")
    control = _per_file_control(env, monkeypatch, "control")
    try:
        rollouts = _three_changed_files(env)
        _settle(store, control)
        for rollout in rollouts:
            _append_turn_and_quota(env, rollout, 70.0)
        failing = str(rollouts[2].path)
        times = 1 if repeat == "re-preparation-succeeds" else None
        results, committed, losses = {}, {}, {}
        for name, target in (("store", store), ("control", control)):
            with pytest.MonkeyPatch.context() as patch:
                seen = _inject_whole_transaction_loss(
                    env, patch, failing, times=times)
                if name == "control":
                    patch.setattr(env.cache, "_CODEX_W11_ENABLED", False,
                                  raising=False)
                target.activate()
                committed[name] = []
                results[name] = env.cache.sync_codex_cache(
                    target.conn, _on_file_committed=committed[name].append)
                losses[name] = seen["losses"]
        mine, theirs = results["store"], results["control"]
        assert losses["store"] and losses["store"][0][0], (
            "the injected loss did not strike an open batch transaction")
        assert losses["store"][0][1] >= 1, (
            "the loss did not strike a later file of the batch",
            losses["store"])
        assert len(losses["store"]) == len(losses["control"]) == (
            1 if times == 1 else 2), losses
        assert not store.conn.in_transaction
        expected_failed = 0 if times == 1 else 1
        assert mine.files_failed == expected_failed, mine
        for field in ("files_failed", "files_processed", "rows_changed",
                      "files_reset_truncated", "lines_seen",
                      "lines_malformed", "token_events_skipped",
                      "skip_reasons"):
            assert getattr(mine, field) == getattr(theirs, field), field
        assert sorted(committed["store"]) == sorted(committed["control"]), (
            "post-commit callbacks differ", committed)
        assert len(committed["store"]) == len(set(committed["store"]))
        for rollout in rollouts:
            assert store.file_row(rollout) == control.file_row(rollout), (
                rollout.name)
        assert_same(store.accounting(), control.accounting())
        assert_same(_ledgers(store), _ledgers(control))
        assert any('"codex-quota"' in record for record in _journal(store)), (
            "non-vacuity: the batch must journal quota observations")
        assert _journal(store) == _journal(control), (
            "the journal appends differ from the per-file control's")

        # Converged: the next clean sync leaves both equal again.
        store.sync()
        control.sync()
        for rollout in rollouts:
            assert store.file_row(rollout) == control.file_row(rollout)
            assert store.file_row(rollout)[1] == rollout.path.stat().st_size
        assert_same(store.accounting(), control.accounting())
        assert_same(_ledgers(store), _ledgers(control))
        assert _journal(store) == _journal(control)
    finally:
        store.close()
        control.close()


def _inject_parse_health_transaction_loss(env: Env, patch) -> list:
    """901-QI-001: lose the finalization transaction inside the parse-health
    write — a real ``ROLLBACK`` on the sync connection, then a
    ``DatabaseError``, which `_update_parse_health_meta` swallows. Records,
    per loss, whether a transaction was open when it struck."""
    losses: list[bool] = []
    real_set = env.cache._set_cache_meta

    def set_meta(conn, key, value):
        if key == "parse_health_codex" and not losses:
            losses.append(conn.in_transaction)
            conn.execute("ROLLBACK")
            raise sqlite3.OperationalError(
                "g10 injected finalization transaction loss")
        return real_set(conn, key, value)

    patch.setattr(env.cache, "_set_cache_meta", set_meta)
    return losses


def _full_walk_marker(store: CacheStore) -> bool:
    import _lib_ingest_frontier as frontier

    return store.conn.execute(
        "SELECT 1 FROM cache_meta WHERE key=?",
        (frontier.CODEX_FULL_WALK_COMPLETE_KEY,)).fetchone() is not None


def test_n_a_finalization_transaction_loss_never_publishes_the_last_batch(
    env, monkeypatch,
):
    """901-QI-001. W11's last batch rides W8's finalization transaction.
    When SQLite ended that transaction inside the parse-health write (which
    swallows the error), the next stage opened a new one and the final
    commit published the batch's files — callbacks, statistics and a
    certified full walk — although their rows and cursors were rolled back.
    The lost files are now withdrawn as a crash before the commit would
    leave them, and the next sync ingests them: the end state then equals
    the per-file control's."""
    store = CacheStore(env, "store")
    control = _per_file_control(env, monkeypatch, "control")
    try:
        rollouts = _three_changed_files(env)
        _settle(store, control)
        for rollout in rollouts:
            _append_turn_and_quota(env, rollout, 80.0)
        before_rows = [store.file_row(rollout) for rollout in rollouts]
        before = store.accounting()
        assert _full_walk_marker(store) and _full_walk_marker(control)
        results, committed, losses = {}, {}, {}
        for name, target in (("store", store), ("control", control)):
            # The parse-health record is written when absent (first adoption).
            target.conn.execute(
                "DELETE FROM cache_meta WHERE key='parse_health_codex'")
            target.conn.commit()
            with pytest.MonkeyPatch.context() as patch:
                losses[name] = _inject_parse_health_transaction_loss(env, patch)
                if name == "control":
                    patch.setattr(env.cache, "_CODEX_W11_ENABLED", False,
                                  raising=False)
                target.activate()
                committed[name] = []
                results[name] = env.cache.sync_codex_cache(
                    target.conn, _on_file_committed=committed[name].append)
        mine, theirs = results["store"], results["control"]
        assert losses["store"] == [True] and losses["control"] == [True], (
            "the injected loss did not strike an open finalization", losses)
        assert theirs.files_processed == 3 and len(committed["control"]) == 3, (
            "non-vacuity: the per-file control committed every file")
        assert not store.conn.in_transaction
        # The rolled-back files are neither published nor counted.
        assert committed["store"] == [], (
            "rolled-back files were published", committed["store"])
        # Each withdrawn file reads as failed, so no caller takes the sync
        # for a clean one (901-QI-001).
        assert (mine.files_processed, mine.rows_changed,
                mine.files_failed) == (0, 0, 3), mine
        assert (mine.lines_seen, mine.lines_malformed,
                mine.token_events_skipped, mine.skip_reasons) == (
            0, 0, 0, {}), mine
        assert not mine.full_walk_complete, "a lost batch certified the walk"
        assert not _full_walk_marker(store), (
            "the completion marker survived a lost batch")
        assert [store.file_row(r) for r in rollouts] == before_rows, (
            "a rolled-back file's cursor moved")
        assert_same(store.accounting(), before)

        # The next clean sync ingests them: the end state is the control's.
        again = store.sync()
        assert again.files_processed == 3 and again.full_walk_complete
        control.sync()
        for rollout in rollouts:
            assert store.file_row(rollout) == control.file_row(rollout), (
                rollout.name)
            assert store.file_row(rollout)[1] == rollout.path.stat().st_size
        assert_same(store.accounting(), control.accounting())
        assert_same(_ledgers(store), _ledgers(control))
        assert _full_walk_marker(store) and _full_walk_marker(control)
    finally:
        store.close()
        control.close()


_ACCOUNT_A = "a" * 32
_ACCOUNT_B = "b" * 32
_DAY = 86_400
_MODERN_FULL = (TESTS_DIR / "fixtures" / "codex-parity" / "v1" / "rollouts"
                / "modern-full.jsonl")


def _identify_codex_roots(env: Env, patch, active: dict) -> None:
    """Every Codex root resolves to the identified account ``active["key"]``."""
    cache = env.cache
    patch.setattr(
        cache, "_resolve_codex_account_for_root",
        lambda _root: cache._CodexRootAccount(
            "identified", active["key"],
            {"account_key": active["key"], "natural_id": active["key"]}))


def _attribution(store: CacheStore) -> list[tuple]:
    return sorted(store.conn.execute(
        "SELECT source_path, line_offset, account_key "
        "FROM codex_session_entries").fetchall(), key=repr)


def test_n_a_finalization_loss_keeps_the_adoption_owed_by_committed_batches(
    env, monkeypatch,
):
    """901-QI-002. Withdrawing a lost last batch rolled back the whole
    finalization, including the window spend adoption owed by batches that
    had already committed. The next sync visits only the withdrawn tail, whose
    bounded adoption range no longer reaches that historic spend, so it stayed
    unattributed for good while later syncs certified completion. After the
    loss and a clean retry, attribution, costs and completion must equal the
    per-file control's."""
    store = CacheStore(env, "store")
    control = _per_file_control(env, monkeypatch, "control")
    try:
        # Historic spend ingested while the root had no identity: NULL.
        historic = env.rollout("q-historic", "conv-q-historic", 2, start=-_DAY)
        _settle(store, control)
        assert {row[2] for row in _attribution(store)
                if row[0] == str(historic.path)} == {None}
        monkeypatch.setattr(env.cache, "CODEX_ACCOUNTING_BATCH_FILES", 1)
        _identify_codex_roots(env, monkeypatch, {"key": _ACCOUNT_A})
        # A committed batch: identified weekly evidence whose nominal range,
        # [BASE - 2 d, BASE + 5 d), holds the historic spend.
        first = env.rollout("q-first", "conv-q-first", 1, start=0)
        with first.path.open("a", encoding="utf-8") as fh:
            fh.write(_lines([_quota_record(first.clock, 20.0)]))
        # The deferred tail, three weeks on: its own adoption range excludes
        # the historic spend.
        tail = env.rollout("q-tail", "conv-q-tail", 1, start=22 * _DAY)
        results = {}
        for name, target in (("store", store), ("control", control)):
            target.conn.execute(
                "DELETE FROM cache_meta WHERE key='parse_health_codex'")
            target.conn.commit()
            with pytest.MonkeyPatch.context() as patch:
                losses = _inject_parse_health_transaction_loss(env, patch)
                if name == "control":
                    patch.setattr(env.cache, "_CODEX_W11_ENABLED", False,
                                  raising=False)
                target.activate()
                results[name] = env.cache.sync_codex_cache(target.conn)
            assert losses == [True], (name, losses)
        # Non-vacuity: the first batch committed and the tail was withdrawn.
        assert store.file_row(first)[1] == first.path.stat().st_size
        assert store.file_row(tail) is None, "the tail was not withdrawn"
        assert {row[2] for row in _attribution(control)
                if row[0] == str(historic.path)} == {_ACCOUNT_A}, (
            "non-vacuity: the control adopts the historic spend")

        # A clean retry on both.
        store.sync()
        control.sync()
        assert _attribution(store) == _attribution(control), (
            "the adoption owed by a committed batch was lost")
        assert_same(store.accounting(), control.accounting())
        for rollout in (historic, first, tail):
            assert store.file_row(rollout) == control.file_row(rollout)
        assert _full_walk_marker(store) and _full_walk_marker(control)
        mine = results["store"]
        assert mine.files_failed == 1 and not mine.full_walk_complete, mine
    finally:
        store.close()
        control.close()


class _TailHandler:
    """The handler surface `_qualified_conversation_events` touches."""

    no_sync = False

    def __init__(self) -> None:
        self.responses: list = []
        self.errors: list[str] = []

    def send_response(self, code, message=None) -> None:
        self.responses.append(code)

    def send_header(self, *_args) -> None:
        pass

    def end_headers(self) -> None:
        pass

    def _respond_json(self, code, body) -> None:
        self.responses.append((code, body))

    def log_error(self, fmt, *args) -> None:
        self.errors.append(fmt % args)


def _account_tail_cycle(env: Env, key: str, account: str, path: str,
                        seen: dict, *, inject=None) -> dict:
    """One cycle of the real account-scoped Codex live tail: the dashboard's
    own `_ingest` under the watch kernel's acknowledgement rule. Records the
    ingest's stats, the transcript ingests it ran, the watch's new `seen` and
    whether it emitted."""
    import _lib_conversation_watch as watch

    conv = sys.modules["_cctally_dashboard_conversation"]
    real_transcript = conv.sync_codex_conversations
    out: dict = {"transcript": [], "losses": None}

    def transcript(writer, **kwargs):
        out["transcript"].append(sorted(kwargs.get("only_paths") or ()))
        return real_transcript(writer, **kwargs)

    def stream(handler, conn, **kwargs):
        def ingest(changed):
            out["stats"] = kwargs["ingest"](changed)
            return out["stats"]

        cached = kwargs["cached_sigs"]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(conv, "sync_codex_conversations", transcript)
            if inject is not None:
                out["losses"] = inject(patch)
            out["seen"], out["emitted"] = watch.watch_step(
                [path], dict(seen), ingest_fn=ingest,
                committed_sig_fn=lambda p: cached([p]).get(p))

    handler = _TailHandler()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(conv, "_run_conversation_events_stream", stream)
        conv._qualified_conversation_events(handler, key, account)
    assert handler.errors == [], handler.errors
    assert handler.responses == [200], handler.responses
    return out


def _tail_state(env: Env, name: str) -> dict:
    redirect_paths_without_conversation_retention(
        env.ns, env.mp, env.tmp / name)
    out = {}
    conn = env.ns["open_conversations_db"]()
    try:
        out["messages"] = sorted(conn.execute(
            "SELECT text, account_key FROM codex_conversation_messages"
        ).fetchall(), key=repr)
        out["events"] = sorted(conn.execute(
            "SELECT DISTINCT account_key FROM codex_conversation_events"
        ).fetchall(), key=repr)
    finally:
        conn.close()
    cache = env.ns["open_cache_db"]()
    try:
        out["entries"] = sorted(cache.execute(
            "SELECT line_offset, total_tokens, account_key "
            "FROM codex_session_entries").fetchall(), key=repr)
        out["files"] = cache.execute(
            "SELECT size_bytes, last_byte_offset FROM codex_session_files"
        ).fetchall()
    finally:
        cache.close()
    return out


def test_n_a_lost_targeted_accounting_batch_holds_the_account_switch_tail(
    env, monkeypatch,
):
    """901-QI-001 (targeted). The account-scoped Codex live tail advances the
    accounting cache first, so transcript ingest never stamps a new account's
    bytes from the previous account's range. When that targeted accounting
    batch lost its finalization transaction, the withdrawal still reported a
    clean ingest: the tail went on to ingest the transcript under the surviving
    A range, stamped B's messages as A, committed its cursor, and the watch
    acknowledged it, so no later accounting retry could repair them. The lost
    batch must read dirty, run no transcript ingest, leave the watch
    unacknowledged, and converge with the control on the retry."""
    active = {"key": _ACCOUNT_A}
    _identify_codex_roots(env, monkeypatch, active)
    rollout = env.sessions / "rollout-switch.jsonl"
    rollout.write_bytes(_MODERN_FULL.read_bytes())
    path = str(rollout)
    env.ns["DashboardHTTPHandler"]  # loads the dashboard siblings
    keys = {}
    for name in ("store", "control"):
        redirect_paths_without_conversation_retention(
            env.ns, env.mp, env.tmp / name)
        cache = env.ns["open_cache_db"]()
        try:
            env.cache.sync_codex_cache(cache)
            keys[name] = cache.execute(
                "SELECT conversation_key FROM codex_conversation_threads"
            ).fetchone()[0]
            cache.execute(
                "DELETE FROM cache_meta WHERE key='parse_health_codex'")
            cache.commit()
        finally:
            cache.close()
        conn = env.ns["open_conversations_db"]()
        try:
            env.cache.sync_codex_conversations(conn)
        finally:
            conn.close()
    assert keys["store"] == keys["control"]
    key = keys["store"]
    seen = {path: rollout.stat().st_size}

    # The account switches, and the rollout grows under B.
    active["key"] = _ACCOUNT_B
    with rollout.open("a", encoding="utf-8") as fh:
        fh.write(_lines([
            {"payload": {"images": [], "local_images": [],
                         "message": "bravo codex tail",
                         "text_elements": [{"text": "bravo codex tail"}],
                         "type": "user_message"},
             "timestamp": "2026-07-14T12:20:00Z", "type": "event_msg"},
            {"payload": {"info": {
                "last_token_usage": {
                    "cached_input_tokens": 0, "input_tokens": 500,
                    "output_tokens": 100, "reasoning_output_tokens": 0,
                    "total_tokens": 600},
                "model_context_window": 272000, "rate_limits": {},
                "total_token_usage": {"total_tokens": 2200}},
                "type": "token_count"},
             "timestamp": "2026-07-14T12:21:00Z", "type": "event_msg"},
        ]))

    redirect_paths_without_conversation_retention(
        env.ns, env.mp, env.tmp / "store")
    lost = _account_tail_cycle(
        env, key, _ACCOUNT_A, path, seen,
        inject=lambda patch: _inject_parse_health_transaction_loss(env, patch))
    assert lost["losses"] == [True], (
        "the injected loss did not strike the targeted accounting batch",
        lost["losses"])
    assert not lost["stats"].targeted_clean, (
        "a lost accounting batch read clean", lost["stats"])
    assert lost["transcript"] == [], (
        "transcript ingest ran after a lost accounting batch",
        lost["transcript"])
    assert (lost["seen"], lost["emitted"]) == (seen, False), (
        "the watch acknowledged a lost accounting batch", lost["seen"])

    redirect_paths_without_conversation_retention(
        env.ns, env.mp, env.tmp / "control")
    control = _account_tail_cycle(env, key, _ACCOUNT_A, path, seen)
    assert control["stats"].targeted_clean
    assert control["transcript"] == [[path]]
    assert control["seen"] == {path: rollout.stat().st_size}

    # The retry: the watch still owes the growth, and this time it is clean.
    redirect_paths_without_conversation_retention(
        env.ns, env.mp, env.tmp / "store")
    retry = _account_tail_cycle(env, key, _ACCOUNT_A, path, lost["seen"])
    assert retry["stats"].targeted_clean
    assert retry["transcript"] == [[path]]
    assert retry["seen"] == control["seen"]
    mine, theirs = _tail_state(env, "store"), _tail_state(env, "control")
    assert ("bravo codex tail", _ACCOUNT_B) in theirs["messages"], (
        "non-vacuity: the control stamps the B tail as B")
    assert mine == theirs


def _inject_planned_losses(env: Env, patch, plan: dict) -> dict:
    """Lose the whole transaction while each planned file is applied, on its
    first ``plan[path]`` attempts (every attempt for None); counts the losses
    per file."""
    seen: dict = {"path": None, "losses": {}}
    real_write = env.cache._write_codex_file_batch
    real_bump = env.cache._bump_codex_physical_mutation_seq

    def write(conn, **kwargs):
        seen["path"] = kwargs["path_str"]
        try:
            return real_write(conn, **kwargs)
        finally:
            seen["path"] = None

    def bump(conn):
        path = seen["path"]
        count = seen["losses"].get(path, 0)
        if path in plan and (plan[path] is None or count < plan[path]):
            seen["losses"][path] = count + 1
            conn.execute("ROLLBACK")
            raise sqlite3.OperationalError("g10 injected planned loss")
        return real_bump(conn)

    patch.setattr(env.cache, "_write_codex_file_batch", write)
    patch.setattr(env.cache, "_bump_codex_physical_mutation_seq", bump)
    return seen["losses"]


def test_l_an_eager_requeue_never_withdraws_a_tail_a_later_slice_committed(
    env, monkeypatch,
):
    """OV-2 (the `_CODEX_W11_EAGER_PREPARE_FOR_TESTS` seam). The last slice of
    a final flush defers its files to the finalization while an earlier slice
    requeued a file. The final flush of the re-prepared files then commits a
    non-last slice, which commits the deferred files too; when its last slice
    applies nothing, the finalization's savepoint check found no savepoint and
    withdrew files that had committed. Callbacks, statistics and the end state
    must equal the per-file control's."""
    store = CacheStore(env, "store")
    control = _per_file_control(env, monkeypatch, "control")
    try:
        rollouts = [_accounting_rollout(env, f"e{index}", f"conv-e{index}")
                    for index in range(4)]
        _settle(store, control)
        for rollout in rollouts:
            _append_turn_and_quota(env, rollout, 60.0)
        plan = {str(rollouts[1].path): 1, str(rollouts[2].path): None}
        results, committed, losses = {}, {}, {}
        for name, target in (("store", store), ("control", control)):
            with pytest.MonkeyPatch.context() as patch:
                losses[name] = _inject_planned_losses(env, patch, plan)
                if name == "store":
                    patch.setattr(
                        env.cache, "_CODEX_W11_EAGER_PREPARE_FOR_TESTS", True,
                        raising=False)
                    patch.setattr(env.cache, "CODEX_ACCOUNTING_BATCH_FILES", 1)
                else:
                    patch.setattr(env.cache, "_CODEX_W11_ENABLED", False,
                                  raising=False)
                target.activate()
                committed[name] = []
                results[name] = env.cache.sync_codex_cache(
                    target.conn, _on_file_committed=committed[name].append)
        mine, theirs = results["store"], results["control"]
        assert losses["store"] == losses["control"] == {
            str(rollouts[1].path): 1, str(rollouts[2].path): 2}, losses
        assert not store.conn.in_transaction
        assert sorted(committed["store"]) == sorted(committed["control"]), (
            "post-commit callbacks differ", committed)
        for field in ("files_failed", "files_processed", "rows_changed",
                      "lines_seen", "full_walk_complete"):
            assert getattr(mine, field) == getattr(theirs, field), (
                field, mine, theirs)
        for rollout in rollouts:
            assert store.file_row(rollout) == control.file_row(rollout), (
                rollout.name)
        assert_same(store.accounting(), control.accounting())

        store.sync()
        control.sync()
        for rollout in rollouts:
            assert store.file_row(rollout) == control.file_row(rollout)
        assert_same(store.accounting(), control.accounting())
        assert _full_walk_marker(store) and _full_walk_marker(control)
    finally:
        store.close()
        control.close()


_CACHE_CRASH_CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import pytest
from conftest import load_script, redirect_paths_without_conversation_retention
import pathlib
os.environ["CODEX_HOME"] = sys.argv[4]
os.environ["CCTALLY_TEST_W9_DISABLE"] = "1"
ns = load_script()
patch = pytest.MonkeyPatch()
redirect_paths_without_conversation_retention(ns, patch, pathlib.Path(sys.argv[3]))
import _cctally_cache as cache
cache._CODEX_BATCH_PRECOMMIT_HOOK = lambda: os._exit(19)
conn = ns["open_cache_db"]()
cache.sync_codex_cache(conn)
os._exit(0)
"""


def test_l_a_crash_before_the_commit_leaves_no_file_committed(env):
    store = CacheStore(env, "store")
    try:
        rollouts = _three_changed_files(env)
        _settle(store)
        for rollout in rollouts:
            _append_turn_and_quota(env, rollout, 60.0)
        before = store.accounting()
        before_files = {r.name: store.file_row(r) for r in rollouts}
    finally:
        store.close()

    child = subprocess.run(
        [sys.executable, "-c", _CACHE_CRASH_CHILD, str(TESTS_DIR),
         str(BIN_DIR), str(store.data_dir), str(env.provider)],
        capture_output=True, text=True, timeout=60,
    )
    assert child.returncode == 19, child.stderr[-2000:]

    store = CacheStore(env, "store")
    try:
        assert {r.name: store.file_row(r) for r in rollouts} == before_files
        assert_same(store.accounting(), before)
        retry = store.sync()
        assert retry.files_processed == 3
        reference = CacheStore(env, "reference")
        try:
            reference.sync()
            # The crashed call's journal appends replay idempotently.
            assert_same(store.accounting(), reference.accounting())
        finally:
            reference.close()
    finally:
        store.close()


def _probe_parsing_outside_transactions(env: Env, monkeypatch,
                                        conn: sqlite3.Connection) -> list:
    """Record whether a write transaction was open at every parsed record."""
    seen: list[bool] = []
    real = env.cache._iter_codex_fused_records_with_offsets

    def probe(*args, **kwargs):
        for emission in real(*args, **kwargs):
            seen.append(conn.in_transaction)
            yield emission

    monkeypatch.setattr(env.cache, "_iter_codex_fused_records_with_offsets",
                        probe)
    return seen


def test_l_no_write_transaction_is_open_while_a_file_is_parsed(
    env, monkeypatch,
):
    store = CacheStore(env, "store")
    try:
        rollouts = _three_changed_files(env)
        store.sync()
        for rollout in rollouts:
            _append_turn_and_quota(env, rollout, 70.0)
        seen = _probe_parsing_outside_transactions(env, monkeypatch, store.conn)
        store.sync()
        assert seen and not any(seen), "a record was parsed inside a transaction"
    finally:
        store.close()


#: Bytes of padding that make one appended turn carry about 1.5 MiB.
_MIB = 1024 * 1024


def _pad_turn(env: Env, rollout: Rollout, nbytes: int) -> int:
    """Append one turn whose prompt carries ``nbytes`` of padding."""
    text = _lines(_turn(rollout.conversation, rollout.next_turn,
                        rollout.clock, pad="p" * nbytes))
    with rollout.path.open("a", encoding="utf-8") as fh:
        fh.write(text)
    rollout.next_turn += 1
    rollout.clock += TURN_SPACING
    return len(text.encode("utf-8"))


class W11Trace:
    """The preparation trace W11 emits: each file's preparation start and end,
    each batch's commit, and the release of its prepared records."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def __call__(self, *event) -> None:
        self.events.append(event)

    def batches(self) -> list[list[str]]:
        return [list(event[1]) for event in self.events if event[0] == "commit"]

    def violations(self, *, limit_files: int, limit_bytes: int) -> list[str]:
        """Every breach of 901-SR-035's observed preparation bound."""
        problems: list[str] = []
        held: dict[str, int] = {}
        committed_since_release = True
        pending_release = False
        for event in self.events:
            kind = event[0]
            if kind == "prepare_start":
                if pending_release:
                    problems.append(
                        f"{event[1]} prepared before the previous batch's "
                        "records were released")
                held[event[1]] = int(event[2])
                if len(held) > 1 and sum(held.values()) > limit_bytes:
                    problems.append(
                        f"held {sum(held.values())} bytes over {len(held)} "
                        "files, above one batch")
                if len(held) > limit_files:
                    problems.append(f"held {len(held)} files, above one batch")
            elif kind == "prepare_drop":
                held.pop(event[1], None)
            elif kind == "commit":
                for path in event[1]:
                    if path not in held:
                        problems.append(f"{path} committed but never prepared")
                pending_release = True
            elif kind == "release":
                for path in event[1]:
                    held.pop(path, None)
                pending_release = False
        if held:
            problems.append(f"never released: {sorted(held)}")
        return problems


def _bound_case(env: Env, monkeypatch, kind: str):
    """The three bound cases' corpora, synced once and then changed."""
    if kind == "files":
        rollouts = [env.rollout(f"b{index:03d}", f"conv-b{index:03d}", 1)
                    for index in range(65)]
    elif kind == "bytes":
        rollouts = [env.rollout(f"y{index}", f"conv-y{index}", 1)
                    for index in range(3)]
    else:
        rollouts = [env.rollout(f"o{index}", f"conv-o{index}", 1)
                    for index in range(3)]
    return rollouts


def _change_bound_case(env: Env, rollouts: list[Rollout], kind: str) -> None:
    if kind == "files":
        for rollout in rollouts:
            env.append(rollout.name)
    elif kind == "bytes":
        for rollout in rollouts:
            _pad_turn(env, rollout, int(1.5 * _MIB))
    else:
        _pad_turn(env, rollouts[0], 1024)
        _pad_turn(env, rollouts[1], 5 * _MIB)
        _pad_turn(env, rollouts[2], 1024)


@pytest.mark.parametrize("kind, batches", [
    ("files", [64, 1]), ("bytes", [2, 1]), ("oversized", [1, 1, 1]),
])
def test_l_a_large_catch_up_commits_in_bounded_batches(
    env, monkeypatch, kind, batches,
):
    """64 files commit together and a 65th starts a second batch; new input
    crossing 4 MiB splits; a file whose own new input exceeds 4 MiB is applied
    alone. Every batch is prepared with no write transaction open, and the
    result equals the per-file control's."""
    store = CacheStore(env, "store")
    control = _per_file_control(env, monkeypatch, "control")
    try:
        rollouts = _bound_case(env, monkeypatch, kind)
        _settle(store, control)
        _change_bound_case(env, rollouts, kind)
        trace = W11Trace()
        monkeypatch.setattr(env.cache, "_CODEX_W11_TRACE", trace, raising=False)
        seen = _probe_parsing_outside_transactions(env, monkeypatch, store.conn)
        stats, statements = _traced_sync(store)
        monkeypatch.setattr(env.cache, "_CODEX_W11_TRACE", None, raising=False)
        assert stats.files_processed == len(rollouts) and stats.files_failed == 0
        assert [len(batch) for batch in trace.batches()] == batches, (
            trace.batches())
        assert _commits(statements) == len(batches)
        assert seen and not any(seen)
        assert trace.violations(limit_files=64, limit_bytes=4 * _MIB) == []
        control.sync()
        assert_same(store.accounting(), control.accounting())
        assert_same(_ledgers(store), _ledgers(control))
    finally:
        store.close()
        control.close()


@pytest.mark.parametrize("kind", ["files", "bytes"])
def test_l_an_eager_preparation_fails_the_observed_bound(
    env, monkeypatch, kind,
):
    """The oracle is not vacuous: preparing every file first and committing
    bounded slices afterwards (kept as a test seam) breaks the trace bound."""
    store = CacheStore(env, "store")
    try:
        rollouts = _bound_case(env, monkeypatch, kind)
        store.sync()
        _change_bound_case(env, rollouts, kind)
        trace = W11Trace()
        monkeypatch.setattr(env.cache, "_CODEX_W11_TRACE", trace, raising=False)
        monkeypatch.setattr(env.cache, "_CODEX_W11_EAGER_PREPARE_FOR_TESTS",
                            True, raising=False)
        stats = store.sync()
        assert stats.files_processed == len(rollouts)
        assert trace.events, "the seam emitted no trace"
        assert trace.violations(limit_files=64, limit_bytes=4 * _MIB), (
            "an eager preparation passed the bound")
    finally:
        store.close()


def _wal_frames_for_page(db: pathlib.Path, pgno: int, start_frame: int) -> int:
    """WAL frames after ``start_frame`` whose page is ``pgno``."""
    data = pathlib.Path(f"{db}-wal").read_bytes()
    page_size = struct.unpack(">I", data[8:12])[0]
    frame = 32
    count = 0
    index = 0
    while frame + 24 <= len(data):
        if index >= start_frame and struct.unpack(
                ">I", data[frame:frame + 4])[0] == pgno:
            count += 1
        frame += 24 + page_size
        index += 1
    return count


def test_l_replaying_an_unchanged_quota_journal_leaves_sqlite_sequence(env):
    store = CacheStore(env, "store")
    try:
        _three_changed_files(env)
        store.sync()
        import _cctally_journal as journal
        import _lib_journal

        # Decoded exactly as the ingest cycle's step 2 hands them to its cache
        # leg: `(record, segment, offset)` in canonical order.
        records = []
        for seg, off, raw in journal._read_range(
                None, journal.journal_high_water()):
            record = _lib_journal.decode_line(raw)
            if record is not None and journal._is_codex_quota_obs(record):
                records.append((record, seg, off))
        assert records, "the walk journaled its quota observations"
        db = store.data_dir / ".local" / "share" / "cctally" / "cache.db"
        sequence_page = store.conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name='sqlite_sequence'"
        ).fetchone()[0]
        seq_before = store.conn.execute(
            "SELECT name, seq FROM sqlite_sequence ORDER BY name").fetchall()
        pinned = Pinned(db)
        try:
            start = wal_frames(db)
            assert journal._cache_applier(records) is None
            frames = _wal_frames_for_page(db, sequence_page, start)
        finally:
            pinned.close()
        assert frames == 0, (
            f"an unchanged replay wrote sqlite_sequence ({frames} frame(s))")
        assert store.conn.execute(
            "SELECT name, seq FROM sqlite_sequence ORDER BY name").fetchall() \
            == seq_before
    finally:
        store.close()


# ── (g) writer-owned checkpoints ─────────────────────────────────────────────


MIB = 1024 * 1024


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _wal_module():
    import importlib

    return importlib.import_module("_lib_wal_checkpoint")


@pytest.fixture
def w9(monkeypatch):
    """The W9 module with a fresh per-process registry and an injected clock."""
    monkeypatch.delenv("CCTALLY_TEST_W9_DISABLE", raising=False)
    wal = _wal_module()
    clock = FakeClock()
    monkeypatch.setattr(wal, "CLOCK", clock)
    wal.reset_policies()
    yield wal, clock
    wal.reset_policies()


def _wal_db(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS blobs(id INTEGER PRIMARY KEY, b BLOB)")
    conn.commit()
    return conn


def _write_pages(conn: sqlite3.Connection, nbytes: int) -> None:
    conn.execute("INSERT INTO blobs(b) VALUES (randomblob(?))", (int(nbytes),))
    conn.commit()


def _main_file_digest(db_path: pathlib.Path) -> str:
    """The main database file's bytes; a checkpoint is what changes them."""
    return hashlib.sha256(pathlib.Path(db_path).read_bytes()).hexdigest()


def _wal_salt(db_path: pathlib.Path) -> "bytes | None":
    """The WAL header's two salts: a new value is a new WAL generation."""
    try:
        header = pathlib.Path(f"{db_path}-wal").read_bytes()[:32]
    except OSError:
        return None
    return header[16:24] if len(header) == 32 else None


def _shm_backfill(db_path: pathlib.Path) -> tuple[int, int]:
    """(mxFrame, nBackfill) from the wal-index, as the reclaim planner reads it."""
    sys.path.insert(0, str(BIN_DIR))
    import _lib_reclaim_planner as planner

    raw = pathlib.Path(f"{db_path}-shm").read_bytes()
    header = planner.parse_wal_index_header(raw[:96])
    order = "<" if sys.byteorder == "little" else ">"
    n_backfill = struct.unpack(order + "I", raw[96:100])[0]
    return header.max_frame, n_backfill


def test_g_the_frame_source_counts_wal_frames_on_this_sqlite(w9, tmp_path):
    wal, _clock = w9
    db = tmp_path / "frames.db"
    conn = _wal_db(db)
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    page = int(conn.execute("PRAGMA page_size").fetchone()[0])
    try:
        assert wal.take_frames(conn) is not None, "the runtime exposes the count"
        for pages in (1, 7, 300):
            before = wal_frames(db)
            _write_pages(conn, pages * page)
            counted = wal.take_frames(conn)
            assert counted == wal_frames(db) - before > pages, (pages, counted)
        # A checkpointed, restarted WAL reuses its file: the length stays while
        # frames are appended, and the counter still counts every one of them.
        assert wal.checkpoint_complete(
            conn.execute("PRAGMA wal_checkpoint(RESTART)").fetchone())
        length = pathlib.Path(f"{db}-wal").stat().st_size
        _write_pages(conn, 40 * page)
        counted = wal.take_frames(conn)
        assert pathlib.Path(f"{db}-wal").stat().st_size == length
        max_frame, _backfill = _shm_backfill(db)
        assert counted == max_frame > 40
        assert wal.take_frames(conn) == 0, "reading consumes the count"
    finally:
        conn.close()


def _synced_writers(env: Env, tmp_path: pathlib.Path):
    share = _no_op_fixture(env, tmp_path)
    cache_conn = env.ns["open_cache_db"]()
    env.cache.sync_cache(cache_conn)
    env.cache.sync_codex_cache(cache_conn)
    conversations = env.ns["open_conversations_db"]()
    env.cache.sync_claude_conversations(conversations)
    env.cache.sync_codex_conversations(conversations)
    return share, cache_conn, conversations


def _autocheckpoint(conn) -> int:
    return int(conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0])


def test_g_sync_writers_own_their_checkpoints_and_other_connections_do_not(
    env, w9, tmp_path, monkeypatch,
):
    monkeypatch.delenv("CCTALLY_TEST_W9_DISABLE", raising=False)
    share, cache_conn, conversations = _synced_writers(env, tmp_path)
    others = []
    try:
        assert _autocheckpoint(cache_conn) == 0
        assert _autocheckpoint(conversations) == 0
        plain_cache = env.ns["open_cache_db"]()
        plain_conversations = env.ns["open_conversations_db"]()
        reader = env.cache.open_conversations_db_readonly()
        others += [plain_cache, plain_conversations, reader]
        assert [_autocheckpoint(c) for c in others] == [1000, 1000, 1000]
        retention = sys.modules.get("_lib_conversation_retention") or (
            __import__("_lib_conversation_retention"))
        reclaim = retention.open_reclaim_connection(share / "conversations.db")
        deletion = retention.open_deletion_connection(share / "conversations.db")
        others += [reclaim, deletion]
        assert _autocheckpoint(reclaim) == 0, "reclaim keeps its own setting"
        assert _autocheckpoint(deletion) == 1000, "deletion keeps the default"
    finally:
        for conn in others + [cache_conn, conversations]:
            conn.close()


def test_g_without_a_readable_frame_count_the_writer_keeps_the_default(
    env, w9, tmp_path, monkeypatch,
):
    wal, _clock = w9
    monkeypatch.delenv("CCTALLY_TEST_W9_DISABLE", raising=False)
    monkeypatch.setattr(wal, "take_frames", lambda conn, reset=True: None)
    _share, cache_conn, conversations = _synced_writers(env, tmp_path)
    try:
        assert _autocheckpoint(cache_conn) == 1000
        assert _autocheckpoint(conversations) == 1000
    finally:
        cache_conn.close()
        conversations.close()


def test_g_the_test_seam_leaves_every_writer_at_the_default(
    env, w9, tmp_path, monkeypatch,
):
    monkeypatch.setenv("CCTALLY_TEST_W9_DISABLE", "1")
    _share, cache_conn, conversations = _synced_writers(env, tmp_path)
    try:
        assert _autocheckpoint(cache_conn) == 1000
        assert _autocheckpoint(conversations) == 1000
    finally:
        cache_conn.close()
        conversations.close()


def test_g_every_sync_ends_at_a_boundary(env, w9, tmp_path, monkeypatch):
    wal, _clock = w9
    monkeypatch.delenv("CCTALLY_TEST_W9_DISABLE", raising=False)
    calls: list[str] = []
    real = wal.at_boundary

    def spy(conn, **kwargs):
        calls.append(pathlib.Path(conn.execute(
            "PRAGMA database_list").fetchone()[2]).name)
        return real(conn, **kwargs)

    monkeypatch.setattr(wal, "at_boundary", spy)
    _share, cache_conn, conversations = _synced_writers(env, tmp_path)
    try:
        # sync_cache, sync_codex_cache; the Claude pass; the Codex pass and its
        # stale-source prune.
        assert calls.count("cache.db") >= 2
        assert calls.count("conversations.db") >= 3
    finally:
        cache_conn.close()
        conversations.close()


def _prefilled_reset_wal(wal, db: pathlib.Path, nbytes: int) -> int:
    """A WAL grown to ``nbytes`` and then checkpointed and restarted: its file
    keeps that length while the next writer reuses it from the first frame."""
    filler = _wal_db(db)
    filler.execute("PRAGMA wal_autocheckpoint = 0")
    _write_pages(filler, nbytes)
    assert wal.checkpoint_complete(
        filler.execute("PRAGMA wal_checkpoint(RESTART)").fetchone())
    length = pathlib.Path(f"{db}-wal").stat().st_size
    filler.close()  # not the last connection: the caller already holds one
    return length


def test_g_the_size_trigger_checkpoints_at_16_mib_on_a_reused_wal(w9, tmp_path):
    wal, _clock = w9
    db = tmp_path / "size.db"
    writer = _wal_db(db)
    try:
        length = _prefilled_reset_wal(wal, db, 48 * MIB)
        assert wal.arm(writer)
        policy = wal.policy_for(writer)
        attempts_at: list[int] = []
        appended = 0
        real = wal.run_passive_checkpoint

        def spy(conn):
            attempts_at.append(appended)
            return real(conn)

        wal.run_passive_checkpoint = spy
        try:
            while appended <= 34 * MIB:
                _write_pages(writer, MIB)
                appended += wal.take_frames(writer, reset=False) * policy.frame_bytes
                wal.after_commit(writer)
                assert pathlib.Path(f"{db}-wal").stat().st_size == length
        finally:
            wal.run_passive_checkpoint = real
        # No checkpoint below the trigger; one at each 16 MiB of new frames.
        assert len(attempts_at) == 2, attempts_at
        assert 16 * MIB <= attempts_at[0] < 16 * MIB + 2 * MIB
        assert 32 * MIB <= attempts_at[1] < 32 * MIB + 2 * MIB
        assert policy.completions == 2
    finally:
        writer.close()


def test_g_no_checkpoint_at_a_commit_below_the_size_trigger(w9, tmp_path):
    wal, _clock = w9
    db = tmp_path / "small.db"
    writer = _wal_db(db)
    try:
        assert wal.arm(writer)
        for _ in range(8):
            _write_pages(writer, MIB)  # 8 MiB: past SQLite's 1,000-page default
            wal.after_commit(writer)
        policy = wal.policy_for(writer)
        assert policy.attempts == 0 and policy.pending
        max_frame, n_backfill = _shm_backfill(db)
        assert max_frame > 1000 and n_backfill == 0, "nothing was checkpointed"
    finally:
        writer.close()


def test_g_a_boundary_checkpoints_after_60_seconds_with_the_flag_set(w9, tmp_path):
    wal, clock = w9
    db = tmp_path / "timer.db"
    writer = _wal_db(db)
    try:
        assert wal.arm(writer)
        _write_pages(writer, 64 * 1024)
        wal.after_commit(writer)
        policy = wal.policy_for(writer)
        clock.advance(59)
        wal.at_boundary(writer)
        assert policy.attempts == 0
        clock.advance(1)
        wal.at_boundary(writer)
        assert policy.attempts == 1 and not policy.pending
        max_frame, n_backfill = _shm_backfill(db)
        assert max_frame == n_backfill > 0
        # Q14: the timer no longer waits for this process's own flag, so a
        # boundary 60 s after the last attempt attempts again; with every
        # frame already copied the attempt writes nothing.
        main = _main_file_digest(db)
        clock.advance(300)
        wal.at_boundary(writer)
        assert policy.attempts == 2 and not policy.pending
        assert _main_file_digest(db) == main, "nothing to copy, nothing written"
    finally:
        writer.close()


def test_g_a_boundary_60_seconds_after_the_last_attempt_copies_foreign_frames(
    w9, tmp_path,
):
    """Q14: another process's frames (a hook's, cache-sync's) are copied at
    this process's next due boundary although it committed nothing itself."""
    wal, clock = w9
    db = tmp_path / "foreign.db"
    writer = _wal_db(db)
    foreign = _wal_db(db)  # stands for another process's sync writer
    foreign.execute("PRAGMA wal_autocheckpoint = 0")
    try:
        assert wal.arm(writer)
        policy = wal.policy_for(writer)
        _write_pages(foreign, 256 * 1024)
        clock.advance(59)
        wal.at_boundary(writer)
        assert policy.attempts == 0, "the timer counts from the arm"
        max_frame, n_backfill = _shm_backfill(db)
        assert max_frame > n_backfill, "the foreign frames are retained"
        clock.advance(1)
        wal.at_boundary(writer)
        assert policy.attempts == 1 and not policy.pending, policy.last_result
        max_frame, n_backfill = _shm_backfill(db)
        assert max_frame == n_backfill > 0, "the boundary copied them"
    finally:
        foreign.close()
        writer.close()


def test_g_the_timer_thread_copies_retained_frames_without_a_boundary(
    w9, tmp_path, monkeypatch,
):
    """Q14 (901-SR-033): with the keeper open, the policy module's timer
    thread copies frames no boundary reaches, whoever wrote them."""
    wal, clock = w9
    monkeypatch.setattr(wal, "TIMER_INTERVAL_SECONDS", 0.02, raising=False)
    db = tmp_path / "timer-thread.db"
    writer = _wal_db(db)
    assert wal.arm(writer)
    writer.close()  # the sync connection is gone; nothing reaches a boundary
    foreign = _wal_db(db)
    foreign.execute("PRAGMA wal_autocheckpoint = 0")
    try:
        _write_pages(foreign, 256 * 1024)
        max_frame, n_backfill = _shm_backfill(db)
        assert max_frame > n_backfill
        clock.advance(60)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            max_frame, n_backfill = _shm_backfill(db)
            if max_frame == n_backfill:
                break
            time.sleep(0.02)
        assert max_frame == n_backfill > 0, (
            "no timer thread copied the retained frames")
    finally:
        foreign.close()


def test_g_a_pinned_reader_keeps_the_flag_and_an_idle_boundary_drains_later(
    w9, tmp_path,
):
    wal, clock = w9
    db = tmp_path / "pinned.db"
    writer = _wal_db(db)
    reader = sqlite3.connect(db)
    try:
        assert wal.arm(writer)
        _write_pages(writer, 64 * 1024)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM blobs").fetchone()
        _write_pages(writer, 256 * 1024)  # frames the reader's snapshot predates
        wal.after_commit(writer)
        policy = wal.policy_for(writer)
        clock.advance(60)
        wal.at_boundary(writer)
        assert policy.attempts == 1 and policy.pending, policy.last_result
        busy, log, checkpointed = policy.last_result
        assert busy == 0 and 0 <= checkpointed < log
        clock.advance(30)
        wal.at_boundary(writer)
        assert policy.attempts == 1, "no retry sooner than 60 s"
        reader.rollback()
        clock.advance(30)
        wal.at_boundary(writer)  # idle: no commit since the incomplete attempt
        assert policy.attempts == 2 and not policy.pending
        max_frame, n_backfill = _shm_backfill(db)
        assert max_frame == n_backfill > 0, "drained with both connections open"
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize("failure", ("busy", "unavailable-counts", "busy-row"))
def test_g_a_failed_attempt_keeps_the_flag_and_retries_60_seconds_later(
    w9, tmp_path, failure,
):
    wal, clock = w9
    db = tmp_path / "failed.db"
    writer = _wal_db(db)
    try:
        assert wal.arm(writer)
        _write_pages(writer, 64 * 1024)
        wal.after_commit(writer)
        policy = wal.policy_for(writer)
        real = wal.run_passive_checkpoint

        def failing(conn):
            if failure == "busy":
                raise sqlite3.OperationalError("database is locked")
            return (0, -1, -1) if failure == "unavailable-counts" else (1, -1, -1)

        wal.run_passive_checkpoint = failing
        try:
            clock.advance(60)
            wal.at_boundary(writer)
            assert policy.attempts == 1 and policy.pending
            clock.advance(59)
            wal.at_boundary(writer)
            assert policy.attempts == 1
        finally:
            wal.run_passive_checkpoint = real
        clock.advance(1)
        wal.at_boundary(writer)  # idle boundary 60 s after the failed attempt
        assert policy.attempts == 2 and not policy.pending
    finally:
        writer.close()


@pytest.mark.parametrize("failure", ("pinned-reader", "busy"))
def test_g_the_size_trigger_does_not_retry_an_unsuccessful_attempt_within_60_s(
    w9, tmp_path, failure,
):
    """901-RW-002. An incomplete or failed PASSIVE reset the frames counted
    since the attempt, so the next 16 MiB of frames size-triggered another
    attempt at once, though W9 says nothing retries sooner than 60 s."""
    wal, clock = w9
    db = tmp_path / "size-retry.db"
    writer = _wal_db(db)
    reader = sqlite3.connect(db)
    real = wal.run_passive_checkpoint
    try:
        assert wal.arm(writer)
        policy = wal.policy_for(writer)
        _write_pages(writer, 64 * 1024)
        wal.after_commit(writer)
        if failure == "pinned-reader":
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM blobs").fetchone()
        else:
            def failing(conn):
                raise sqlite3.OperationalError("database is locked")

            wal.run_passive_checkpoint = failing
        _write_pages(writer, 17 * MIB)
        wal.after_commit(writer)
        assert policy.attempts == 1 and policy.pending, policy.last_result
        clock.advance(30)
        _write_pages(writer, 17 * MIB)
        wal.after_commit(writer)
        assert policy.attempts == 1, (
            "a threshold-sized append retried the unsuccessful attempt "
            "sooner than 60 s")
        clock.advance(29)
        _write_pages(writer, 64 * 1024)
        wal.after_commit(writer)
        assert policy.attempts == 1, "no retry sooner than 60 s"
        if failure == "pinned-reader":
            reader.rollback()
        else:
            wal.run_passive_checkpoint = real
        clock.advance(1)
        _write_pages(writer, 64 * 1024)
        wal.after_commit(writer)
        assert policy.attempts == 2 and not policy.pending, policy.last_result
        # A completed attempt keeps today's size trigger: the next 16 MiB
        # attempts at once.
        _write_pages(writer, 17 * MIB)
        wal.after_commit(writer)
        assert policy.attempts == 3, "the size trigger stopped after a success"
    finally:
        wal.run_passive_checkpoint = real
        reader.close()
        writer.close()


def test_g_two_writer_processes_do_not_both_checkpoint_at_every_boundary(
    w9, tmp_path,
):
    wal, clock = w9
    db = tmp_path / "two.db"
    first, second = _wal_db(db), _wal_db(db)
    try:
        assert wal.arm(first) and wal.arm(second)
        # One policy per process: two independent instances over one file.
        policies = [wal.WalCheckpointPolicy("a"), wal.WalCheckpointPolicy("b")]
        boundaries = 0
        for step in range(25):  # a boundary every 5 s for two minutes
            for conn, policy in zip((first, second), policies):
                _write_pages(conn, 16 * 1024)
                wal.at_boundary(conn, policy=policy)
                boundaries += 1
            clock.advance(5)
        assert boundaries == 50
        assert [p.attempts for p in policies] == [2, 2]
    finally:
        first.close()
        second.close()


def test_g_the_last_connection_to_close_drains_the_wal(w9, tmp_path):
    """Q14: with the keeper open a sync connection's close is not the store's
    last close, so it neither checkpoints nor resets the WAL; the process's
    final keeper close, when it is the last, drains it."""
    wal, _clock = w9
    db = tmp_path / "close.db"
    wal_path = pathlib.Path(f"{db}-wal")
    writer = _wal_db(db)
    assert wal.arm(writer)
    _write_pages(writer, MIB)
    wal.after_commit(writer)
    assert wal_path.stat().st_size > MIB
    salt = _wal_salt(db)
    main = _main_file_digest(db)
    writer.close()
    assert wal_path.stat().st_size > MIB and _wal_salt(db) == salt, (
        "the sync connection's close reset the WAL")
    max_frame, n_backfill = _shm_backfill(db)
    assert max_frame > n_backfill and _main_file_digest(db) == main, (
        "the sync connection's close checkpointed")
    wal.finalize()
    assert not wal_path.exists() or wal_path.stat().st_size == 0
    check = sqlite3.connect(db)
    try:
        assert check.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
    finally:
        check.close()


def test_g_the_shrink_still_truncates_and_a_failed_shrink_waits_60_seconds(
    env, w9, tmp_path, monkeypatch,
):
    wal, clock = w9
    monkeypatch.setenv("CCTALLY_TEST_CACHE_WAL_TRIGGER_BYTES", "0")
    db = tmp_path / "shrink.db"
    writer = _wal_db(db)
    reader = sqlite3.connect(db)
    wal_path = pathlib.Path(f"{db}-wal")
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        _write_pages(writer, MIB)
        env.cache._maybe_truncate_wal(writer, db)
        assert wal_path.stat().st_size == 0, "the shrink truncates"

        _write_pages(writer, MIB)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM blobs").fetchone()
        _write_pages(writer, MIB)
        env.cache._maybe_truncate_wal(writer, db)  # the reader blocks it
        assert wal_path.stat().st_size > 0
        reader.rollback()
        clock.advance(30)
        env.cache._maybe_truncate_wal(writer, db)
        assert wal_path.stat().st_size > 0, "a failed shrink waits 60 s"
        clock.advance(30)
        env.cache._maybe_truncate_wal(writer, db)
        assert wal_path.stat().st_size == 0
    finally:
        reader.close()
        writer.close()
