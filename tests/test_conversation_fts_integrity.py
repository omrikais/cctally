"""#1094: conversation FTS postings written by another SQLite tokenizer.

Every test here runs in-process against a store built by the product's own
schema apply in a temporary data dir. Build keys are passed explicitly or
stubbed where a test needs two builds in one process; everything else is the
real kernel (`bin/_lib_conversation_fts_integrity.py`). Spec:
`docs/superpowers/specs/2026-11-12-1094-fts-tokenizer-integrity.md` section 6.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402
from _fts5_gate import requires_fts5  # noqa: E402

import _cctally_core  # noqa: E402

KEY_A = "a" * 64
KEY_B = "b" * 64
BUILD_PREFIX = "conversation_fts_tokenizer_build:"
INVALIDATED = "conversation_fts_tokenizer_invalidated"


def _kernel():
    import _lib_conversation_fts_integrity as fts
    return fts


def _store(tmp_path, monkeypatch):
    """A head-schema conversations store in a temp data dir."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_conversations_db"](attach_cache=False)
    conn.close()
    return ns, _kernel(), Path(_cctally_core.CONVERSATIONS_DB_PATH)


def _raw(path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=5.0)


_OFFSET = [0]


def _insert_message(conn, text="hello world", *, session_id="s1",
                    search_tool="", search_thinking=""):
    _OFFSET[0] += 1
    cur = conn.execute(
        "INSERT INTO conversation_messages (session_id, uuid, source_path, "
        "byte_offset, timestamp_utc, entry_type, text, search_tool, "
        "search_thinking) VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, f"u-{_OFFSET[0]}", "seed.jsonl", _OFFSET[0],
         "2026-11-01T00:00:00.000Z", "assistant", text, search_tool,
         search_thinking))
    return cur.lastrowid


def _build_rows(conn) -> dict:
    return dict(conn.execute(
        "SELECT key, value FROM cache_meta WHERE key > ? AND key < ?",
        (BUILD_PREFIX, "conversation_fts_tokenizer_build;")).fetchall())


def _invalidated(conn):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?",
                       (INVALIDATED,)).fetchone()
    return None if row is None else row[0]


def _seed_build_row(conn, key, fp):
    conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) VALUES(?, ?)",
                 (BUILD_PREFIX + key, fp))
    conn.commit()


# ── Test 5: fingerprint and build-key sensitivity ─────────────────────────

@requires_fts5
def test_fingerprint_and_build_key_see_the_observed_defect_class(monkeypatch):
    """Detects: a probe blind to SQLite 3.54.0's U+FFFE/U+FFFF-as-letter
    regression (reproduced by `tokenchars`) or to diacritic folding, which
    would let a store written by such a build stay certified; and a digest
    that is not a pure function of the build's tokenizer output (two
    processes of one build disagreeing would refuse every admission)."""
    fts = _kernel()
    # Each full-probe fingerprint is computed once (about a second each);
    # the purity check below runs the same digest over the sentinel probe.
    base = fts.fingerprint()
    assert base == fts.fingerprint()  # memoized, stable
    regression = "unicode61 tokenchars '￾￿'"
    assert fts.fingerprint(tokenize_args=regression) != base
    assert fts.fingerprint(tokenize_args="unicode61 remove_diacritics 0") != base

    # A fresh computation on a new connection reproduces the digest.
    def sentinel_digest():
        return fts._probe_digest(fts.SENTINEL_CODE_POINTS, None,
                                 truncation_row=True)

    assert sentinel_digest() == sentinel_digest()
    conn = sqlite3.connect(":memory:")
    try:
        key = fts.build_key(conn)
        assert key == fts.build_key(conn)
        monkeypatch.setattr(fts, "_BUILD_KEY_MEMO", {})
        assert fts.build_key(conn) == key
        assert fts.build_key(conn, tokenize_args=regression) != key
    finally:
        conn.close()


# ── Test 3: guard transactionality ────────────────────────────────────────

@requires_fts5
def test_guard_retracts_in_the_writers_own_transaction(tmp_path, monkeypatch):
    """Detects: a write by another tokenizer build that leaves the store
    verified, or a retraction that survives the writer's rollback."""
    _, fts, path = _store(tmp_path, monkeypatch)
    a, b = _raw(path), _raw(path)
    try:
        fts.install_conversation_fts_guard(a, key=KEY_A)
        fts.install_conversation_fts_guard(b, key=KEY_B)
        _seed_build_row(a, KEY_A, "fp-a")

        # A listed build's write keeps the state.
        _insert_message(a)
        a.commit()
        assert _build_rows(a) == {BUILD_PREFIX + KEY_A: "fp-a"}

        # B's write retracts inside B's transaction: A still sees the row
        # until B commits, and B's commit carries the retraction.
        _insert_message(b)
        assert _build_rows(a) == {BUILD_PREFIX + KEY_A: "fp-a"}
        b.rollback()
        assert _build_rows(a) == {BUILD_PREFIX + KEY_A: "fp-a"}
        assert _invalidated(a) is None

        _insert_message(b)
        b.commit()
        assert _build_rows(a) == {}
        assert _invalidated(a).startswith(KEY_B + " ")
        assert _invalidated(a).endswith("Z")

        # Admission with A's fingerprint lists B; B's writes then keep it.
        _seed_build_row(a, KEY_A, "fp-a")
        assert fts.admit_compatible_build(b, key=KEY_B, fp="fp-a") is True
        _insert_message(b)
        b.commit()
        assert set(_build_rows(a)) == {BUILD_PREFIX + KEY_A,
                                       BUILD_PREFIX + KEY_B}

        # A different fingerprint is not admitted, and its first write
        # retracts the state.
        a.execute("DELETE FROM cache_meta WHERE key=?", (BUILD_PREFIX + KEY_B,))
        a.commit()
        assert fts.admit_compatible_build(b, key=KEY_B, fp="fp-other") is False
        assert set(_build_rows(a)) == {BUILD_PREFIX + KEY_A}
        b.execute("DELETE FROM conversation_messages WHERE id = "
                  "(SELECT max(id) FROM conversation_messages)")
        b.commit()
        assert _build_rows(a) == {}
    finally:
        a.close()
        b.close()


@requires_fts5
def test_admission_never_exceeds_sixteen_build_rows(tmp_path, monkeypatch):
    """Detects: an unbounded build-row set growing on every SQLite upgrade."""
    _, fts, path = _store(tmp_path, monkeypatch)
    conn = _raw(path)
    try:
        for i in range(16):
            conn.execute("INSERT INTO cache_meta(key, value) VALUES(?, 'fp')",
                         (BUILD_PREFIX + f"{i:064x}",))
        conn.commit()
        assert fts.admit_compatible_build(conn, key=KEY_B, fp="fp") is False
        assert len(_build_rows(conn)) == 16
    finally:
        conn.close()


def _stub_build_key(monkeypatch, fts, key):
    monkeypatch.setattr(fts, "build_key",
                        lambda conn, *, tokenize_args=None: key)


# ── Test 4: corpus operations ─────────────────────────────────────────────

@requires_fts5
@pytest.mark.parametrize("operation",
                         ["rebuild", "delete-all", "clear-without-fts5"])
def test_direct_fts_commands_by_an_unlisted_build_retract(
        tmp_path, monkeypatch, operation):
    """Detects: a direct index write ('rebuild', or the storm-free clear's
    'delete-all') that bypasses the row-level guard and leaves the store
    verified for a build that never verified it; and a clear (with or
    without FTS5) whose per-row guard trigger forfeits SQLite's truncate
    fast path."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    import _cctally_db
    _stub_build_key(monkeypatch, fts, KEY_B)
    conn = _raw(path)
    try:
        for word in ("alpha beta", "gamma", "delta"):
            _insert_message(conn, word)
        conn.commit()
        if operation == "clear-without-fts5":
            # A store an FTS5-less build opened: no permanent triggers, the
            # `fts5_unavailable` marker, and this connection's guard.
            _cctally_db._drop_conversation_fts_triggers(conn)
            conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                         "VALUES('fts5_unavailable', '1')")
            conn.commit()
        _seed_build_row(conn, KEY_A, "fp-a")
        fts.install_conversation_fts_guard(conn)
        if operation == "rebuild":
            fts.conversation_fts_command(conn, "conversation_fts", "rebuild")
        else:
            traced = []
            conn.set_trace_callback(traced.append)
            _cctally_db.clear_conversation_messages(conn)
            conn.set_trace_callback(None)
            # The clear keeps SQLite's truncate fast path: no guard trigger
            # fires per deleted row (a TEMP trigger on the table would turn
            # the clear into a per-row delete), and the recreate step puts
            # the guard back.
            # SQLite reports each trigger subprogram as the outer statement
            # again, so one line means no per-row trigger ran.
            assert traced.count("DELETE FROM conversation_messages") == 1, \
                traced
            assert conn.execute(
                "SELECT count(*) FROM conversation_messages").fetchone()[0] == 0
            assert {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_temp_master WHERE type='trigger' "
                "AND tbl_name='conversation_messages'")} == {
                f"cctally_fts_guard_conversation_messages_{s}"
                for s in ("ai", "au", "ad")}
        conn.commit()
        assert _build_rows(conn) == {}
        assert _invalidated(conn).startswith(KEY_B + " ")
    finally:
        conn.close()


@requires_fts5
@pytest.mark.parametrize("isolation", ["legacy", "autocommit"])
@pytest.mark.parametrize("operation", ["rebuild", "delete-all"])
def test_direct_fts_command_holds_the_write_lock_from_its_check(
        tmp_path, monkeypatch, operation, isolation):
    """Detects: a certification by another connection committing between
    the unlisted-build check and the direct FTS command, so the command's
    postings land under a verification that no longer covers them."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    import _cctally_db
    _stub_build_key(monkeypatch, fts, KEY_B)
    conn = _raw(path)
    if isolation == "autocommit":
        conn.isolation_level = None
    other = sqlite3.connect(str(path), timeout=0)
    attempts = []
    real_retract = fts.retract_unless_listed

    def retract_then_race(c, **kw):
        retracted = real_retract(c, **kw)
        try:
            other.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                          "VALUES(?, 'fp-a')", (BUILD_PREFIX + KEY_A,))
            other.commit()
            attempts.append("certified")
        except sqlite3.OperationalError:
            other.rollback()
            attempts.append("locked")
        return retracted

    try:
        _insert_message(conn, "alpha beta")
        if conn.in_transaction:
            conn.commit()
        fts.install_conversation_fts_guard(conn)
        monkeypatch.setattr(fts, "retract_unless_listed", retract_then_race)
        if operation == "rebuild":
            fts.conversation_fts_command(conn, "conversation_fts", "rebuild")
        else:
            _cctally_db.clear_conversation_messages(conn)
        if conn.in_transaction:
            conn.commit()
        assert attempts and set(attempts) == {"locked"}, attempts
        assert _build_rows(conn) == {}
    finally:
        other.close()
        conn.close()


@requires_fts5
@pytest.mark.parametrize("dispatcher_raises", [False, True],
                         ids=["dispatcher-ok", "dispatcher-raises"])
def test_opener_that_applies_a_conversations_migration_retracts(
        tmp_path, monkeypatch, dispatcher_raises):
    """Detects: a migration that recreates a content table or writes an FTS
    table directly leaving another build's verification in place, including
    when the dispatcher raises after that migration committed."""
    import shutil
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    fts = _kernel()
    pre = (Path(__file__).resolve().parent / "fixtures" / "migrations"
           / "per-migration"
           / "conversations_013_conversation_sessions_input_fingerprint"
           / "pre.sqlite")
    path = Path(_cctally_core.CONVERSATIONS_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pre, path)
    seed = _raw(path)
    try:
        assert seed.execute("PRAGMA user_version").fetchone()[0] == 12
        _seed_build_row(seed, KEY_A, "fp-a")
    finally:
        seed.close()
    _stub_build_key(monkeypatch, fts, KEY_B)
    if dispatcher_raises:
        import _cctally_cache
        real_dispatch = _cctally_cache._run_pending_migrations

        opened = []

        def dispatch_then_raise(*args, **kwargs):
            opened.append(args[0])
            real_dispatch(*args, **kwargs)
            raise RuntimeError("dispatcher failed after a migration")

        monkeypatch.setattr(_cctally_cache, "_run_pending_migrations",
                            dispatch_then_raise)
        with pytest.raises(RuntimeError, match="dispatcher failed"):
            ns["open_conversations_db"](attach_cache=False)
        opened[0].close()
        conn = _raw(path)
    else:
        conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert _build_rows(conn) == {}
        assert _invalidated(conn).startswith(KEY_B + " ")
    finally:
        conn.close()


@requires_fts5
def test_opener_writes_nothing_on_a_store_without_build_rows(
        tmp_path, monkeypatch):
    """Detects: the guard or the admission churning cache_meta (and every
    golden that snapshots it) on a store that was never verified."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    _stub_build_key(monkeypatch, fts, KEY_B)
    before = _raw(path)
    try:
        snapshot = before.execute(
            "SELECT key, value FROM cache_meta ORDER BY key").fetchall()
    finally:
        before.close()
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        _insert_message(conn)
        conn.commit()
        assert conn.execute(
            "SELECT key, value FROM cache_meta ORDER BY key").fetchall() \
            == snapshot
        # The opener installed the guard for this build's key.
        assert conn.execute(
            "SELECT count(*) FROM sqlite_temp_master WHERE type='trigger' "
            "AND name LIKE 'cctally_fts_guard_%' AND sql LIKE ?",
            (f"%{KEY_B}%",)).fetchone()[0] == 9
    finally:
        conn.close()


# ── Test 3 extension: the retention prune's fallback switch ──────────────

@requires_fts5
def test_retention_fallback_deletion_by_unlisted_build_retracts(
        tmp_path, monkeypatch):
    """Detects: the retention prune's switch to temp_store=FILE deleting the
    TEMP guard, so a large-group deletion by another build leaves the store
    verified (spec N1)."""
    import datetime as dt
    import importlib
    ns, fts, path = _store(tmp_path, monkeypatch)
    retention = importlib.import_module("_lib_conversation_retention")
    importlib.import_module("_lib_write_io").reset_for_tests()
    ns["save_config"]({"conversation": {"retention_days": 180}})
    seed = _raw(path)
    try:
        for i in range(60):
            seed.execute(
                "INSERT INTO conversation_messages (session_id, uuid, "
                "source_path, byte_offset, timestamp_utc, entry_type, text) "
                "VALUES ('old', ?, 'old.jsonl', ?, ?, 'human', 'old text')",
                (f"old-{i}", i, "2025-08-01T12:00:00.000Z"))
        seed.execute(
            "INSERT INTO conversation_messages (session_id, uuid, "
            "source_path, byte_offset, timestamp_utc, entry_type, text) "
            "VALUES ('keep', 'k', 'keep.jsonl', 0, ?, 'human', 'kept')",
            ("2026-07-17T08:00:00.000Z",))
        seed.commit()
        _seed_build_row(seed, KEY_A, "fp-a")
    finally:
        seed.close()
    _stub_build_key(monkeypatch, fts, KEY_B)
    monkeypatch.setattr(retention, "DELETION_FALLBACK_ROW_THRESHOLD", 40)
    at_commit = []
    real_open = retention._open_deletion_connection

    class Spy:
        def __init__(self, inner):
            self._conn = inner

        def commit(self):
            at_commit.append((
                int(self._conn.execute("PRAGMA temp_store").fetchone()[0]),
                _build_rows(self._conn), self._conn.in_transaction))
            return self._conn.commit()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(retention, "_open_deletion_connection",
                        lambda p: Spy(real_open(p)))
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        stats = retention._maybe_prune_conversation_retention(
            conn, now_utc=dt.datetime(2026, 7, 17, 9, tzinfo=dt.timezone.utc),
            retention_days=180, force=True,
            record_phase=lambda kind, payload: None)
        assert stats is not None and stats.complete, stats
        # The fallback group's own transaction carries the retraction.
        assert at_commit and at_commit[0] == (1, {}, True), at_commit
        assert _build_rows(conn) == {}
        assert _invalidated(conn).startswith(KEY_B + " ")
    finally:
        conn.close()


# ── T3: the verification and repair step ─────────────────────────────────

import datetime as _dt  # noqa: E402
import fcntl as _fcntl  # noqa: E402
import hashlib as _hashlib  # noqa: E402
import json as _json  # noqa: E402

NOW = _dt.datetime(2026, 11, 12, 12, 0, tzinfo=_dt.timezone.utc)
PLENTY = 10 ** 13
#: The stored text holds the real U+FFFE after an escaped surrogate, as the
#: affected production row does (spec 2.2); the phrase crosses it.
STALE_TOOL = "Read x 'a\\ud800', 'a￾', 'ok' done"
PHRASE = "ud800.a.ok"


def _plenty(_path):
    return PLENTY


def _verify(fts, mode, *, now=NOW, free=_plenty, notice=None):
    return fts.verify_conversation_fts(
        mode=mode, now=lambda: now, free_bytes=free, notice=notice)


def _seed_stale(conn, *, others=True):
    """Insert rows through the product (the permanent triggers index them),
    then give one row postings its content does not produce: the
    trigger-equivalent 'delete' of its true values and an insert with
    ``a`` + U+FFFE replaced by ``ax`` (what 3.54.0's amalgamation wrote)."""
    if others:
        _insert_message(conn, "a quick note about apples", session_id="s0")
        _insert_message(conn, "another assistant answer", session_id="s0")
    rowid = _insert_message(conn, "tool call", session_id="s1",
                            search_tool=STALE_TOOL)
    row = conn.execute(
        "SELECT text, search_tool, search_thinking FROM conversation_messages "
        "WHERE id=?", (rowid,)).fetchone()
    conn.execute(
        "INSERT INTO conversation_fts(conversation_fts, rowid, text, "
        "search_tool, search_thinking) VALUES('delete', ?, ?, ?, ?)",
        (rowid, *row))
    conn.execute(
        "INSERT INTO conversation_fts(rowid, text, search_tool, "
        "search_thinking) VALUES(?, ?, ?, ?)",
        (rowid, row[0], row[1].replace("a￾", "ax"), row[2]))
    conn.commit()
    return rowid


_D3_KEYS = ("conversation_fts_tokenizer",)


def _source_digest(path) -> str:
    """Every non-FTS table, all columns, every row, ordered; the FTS virtual
    and shadow tables and the D3 rows excluded."""
    conn = _raw(path)
    try:
        fts_names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND sql LIKE 'CREATE VIRTUAL TABLE%'")]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "ORDER BY name")]
        digest = _hashlib.sha256()
        for name in tables:
            if name in fts_names or any(
                    name.startswith(f + "_") for f in fts_names):
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
            order = ", ".join(f'"{c}"' for c in cols)
            rows = conn.execute(
                f'SELECT * FROM "{name}" ORDER BY {order}').fetchall()
            if name == "cache_meta":
                rows = [r for r in rows if not str(r[0]).startswith(_D3_KEYS)]
            digest.update(name.encode() + repr(rows).encode())
        return digest.hexdigest()
    finally:
        conn.close()


def _hits(ns, kind):
    import _lib_conversation_query as cq
    conn = ns["open_conversations_db"]()
    try:
        out = cq.search_conversations(conn, PHRASE, kind=kind)
        return [h["session_id"] for h in out["hits"]]
    finally:
        conn.close()


def _rank1_ok(path, table="conversation_fts") -> bool:
    conn = _raw(path)
    try:
        conn.execute(f"INSERT INTO {table}({table}, rank) "
                     "VALUES('integrity-check', 1)")
        conn.commit()
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def _by_name(result):
    return {ix.name: ix for ix in result.indexes}


# ── Test 1: seeded stale posting, detected and repaired ───────────────────

@requires_fts5
def test_seeded_stale_posting_is_detected_and_repaired(tmp_path, monkeypatch):
    """Detects: the #1094 defect (a phrase search missing a row whose
    postings another tokenizer wrote), any repair that loses or alters
    source content, and a write of the kernel's dynamic-SQL sites (the TEMP
    guard triggers, the rank-1 'integrity-check' and the 'rebuild' command;
    `tests/test_stats_writer_surface_386.py` FROZEN_DYNAMIC_SITES) reaching a
    database other than conversations.db and its TEMP schema."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    conn = ns["open_conversations_db"]()
    try:
        _seed_stale(conn)
    finally:
        conn.close()
    assert _hits(ns, "all") == [] and _hits(ns, "tools") == []
    before = _source_digest(path)

    # The step's own connection, from the opener's guard install onward.
    writes, attached = [], []
    write_actions = {
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE}
    real_prepare = fts.prepare_writer_connection

    def watched_prepare(c):
        attached.append([(r[1], r[2]) for r in
                         c.execute("PRAGMA database_list")])

        def authorize(action, arg1, arg2, db_name, trigger):
            if action in write_actions and trigger is None:
                writes.append((action, arg1, db_name))
            return sqlite3.SQLITE_OK

        c.set_authorizer(authorize)
        real_prepare(c)

    monkeypatch.setattr(fts, "prepare_writer_connection", watched_prepare)
    result = _verify(fts, "force")
    monkeypatch.setattr(fts, "prepare_writer_connection", real_prepare)

    assert attached and all(
        [(n, Path(f).resolve()) for n, f in dbs] == [("main", path.resolve())]
        for dbs in attached), attached
    assert writes and {db for _a, _t, db in writes} <= {"main", "temp"}, \
        writes
    assert {(a, t) for a, t, _db in writes} >= {
        (sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
         "cctally_fts_guard_conversation_messages_ai"),
        (sqlite3.SQLITE_INSERT, "conversation_fts")}, writes

    assert result.outcome == "verified", result
    claude = _by_name(result)["conversation_fts"]
    assert (claude.before, claude.action, claude.after) == (
        "mismatch", "rebuilt", "consistent")
    for name in ("codex_conversation_fts", "conversation_title_fts"):
        ix = _by_name(result)[name]
        assert (ix.before, ix.action, ix.after) == (
            "consistent", "none", "consistent")
    assert result.state.verified
    # The disk guard ran before the rebuild, so its numbers are reported.
    assert result.need_bytes > 0 and result.avail_bytes == PLENTY
    assert _hits(ns, "all") == ["s1"] and _hits(ns, "tools") == ["s1"]
    assert _rank1_ok(path)
    assert _source_digest(path) == before


# ── Test 2: orphan repaired ───────────────────────────────────────────────

@requires_fts5
@pytest.mark.parametrize("others", [True, False],
                         ids=["with-other-rows", "last-content-row"])
def test_orphaned_postings_are_repaired(tmp_path, monkeypatch, others):
    """Detects: orphaned postings (a stale row deleted through conv_fts_ad)
    surviving the step, and an empty content table that hides postings
    being certified without a rebuild."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        rowid = _seed_stale(conn, others=others)
        conn.execute("DELETE FROM conversation_messages WHERE id=?", (rowid,))
        conn.commit()
    finally:
        conn.close()
    assert not _rank1_ok(path)
    ranked = ("SELECT rowid FROM conversation_fts WHERE conversation_fts "
              "MATCH '\"a\"*' ORDER BY rank")
    if others:
        probe = _raw(path)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                probe.execute(ranked).fetchall()
        finally:
            probe.close()

    result = _verify(fts, "force")

    assert result.outcome == "verified", result
    assert _by_name(result)["conversation_fts"].action == "rebuilt"
    assert _rank1_ok(path)
    probe = _raw(path)
    try:
        probe.execute(ranked).fetchall()
    finally:
        probe.close()


# ── Test 6: same key, different behavior ─────────────────────────────────

def _write_meta(path, **fields):
    conn = _raw(path)
    try:
        meta = {"schemaVersion": 1, **fields}
        conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                     "VALUES('conversation_fts_tokenizer', ?)",
                     (_json.dumps(meta),))
        conn.commit()
    finally:
        conn.close()


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


@requires_fts5
@pytest.mark.parametrize("age_days, ran", [(31, True), (29, False)])
def test_backstop_reverifies_a_store_the_build_key_cannot_tell_apart(
        tmp_path, monkeypatch, age_days, ran):
    """Detects: a tokenizer change the build key misses (same key, different
    fingerprint) leaving the store wrongly verified forever."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    _stub_build_key(monkeypatch, fts, KEY_A)
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        _seed_build_row(conn, KEY_A, "f" * 64)
        _seed_stale(conn)
        # The guard stayed silent: this build is listed.
        assert set(_build_rows(conn)) == {BUILD_PREFIX + KEY_A}
    finally:
        conn.close()
    _write_meta(path, verifiedAt=_iso(NOW - _dt.timedelta(days=age_days)),
                lastAttemptAt=_iso(NOW - _dt.timedelta(days=age_days)))

    result = _verify(fts, "automatic")

    if ran:
        assert result.outcome == "verified", result
        assert _by_name(result)["conversation_fts"].action == "rebuilt"
        # A small store's rank-1 pass is fast, so the fingerprint is not
        # recorded (FR1-b): the row holds the empty "unknown" value.
        assert result.state.build_rows == {KEY_A: ""}
    else:
        assert result.outcome == "skipped", result
        assert not _rank1_ok(path)


# ── Test 7: admission and deferral ───────────────────────────────────────

def _cache_meta(path):
    conn = _raw(path)
    try:
        return conn.execute(
            "SELECT key, value FROM cache_meta ORDER BY key").fetchall()
    finally:
        conn.close()


@requires_fts5
@pytest.mark.parametrize("marker", [
    "conversation_rebuild_claude_pending",
    "conversation_rebuild_codex_pending",
    "conversation_search_split_pending",
    "conversation_title_fts_backfill_pending",
])
def test_pending_corpus_work_defers_and_writes_nothing(
        tmp_path, monkeypatch, marker):
    """Detects: the step certifying around (or racing) corpus work that
    clears or refills the indexes."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    conn = _raw(path)
    try:
        conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                     "VALUES(?, '1')", (marker,))
        conn.commit()
    finally:
        conn.close()
    snapshot = _cache_meta(path)
    result = _verify(fts, "force")
    assert (result.outcome, result.deferred_by) == ("deferred", marker)
    assert _cache_meta(path) == snapshot


@requires_fts5
def test_missing_required_index_defers(tmp_path, monkeypatch):
    """Detects: a vacuous verification of a store whose title index is
    missing (it would certify two indexes and call the store verified)."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    import _cctally_db
    conn = _raw(path)
    try:
        _cctally_db._drop_conversation_title_fts_triggers(conn)
        conn.execute("DROP TABLE conversation_title_fts")
        conn.commit()
    finally:
        conn.close()
    result = _verify(fts, "force")
    assert (result.outcome, result.deferred_by) == (
        "deferred", "conversation_title_fts")
    conn = _raw(path)
    try:
        conn.execute("INSERT INTO cache_meta(key, value) VALUES("
                     "'conversation_title_fts_backfill_pending', '1')")
        conn.commit()
    finally:
        conn.close()
    result = _verify(fts, "force")
    assert result.outcome == "deferred"
    assert not result.state.verified


@requires_fts5
def test_held_provider_flock_reports_busy_and_writes_nothing(
        tmp_path, monkeypatch):
    """Detects: the step running rank 1 beside a conversation writer."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    snapshot = _cache_meta(path)
    with open(_cctally_core.CONVERSATIONS_LOCK_PATH, "w") as held:
        _fcntl.flock(held, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        result = _verify(fts, "force")
    assert result.outcome == "busy"
    assert _cache_meta(path) == snapshot
    # The locks were released: the next attempt runs.
    assert _verify(fts, "force").outcome == "verified"


def _short_busy_timeout(monkeypatch):
    """The step's own connection waits 50 ms for a write lock, not 15 s."""
    import _cctally_cache
    real_open = _cctally_cache.open_conversations_db

    def opener(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        conn.execute("PRAGMA busy_timeout = 50")
        return conn

    monkeypatch.setattr(_cctally_cache, "open_conversations_db", opener)


@requires_fts5
@pytest.mark.parametrize("held_from", ["before", "after-attempt-record"])
def test_a_pending_write_transaction_reports_busy_and_certifies_nothing(
        tmp_path, monkeypatch, held_from):
    """Detects: a writer holding conversations.db mid-attempt making the step
    raise (reported as an unopenable store) instead of `busy`, or a
    certification written around a transaction it could not see; and a
    verified store that an attempt could not finish checking staying
    certified (review C1: the attempt record withdraws it)."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    assert _verify(fts, "force").outcome == "verified"
    _short_busy_timeout(monkeypatch)
    other = _raw(path)
    real_record = fts._record_attempt

    def record_then_hold(conn, meta):
        real_record(conn, meta)
        if not other.in_transaction:
            other.execute("BEGIN IMMEDIATE")

    try:
        if held_from == "before":
            other.execute("BEGIN IMMEDIATE")
        else:
            monkeypatch.setattr(fts, "_record_attempt", record_then_hold)
        before = _state(fts, path)
        result = _verify(fts, "force")
        assert result.outcome == "busy", result
        state = _state(fts, path)
        if held_from == "before":
            # Nothing recorded: nothing changes.
            assert (state.verified, state.build_rows, state.invalidated) == (
                before.verified, before.build_rows, before.invalidated)
        else:
            assert (state.verified, state.build_rows, state.reason) == (
                False, {}, "checking"), state
        other.rollback()
        monkeypatch.setattr(fts, "_record_attempt", real_record)
        assert _verify(fts, "force").outcome == "verified"
    finally:
        other.close()


@requires_fts5
@pytest.mark.parametrize("failing", ["exclusive", "downgrade"])
def test_a_failing_flock_counts_as_contention(tmp_path, monkeypatch, failing):
    """Detects: an flock error (a filesystem without flock support, an
    interrupted call) escaping the step as a crash, or a failed downgrade
    leaving the maintenance flock held exclusive for the whole check."""
    import types
    ns, fts, path = _store(tmp_path, monkeypatch)
    snapshot = _cache_meta(path)
    real = _fcntl.flock

    def flock(fh, op):
        if failing == "exclusive" and op == _fcntl.LOCK_EX | _fcntl.LOCK_NB:
            raise OSError(5, "Input/output error")
        if failing == "downgrade" and op == _fcntl.LOCK_SH:
            raise OSError(5, "Input/output error")
        return real(fh, op)

    with monkeypatch.context() as scoped:
        scoped.setattr(fts, "fcntl", types.SimpleNamespace(
            flock=flock, LOCK_EX=_fcntl.LOCK_EX, LOCK_NB=_fcntl.LOCK_NB,
            LOCK_SH=_fcntl.LOCK_SH, LOCK_UN=_fcntl.LOCK_UN))
        assert _verify(fts, "force").outcome == "busy"
    assert _cache_meta(path) == snapshot
    # Every lock was released.
    for lock in (_cctally_core.CONVERSATIONS_LOCK_MAINTENANCE_PATH,
                 _cctally_core.CONVERSATIONS_LOCK_PATH,
                 _cctally_core.CONVERSATIONS_LOCK_CODEX_PATH):
        with open(lock, "w") as fh:
            _fcntl.flock(fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            _fcntl.flock(fh, _fcntl.LOCK_UN)


@requires_fts5
def test_cooldown_counts_from_the_committed_attempt(tmp_path, monkeypatch):
    """Detects: repeated multi-minute automatic attempts (no cooldown, or a
    cooldown lost when an attempt is interrupted); and an attempt
    interrupted after its record leaving the store certified, so a check
    that never finished keeps vouching for the indexes (review C1)."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    _write_meta(path, lastAttemptAt=_iso(NOW - _dt.timedelta(hours=23)))
    assert _verify(fts, "automatic").outcome == "skipped"
    assert _verify(fts, "force").outcome == "verified"

    # A verified store due for its 30-day backstop: an attempt interrupted
    # after its record still starts the cooldown, and leaves it unverified.
    old = _iso(NOW - _dt.timedelta(days=31))
    _write_meta(path, verifiedAt=old, lastAttemptAt=old)
    assert _state(fts, path).verified

    def boom(*args, **kwargs):
        raise RuntimeError("interrupted")

    # A scoped patch: undoing the test's own monkeypatch would also undo the
    # temp data dir redirect.
    with monkeypatch.context() as scoped:
        scoped.setattr(fts, "_check_indexes", boom)
        with pytest.raises(RuntimeError):
            _verify(fts, "automatic")
    state = _state(fts, path)
    assert state.meta.get("lastAttemptAt") == _iso(NOW)
    assert (state.verified, state.reason) == (False, "checking"), state
    assert _verify(fts, "automatic",
                   now=NOW + _dt.timedelta(hours=1)).outcome == "skipped"
    # Eligible again once the cooldown has passed; only certification
    # restores the verified state.
    assert _verify(fts, "automatic",
                   now=NOW + _dt.timedelta(hours=25)).outcome == "verified"
    assert _state(fts, path).reason == "verified"


@requires_fts5
@pytest.mark.parametrize("mode, outcome", [("check", "mismatch"),
                                           ("force", "verified")])
def test_a_proven_mismatch_unverifies_even_when_its_retraction_fails(
        tmp_path, monkeypatch, mode, outcome):
    """Detects (review C1): a recently verified store whose check proves a
    mismatch staying certified when the retraction's write fails, so the
    automatic step skips it until the 30-day backstop instead of retrying
    after the 24-hour cooldown; and that write failure turning a proven
    mismatch into `busy` (or stopping the repair)."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    assert _verify(fts, "force").outcome == "verified"
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        _seed_stale(conn)
    finally:
        conn.close()
    assert _state(fts, path).verified

    def failing_retract(conn, *, reason):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as scoped:
        scoped.setattr(fts, "retract", failing_retract)
        result = _verify(fts, mode)
    assert result.outcome == outcome, result
    if mode == "force":
        assert result.state.verified and _rank1_ok(path)
        return
    state = _state(fts, path)
    assert not state.verified, state
    assert _verify(fts, "automatic",
                   now=NOW + _dt.timedelta(hours=1)).outcome == "skipped"
    assert _verify(fts, "automatic",
                   now=NOW + _dt.timedelta(hours=25)).outcome == "verified"
    assert _rank1_ok(path)


def _state(fts, path):
    conn = _raw(path)
    try:
        return fts.read_state(conn)
    finally:
        conn.close()


@requires_fts5
def test_retraction_after_verification_waits_for_the_cooldown(
        tmp_path, monkeypatch):
    """Detects: a store written alternately by two tokenizers re-verified on
    every pass instead of at most daily (spec R1)."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    assert _verify(fts, "automatic").outcome == "verified"
    other = _raw(path)
    try:
        fts.install_conversation_fts_guard(other, key=KEY_B)
        _insert_message(other)
        other.commit()
    finally:
        other.close()
    assert not _state(fts, path).verified
    later = NOW + _dt.timedelta(hours=1)
    assert _verify(fts, "automatic", now=later).outcome == "skipped"
    assert _verify(fts, "automatic",
                   now=NOW + _dt.timedelta(hours=25)).outcome == "verified"


# ── FR1-b: the fingerprint is recorded only where admission pays ─────────

def _meta_doc(path) -> dict:
    conn = _raw(path)
    try:
        row = conn.execute("SELECT value FROM cache_meta WHERE key=?",
                           ("conversation_fts_tokenizer",)).fetchone()
        return _json.loads(row[0])
    finally:
        conn.close()


@requires_fts5
def test_fast_certification_records_no_fingerprint_and_opener_skips_it(
        tmp_path, monkeypatch):
    """Detects: a second of fingerprint work added to every verification and
    to every opener of a store a new build first touches, on stores whose
    whole rank-1 pass is cheaper than the fingerprint that would spare it;
    and an empty (unknown) fingerprint admitting a build it never compared."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    result = _verify(fts, "force")
    assert result.outcome == "verified", result
    assert result.fingerprint is None
    rows = _state(fts, path).build_rows
    assert list(rows.values()) == [""], rows
    assert _meta_doc(path)["fingerprint"] is None
    probe = _raw(path)
    try:
        assert fts.admit_compatible_build(probe, key=KEY_B, fp="") is False
    finally:
        probe.close()
    assert _state(fts, path).build_rows == rows

    def no_fingerprint(*args, **kwargs):
        raise AssertionError("fingerprint computed by the opener")

    _stub_build_key(monkeypatch, fts, KEY_B)
    monkeypatch.setattr(fts, "fingerprint", no_fingerprint)
    snapshot = _cache_meta(path)
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert _cache_meta(path) == snapshot
        # This build's first content write retracts the state.
        _insert_message(conn)
        conn.commit()
        assert _build_rows(conn) == {}
        assert _invalidated(conn).startswith(KEY_B + " ")
    finally:
        conn.close()


@requires_fts5
def test_slow_certification_records_fingerprint_and_admits_an_equal_build(
        tmp_path, monkeypatch):
    """Detects: a large store (rank 1 of two seconds or more) certified
    without its fingerprint, so every SQLite upgrade that keeps the tokenizer
    re-runs a multi-minute verification instead of admitting the build."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    ticks = iter(range(0, 10_000))
    # Each rank-1 statement appears to take one second.
    monkeypatch.setattr(fts, "_rank1_clock", lambda: float(next(ticks)))
    result = _verify(fts, "force")
    assert result.outcome == "verified", result
    real = fts.fingerprint()
    assert real and result.fingerprint == real
    assert list(_state(fts, path).build_rows.values()) == [real]
    assert _meta_doc(path)["fingerprint"] == real

    _stub_build_key(monkeypatch, fts, KEY_B)
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert _build_rows(conn).get(BUILD_PREFIX + KEY_B) == real
        _insert_message(conn)
        conn.commit()
        assert BUILD_PREFIX + KEY_B in _build_rows(conn)
        assert _invalidated(conn) is None
    finally:
        conn.close()


# ── FR1-c: the "checking" notice only where it can take minutes ──────────

@requires_fts5
@pytest.mark.parametrize("large", [False, True], ids=["small", "over-1gib"])
def test_checking_notice_only_for_a_store_of_a_gibibyte_or_more(
        tmp_path, monkeypatch, large):
    """Detects: a notice promising minutes of work printed by every daily
    `cache-sync` on stores whose check takes milliseconds, or withheld on a
    store large enough for the check to take minutes."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    if large:
        monkeypatch.setattr(fts, "_store_bytes", lambda p: 1024 ** 3)
    lines = []
    assert _verify(fts, "automatic", notice=lines.append).outcome == "verified"
    assert (fts.NOTICE_CHECKING in lines) is large, lines


# ── Test 9: disk guard ────────────────────────────────────────────────────

@requires_fts5
@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 43, 0),
                    reason="the source term needs octet_length")
def test_disk_guard_refuses_and_a_proven_mismatch_unverifies(
        tmp_path, monkeypatch, capsys):
    """Detects: a gigabyte-scale rebuild admitted on a near-full volume
    because the guard sized it by the (emptied) index instead of the text it
    re-indexes; a proven inconsistency left certified when its repair is
    refused; an index rebuilt beside a refused one reported unchecked; and a
    refusal the automatic step and the command report nowhere."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    import _cctally_db
    monkeypatch.setattr(fts, "DISK_HEADROOM_BYTES", 4096)
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        for i in range(20):
            _insert_message(conn, f"message number {i} " + "word " * 4000)
        conn.execute("INSERT INTO conversation_ai_titles(session_id, "
                     "ai_title, source_path, byte_offset) "
                     "VALUES('s1', 'a short title', 't.jsonl', 0)")
        conn.commit()
    finally:
        conn.close()
    assert _verify(fts, "force").outcome == "verified"
    raw = _raw(path)
    try:
        # Content kept, both indexes emptied, by a writer that bypasses the
        # guard.
        for table in ("conversation_fts", "conversation_title_fts"):
            raw.execute(f"INSERT INTO {table}({table}) VALUES('delete-all')")
        raw.commit()
        source = raw.execute(
            "SELECT sum(length(CAST(text AS BLOB)) "
            "+ length(CAST(search_tool AS BLOB)) "
            "+ length(CAST(search_thinking AS BLOB))) "
            "FROM conversation_messages").fetchone()[0]
        index_now = fts._index_bytes(raw, "conversation_fts")
        title_need = fts.disk_requirement(raw, "conversation_title_fts")
    finally:
        raw.close()
    assert source > 4 * max(index_now, 1)
    true_need = 2 * max(index_now, source) + 4096
    index_only_need = 2 * index_now + 4096
    floor = max(index_only_need, title_need)
    free = floor + (true_need - floor) // 2
    assert floor < free < true_need
    assert _state(fts, path).verified
    # Old enough for the automatic step to be due.
    meta = _meta_doc(path)
    old = _iso(NOW - _dt.timedelta(days=31))
    _write_meta(path, **{**{k: v for k, v in meta.items()
                            if k != "schemaVersion"},
                         "verifiedAt": old, "lastAttemptAt": old})

    lines = []
    result = _verify(fts, "automatic", free=lambda p: free,
                     notice=lines.append)

    assert result.outcome == "disk", result
    claude = _by_name(result)["conversation_fts"]
    assert (claude.before, claude.action) == ("mismatch", "refused")
    title = _by_name(result)["conversation_title_fts"]
    assert (title.before, title.action, title.after) == (
        "mismatch", "rebuilt", "consistent")
    assert (result.need_bytes, result.avail_bytes) == (true_need, free)
    assert not result.state.verified
    assert not _rank1_ok(path)
    assert _rank1_ok(path, "conversation_title_fts")
    assert fts.NOTICE_NOT_REBUILT_DISK.format(
        name="conversation_fts", need=fts.format_bytes(true_need),
        avail=fts.format_bytes(free)) in lines, lines

    monkeypatch.setattr(_cctally_db._fts_integrity, "_volume_free_bytes",
                        lambda p: free)
    code, out, err = _run_command(ns, [], capsys)
    assert code == 3
    assert fts.TEXT_DISK_REFUSED.format(
        name="conversation_fts", need=fts.format_bytes(true_need),
        avail=fts.format_bytes(free)) in out.splitlines(), out
    assert fts.TEXT_SUMMARY_DISK in err.splitlines(), err


@requires_fts5
def test_failed_rebuild_reports_repair_failed_with_a_notice(
        tmp_path, monkeypatch):
    """Detects: an automatic repair that fails leaving no trace in the
    dashboard or `cache-sync` output, and its index reported unchecked."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        _seed_stale(conn)
    finally:
        conn.close()
    real_command = fts.conversation_fts_command

    def failing(conn, table, command):
        if command == "rebuild":
            raise sqlite3.OperationalError("disk I/O error")
        return real_command(conn, table, command)

    monkeypatch.setattr(fts, "conversation_fts_command", failing)
    lines = []
    result = _verify(fts, "automatic", notice=lines.append)
    assert result.outcome == "repair-failed", result
    claude = _by_name(result)["conversation_fts"]
    assert (claude.before, claude.after) == ("mismatch", "error")
    assert all(ix.after != "unchecked" for ix in result.indexes)
    assert fts.NOTICE_REPAIR_FAILED.format(name="conversation_fts") in lines


@requires_fts5
@pytest.mark.parametrize("case", ["rebuilt-not-rechecked", "check-error"])
def test_reindex_search_text_names_what_was_not_checked(
        tmp_path, monkeypatch, capsys, case):
    """Detects: the command printing nothing for an index it could not
    check, or calling a rebuilt index consistent when its re-check raised."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    import _cctally_db
    state = fts.FtsState(False, {}, None, {}, "mismatch")
    if case == "rebuilt-not-rechecked":
        row = fts.IndexResult("conversation_fts", "mismatch", "rebuilt",
                              "error", 75.0, "database is locked")
        outcome, expected = "repair-failed", fts.TEXT_REBUILT_NOT_RECHECKED.format(
            name="conversation_fts", dur=fts.format_duration(75.0),
            error="database is locked")
        summary = fts.TEXT_SUMMARY_REPAIR_FAILED
    else:
        row = fts.IndexResult("conversation_fts", "error", "none", "error",
                              0.0, "database is locked")
        outcome, expected = "busy", fts.TEXT_CHECK_ERROR.format(
            name="conversation_fts", error="database is locked")
        summary = fts.TEXT_BUSY
    result = fts.VerifyResult(outcome, (row,), None, sqlite3.sqlite_version,
                              None, state)
    monkeypatch.setattr(_cctally_db._fts_integrity, "verify_conversation_fts",
                        lambda **kw: result)
    code, out, err = _run_command(ns, [], capsys)
    assert code == 3
    assert out.splitlines() == [expected]
    assert err.splitlines() == [summary]


@requires_fts5
def test_check_mismatch_unverifies_without_rebuilding(tmp_path, monkeypatch):
    """Detects: `check` leaving a store it proved inconsistent certified, or
    rebuilding when asked only to check; and a `check` that finds every
    index consistent leaving the store unverified (the attempt record
    withdraws the verification, so the check must certify, review C1)."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    assert _verify(fts, "force").outcome == "verified"
    consistent = _verify(fts, "check")
    assert consistent.outcome == "verified", consistent
    assert _state(fts, path).reason == "verified"
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        _seed_stale(conn)
    finally:
        conn.close()
    assert _state(fts, path).verified
    result = _verify(fts, "check")
    assert result.outcome == "mismatch"
    assert _by_name(result)["conversation_fts"].action == "none"
    assert not result.state.verified
    assert _state(fts, path).reason == "mismatch"
    assert not _rank1_ok(path)


# ── Test 8: `cctally db reindex-search` ───────────────────────────────────

@pytest.mark.parametrize("seconds, rendered", [
    (41.24, "41.2s"), (59.94, "59.9s"), (59.95, "1m 00s"),
    (59.999, "1m 00s"), (785.0, "13m 05s")])
def test_a_duration_rounds_before_it_chooses_its_unit(seconds, rendered):
    """Detects: a rebuild of just under a minute reported as `60.0s` (the
    seconds rounded up after the under-a-minute branch was chosen)."""
    assert _kernel().format_duration(seconds) == rendered


def _run_command(ns, argv, capsys):
    args = ns["build_parser"]().parse_args(["db", "reindex-search", *argv])
    code = args.func(args)
    out = capsys.readouterr()
    return code, out.out, out.err


@requires_fts5
def test_reindex_search_command_exit_codes_and_envelope(
        tmp_path, monkeypatch, capsys):
    """Detects: the command misreporting a consistent store, a `--check`
    finding, or consuming #945-class corpus work (a pending Codex rebuild
    with retained events) instead of deferring to it."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    import _cctally_db
    monkeypatch.setattr(_cctally_db._fts_integrity, "_volume_free_bytes",
                        _plenty)

    code, out, _err = _run_command(ns, [], capsys)
    assert code == 0, out
    assert out.splitlines()[-1] == fts.TEXT_SUMMARY_OK.format(
        sqlite_version=sqlite3.sqlite_version)

    code, out, _err = _run_command(ns, ["--json"], capsys)
    payload = _json.loads(out)
    assert code == 0
    assert list(payload)[0] == "schemaVersion" and payload["schemaVersion"] == 1
    for key in ("outcome", "indexes", "deferredBy", "sqliteVersion",
                "fingerprint", "state"):
        assert key in payload, key
    assert payload["outcome"] == "verified"
    assert payload["state"]["verified"] == "verified"
    assert {ix["name"] for ix in payload["indexes"]} == {
        "conversation_fts", "codex_conversation_fts",
        "conversation_title_fts"}
    assert set(payload["indexes"][0]) == {
        "name", "before", "action", "after", "seconds"}

    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        _seed_stale(conn)
    finally:
        conn.close()
    code, out, _err = _run_command(ns, ["--check", "--json"], capsys)
    payload = _json.loads(out)
    assert (code, payload["outcome"]) == (1, "mismatch")
    assert payload["state"]["verified"] == "unverified"
    code, out, _err = _run_command(ns, ["--check"], capsys)
    assert code == 1
    assert fts.TEXT_CHECK_MISMATCH.format(
        name="conversation_fts") in out.splitlines()
    assert out.splitlines()[-1] == fts.TEXT_SUMMARY_CHECK_MISMATCH

    # A pending Codex corpus rebuild with retained events: deferred, the
    # marker and every source row untouched.
    conn = _raw(path)
    try:
        conn.execute(
            "INSERT INTO codex_conversation_events(source_path, line_offset, "
            "source_root_key, conversation_key, payload_json) "
            "VALUES('r.jsonl', 0, 'root', 'k', '{}')")
        conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                     "VALUES('conversation_rebuild_codex_pending', '1')")
        conn.commit()
    finally:
        conn.close()
    before = _source_digest(path)
    code, out, _err = _run_command(ns, ["--json"], capsys)
    payload = _json.loads(out)
    assert (code, payload["outcome"]) == (3, "deferred")
    assert payload["deferredBy"] == "conversation_rebuild_codex_pending"
    assert _source_digest(path) == before
    code, out, _err = _run_command(ns, [], capsys)
    assert code == 3
    assert fts.TEXT_DEFERRED.format(
        marker="conversation_rebuild_codex_pending") in _err.splitlines()


@requires_fts5
def test_reindex_search_json_reports_an_unopenable_store(
        tmp_path, monkeypatch, capsys):
    """Detects: `--json` consumers (scripts, the dashboard) receiving an
    empty stdout they cannot parse when conversations.db cannot be opened."""
    ns, fts, path = _store(tmp_path, monkeypatch)
    import _cctally_cache

    def unopenable(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(_cctally_cache, "open_conversations_db", unopenable)
    code, out, err = _run_command(ns, ["--json"], capsys)
    payload = _json.loads(out)
    assert code == 3
    assert list(payload)[0] == "schemaVersion" and payload["schemaVersion"] == 1
    assert payload["outcome"] == "store-unavailable"
    assert "unable to open database file" in payload["error"]
    assert "unable to open database file" in err
    code, out, err = _run_command(ns, [], capsys)
    assert code == 3 and out == "" and "unable to open database file" in err


@requires_fts5
def test_reindex_search_reports_a_database_error_after_the_store_opened(
        tmp_path, monkeypatch, capsys):
    """Detects: a database error from the step's first reads (after the
    store opened) escaping `db reindex-search` as a traceback, outside its
    exit-code set, and `--json` consumers again receiving no envelope."""
    ns, fts, path = _store(tmp_path, monkeypatch)

    def malformed(conn):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(fts, "_plan", malformed)
    code, out, err = _run_command(ns, ["--json"], capsys)
    payload = _json.loads(out)
    assert code == 3
    assert list(payload)[0] == "schemaVersion" and payload["schemaVersion"] == 1
    assert payload["outcome"] == "error"
    assert "database disk image is malformed" in payload["error"]
    assert payload["state"] is None and payload["indexes"] == []
    expected = fts.TEXT_CHECK_FAILED.format(
        kind="DatabaseError", error="database disk image is malformed")
    assert expected in err.splitlines()
    code, out, err = _run_command(ns, [], capsys)
    assert code == 3 and out == "" and expected in err.splitlines()
