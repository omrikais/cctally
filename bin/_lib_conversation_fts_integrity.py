"""#1094: conversation FTS postings written by another SQLite tokenizer.

``conversation_fts``, ``codex_conversation_fts`` and ``conversation_title_fts``
are external-content FTS5 indexes. Every posting was produced by the tokenizer
of the SQLite build that wrote its row, and the delete/update triggers remove
the postings the RUNNING build produces from the row's content. When two builds
tokenize some text differently (SQLite 3.54.0's amalgamation treated U+FFFE and
U+FFFF as letters), the index keeps the first build's postings: a phrase search
misses the row and deleting the row leaves orphaned postings. Only FTS5's
content-aware ``integrity-check`` (rank 1) detects it.

This kernel owns (spec D1-D5):

* the tokenizer fingerprint (D1, about a second; recorded only when the
  certifying rank-1 pass took ``FINGERPRINT_MIN_RANK1_SECONDS`` or more) and
  the cheap build key (D2), both memoized per process and keyed by the
  tokenizer argument string;
* the stored state (D3): ``cache_meta`` rows in ``conversations.db`` — one
  ``conversation_fts_tokenizer_build:<key>`` row per build whose writes keep
  the verified indexes consistent (verified <=> at least one exists), the
  ``conversation_fts_tokenizer_invalidated`` row, and the JSON history document
  ``conversation_fts_tokenizer`` (never authoritative for state);
* the write-time guard (D4): per-connection ``TEMP`` triggers on each content
  table that retract the state inside the writer's own transaction when an
  unlisted build writes, the admission of a build with an equal fingerprint,
  and ``conversation_fts_command`` for every direct FTS command;
* the verification and repair step (D5), ``verify_conversation_fts``.

Changing ``PRAGMA temp_store`` deletes every TEMP object of a connection, the
guard included, so every such change on a conversations connection is
followed at once by ``install_conversation_fts_guard`` (spec D4, N1).

Stdlib-only; imports ``_cctally_core`` for paths and the conversations opener
lazily, so ``_cctally_db`` / ``_cctally_cache`` / the retention module can
import this at module top without a cycle.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Callable, Iterable

import _cctally_core

UTC = dt.timezone.utc

#: The conversation indexes declare no ``tokenize=`` option, so they use the
#: default ``unicode61`` with ``remove_diacritics=1``. One shared constant: the
#: fingerprint and build key probe exactly what the indexes run.
CONVERSATION_FTS_TOKENIZE_ARGS: "str | None" = None

#: ``(fts_table, content_table, indexed_columns)`` for the three indexes
#: (``bin/_cctally_db.py`` FTS DDL).
CONTENT_TABLES: "tuple[tuple[str, str, tuple[str, ...]], ...]" = (
    ("conversation_fts", "conversation_messages",
     ("text", "search_tool", "search_thinking")),
    ("codex_conversation_fts", "codex_conversation_messages",
     ("text", "search_tool", "search_thinking")),
    ("conversation_title_fts", "conversation_ai_titles", ("ai_title",)),
)

BUILD_ROW_PREFIX = "conversation_fts_tokenizer_build:"
#: Exclusive upper bound of the build-row key range (``;`` follows ``:``).
_BUILD_ROW_UPPER = "conversation_fts_tokenizer_build;"
INVALIDATED_KEY = "conversation_fts_tokenizer_invalidated"
#: The invalidation reason the attempt record writes (review C1): an attempt
#: that started and has not certified.
CHECKING_REASON = "checking"
META_KEY = "conversation_fts_tokenizer"
META_SCHEMA_VERSION = 1

MAX_BUILD_ROWS = 16
ADMISSION_BUSY_TIMEOUT_MS = 250
#: A certification records this build's fingerprint only when its rank-1
#: pass took at least this long: below it, re-verifying a store under a new
#: build costs less than computing the fingerprint that would admit it, so
#: the build row holds ``""`` (unknown), which never admits anything.
FINGERPRINT_MIN_RANK1_SECONDS = 2.0

_ANY_BUILD_ROW_SQL = (
    f"EXISTS (SELECT 1 FROM cache_meta WHERE key > '{BUILD_ROW_PREFIX}' "
    f"AND key < '{_BUILD_ROW_UPPER}')")
#: A build row with a recorded fingerprint: the only kind admission can match.
_ANY_FINGERPRINTED_BUILD_ROW_SQL = (
    f"EXISTS (SELECT 1 FROM cache_meta WHERE key > '{BUILD_ROW_PREFIX}' "
    f"AND key < '{_BUILD_ROW_UPPER}' AND value <> '')")
_DELETE_BUILD_ROWS_SQL = (
    f"DELETE FROM cache_meta WHERE key > '{BUILD_ROW_PREFIX}' "
    f"AND key < '{_BUILD_ROW_UPPER}'")
_UTC_NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"

_GUARD_PREFIX = "cctally_fts_guard_"
_GUARD_EVENTS = (("ai", "INSERT"), ("au", "UPDATE"), ("ad", "DELETE"))

# ── user-visible text (approved by the operator 2026-11-12, #1094) ──
# Each string is defined once so the wording can be swapped in one place.

TEXT_CONSISTENT = "{name:<24}consistent (checked in {secs})"
TEXT_REBUILT = "{name:<24}out of date → rebuilt in {dur} → consistent"
TEXT_REBUILT_STILL_BAD = (
    "{name:<24}out of date → rebuilt in {dur} → still inconsistent")
TEXT_CHECK_MISMATCH = "{name:<24}out of date"
TEXT_DISK_REFUSED = (
    "{name:<24}out of date; not rebuilt: needs {need} free on the store's "
    "volume, {avail} available")
TEXT_SUMMARY_OK = (
    "Search indexes match this SQLite's tokenizer ({sqlite_version}).")
TEXT_SUMMARY_CHECK_MISMATCH = (
    "Search indexes are out of date; run cctally db reindex-search to "
    "rebuild them.")
TEXT_DEFERRED = (
    "Search indexes not checked: a conversation rebuild is pending "
    "({marker}); run again after it finishes.")
TEXT_BUSY = (
    "Search indexes not checked: another cctally process is maintaining the "
    "conversation store; try again shortly.")
# Added during implementation; approved with the rest of the batch.
TEXT_UNAVAILABLE = (
    "Search indexes not checked: this SQLite build has no FTS5 full-text "
    "search.")
TEXT_OPEN_FAILED = "cctally: could not open conversations.db ({error})"
NOTICE_CHECKING = (
    "[conversations] checking the search indexes against their messages "
    "(after an upgrade, then every 30 days); this can take a few minutes")
NOTICE_REBUILDING = (
    "[conversations] search index {name} disagrees with its messages (a "
    "SQLite tokenizer change); rebuilding it, which can take several minutes")
NOTICE_REBUILT = "[conversations] search index {name} rebuilt in {dur}"
# The dashboard's failure line, defined here so `cache-sync` reports a
# failure after the store opened the same way.
TEXT_CHECK_FAILED = "[conversations] search index check failed: {kind}: {error}"
# FR2 (review F7, F13).
TEXT_CHECK_ERROR = "{name:<24}not checked: {error}"
TEXT_REBUILT_NOT_RECHECKED = (
    "{name:<24}out of date → rebuilt in {dur} → not re-checked: {error}")
TEXT_SUMMARY_DISK = (
    "Search indexes are out of date and were not rebuilt; free space on the "
    "store's volume, then run cctally db reindex-search.")
TEXT_SUMMARY_REPAIR_FAILED = (
    "Search indexes are still out of date after a rebuild; run cctally db "
    "reindex-search again.")
NOTICE_NOT_REBUILT_DISK = (
    "[conversations] search index {name} disagrees with its messages but was "
    "not rebuilt: needs {need} free on the store's volume, {avail} available; "
    "run cctally db reindex-search after freeing space")
NOTICE_REPAIR_FAILED = (
    "[conversations] search index {name} still disagrees with its messages "
    "after a rebuild; run cctally db reindex-search")


def format_secs(seconds: float) -> str:
    """``41.2s``."""
    return f"{seconds:.1f}s"


def format_duration(seconds: float) -> str:
    """``13m 05s``; under a minute ``41.2s``. Rounded to the shown tenth
    before the unit is chosen, so 59.95 s reads ``1m 00s``, never
    ``60.0s``."""
    if round(seconds, 1) < 60:
        return format_secs(seconds)
    whole = int(round(seconds))
    return f"{whole // 60}m {whole % 60:02d}s"


def format_bytes(count: int) -> str:
    """Decimal GB, one decimal: ``5.7 GB``."""
    return f"{count / 1e9:.1f} GB"


def _utc_iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value) -> "dt.datetime | None":
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# ── D1 / D2: fingerprint and build key ────────────────────────────────────

#: Code points at the tokenizer's known edges (spec D2).
SENTINEL_CODE_POINTS = (
    0x0000, 0x0009, 0x0021, 0x002D, 0x005F, 0x0041, 0x0061, 0x00A0, 0x00AD,
    0x00DF, 0x00E9, 0x0130, 0x01C5, 0x0300, 0x0301, 0x034F, 0x03A9, 0x0E31,
    0x200B, 0x200C, 0x200D, 0x2028, 0x2167, 0x212A, 0x3000, 0xE000, 0xFB01,
    0xFDD0, 0xFFFD, 0xFFFE, 0xFFFF, 0x1D400, 0x1FFFE, 0x10FFFD,
)
_SEGMENTS_PER_ROW = 4096
_TRUNCATION_ROW = "q" * 40000 + " " + "é" * 20000

_FINGERPRINT_MEMO: "dict[str | None, str | None]" = {}
_BUILD_KEY_MEMO: "dict[str | None, str]" = {}
_FTS5_MEMO: "list[bool]" = []


def _segment(cp: int) -> str:
    # c between tokens, then c inside the token q...q (both scanner states).
    c = chr(cp)
    return c + " q" + c + "q "


def _all_scalars() -> Iterable[int]:
    for cp in range(0, 0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        yield cp


def _fts5_table_sql(tokenize_args: "str | None") -> str:
    if tokenize_args is None:
        return "CREATE VIRTUAL TABLE p USING fts5(x)"
    quoted = tokenize_args.replace('"', '""')
    return f'CREATE VIRTUAL TABLE p USING fts5(x, tokenize = "{quoted}")'


def _probe_rows(code_points: Iterable[int], *,
                truncation_row: bool) -> "list[tuple[int, str]]":
    segments = [_segment(cp) for cp in code_points]
    rows = [(i // _SEGMENTS_PER_ROW + 1,
             "".join(segments[i:i + _SEGMENTS_PER_ROW]))
            for i in range(0, len(segments), _SEGMENTS_PER_ROW)]
    if truncation_row:
        rows.append((len(rows) + 1, _TRUNCATION_ROW))
    return rows


def _probe_digest(code_points: Iterable[int], tokenize_args: "str | None",
                  *, truncation_row: bool) -> str:
    """SHA-256 over the probe rows' ``fts5vocab`` instance table in a private
    ``:memory:`` FTS5 table (spec D1): one ``doc<TAB>offset<TAB>term`` line
    per instance, in the vocabulary table's native order, joined by one SQL
    aggregate (no Python-side sort; about half the cost of the old ordered
    loop). The native order is a function of the postings alone, so equal
    tokenizer output gives an equal digest."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(_fts5_table_sql(tokenize_args))
        conn.executemany("INSERT INTO p(rowid, x) VALUES (?, ?)",
                         _probe_rows(code_points,
                                     truncation_row=truncation_row))
        conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(p, 'instance')")
        # The aggregate as raw bytes: identical to its UTF-8 encoding for
        # valid text, and a defective build's output cannot fail a decode.
        conn.text_factory = bytes
        (joined,) = conn.execute(
            "SELECT group_concat(doc || char(9) || offset || char(9) || term, "
            "char(10)) FROM v").fetchone()
        if joined is None:
            joined = b""
        elif isinstance(joined, str):
            joined = joined.encode("utf-8")
        return hashlib.sha256(joined).hexdigest()
    finally:
        conn.close()


def fts5_module_available() -> bool:
    """Can this process's SQLite create an FTS5 table? Answered once."""
    if not _FTS5_MEMO:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            _FTS5_MEMO.append(True)
        except sqlite3.OperationalError:
            _FTS5_MEMO.append(False)
        finally:
            conn.close()
    return _FTS5_MEMO[0]


def fingerprint(*, tokenize_args: "str | None" = None) -> "str | None":
    """The D1 tokenizer fingerprint (about 1.1 s), memoized per process and
    ``tokenize_args``. None when this SQLite has no FTS5."""
    if tokenize_args not in _FINGERPRINT_MEMO:
        if not fts5_module_available():
            _FINGERPRINT_MEMO[tokenize_args] = None
        else:
            _FINGERPRINT_MEMO[tokenize_args] = _probe_digest(
                _all_scalars(), tokenize_args, truncation_row=True)
    return _FINGERPRINT_MEMO[tokenize_args]


def build_key(conn: sqlite3.Connection, *,
              tokenize_args: "str | None" = None) -> str:
    """The D2 build key: SHA-256 hex over the source id, the sorted compile
    options, the tokenizer argument string and the sentinel digest. A few
    milliseconds, memoized per process and ``tokenize_args`` (every connection
    of a process runs the same library)."""
    if tokenize_args not in _BUILD_KEY_MEMO:
        source_id = conn.execute("SELECT sqlite_source_id()").fetchone()[0]
        options = sorted(
            str(row[0]) for row in conn.execute("PRAGMA compile_options"))
        if fts5_module_available():
            sentinel = _probe_digest(SENTINEL_CODE_POINTS, tokenize_args,
                                     truncation_row=False)
        else:
            sentinel = "fts5-unavailable"
        material = "\n".join(
            [str(source_id), *options, tokenize_args or "", sentinel])
        _BUILD_KEY_MEMO[tokenize_args] = hashlib.sha256(
            material.encode("utf-8")).hexdigest()
    return _BUILD_KEY_MEMO[tokenize_args]


# ── D3: stored state ──────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class FtsState:
    verified: bool
    #: build key hex -> verified fingerprint hex, or ``""`` when the
    #: certification did not record one (a fast rank-1 pass; FR1-b)
    build_rows: "dict[str, str]"
    invalidated: "str | None"
    meta: dict
    #: ``verified``; or, unverified: ``legacy`` / ``new`` (no build row and no
    #: invalidation row; by whether any content table holds a row),
    #: ``mismatch`` (a proven inconsistency), ``checking`` (an attempt that
    #: started and did not certify: the attempt record withdraws the
    #: verification, review C1) or ``unlisted-build`` (a write by a build
    #: that was not listed).
    reason: str


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _has_cache_meta(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "cache_meta")


def _any_content(conn: sqlite3.Connection) -> bool:
    for _fts, content, _cols in CONTENT_TABLES:
        try:
            if conn.execute(
                    f"SELECT 1 FROM main.{content} LIMIT 1").fetchone():
                return True
        except sqlite3.OperationalError:
            continue
    return False


def read_state(conn: sqlite3.Connection) -> FtsState:
    if not _has_cache_meta(conn):
        return FtsState(False, {}, None, {}, "new")
    rows = conn.execute(
        "SELECT key, value FROM main.cache_meta WHERE key > ? AND key < ?",
        (BUILD_ROW_PREFIX, _BUILD_ROW_UPPER)).fetchall()
    build_rows = {str(k)[len(BUILD_ROW_PREFIX):]: str(v) for k, v in rows}
    inv = conn.execute("SELECT value FROM main.cache_meta WHERE key=?",
                       (INVALIDATED_KEY,)).fetchone()
    invalidated = None if inv is None else str(inv[0])
    meta_row = conn.execute("SELECT value FROM main.cache_meta WHERE key=?",
                            (META_KEY,)).fetchone()
    meta: dict = {}
    if meta_row is not None:
        try:
            loaded = json.loads(meta_row[0])
            if isinstance(loaded, dict):
                meta = loaded
        except (TypeError, ValueError):
            meta = {}
    if build_rows:
        reason = "verified"
    elif invalidated is None:
        reason = "legacy" if _any_content(conn) else "new"
    elif invalidated.startswith("mismatch "):
        reason = "mismatch"
    elif invalidated.startswith(CHECKING_REASON + " "):
        reason = CHECKING_REASON
    else:
        reason = "unlisted-build"
    return FtsState(bool(build_rows), build_rows, invalidated, meta, reason)


# ── D4: write-time guard ──────────────────────────────────────────────────

def _guard_name(content_table: str, suffix: str) -> str:
    return f"{_GUARD_PREFIX}{content_table}_{suffix}"


def _retraction_condition(key: str) -> str:
    return (f"NOT EXISTS (SELECT 1 FROM cache_meta WHERE key = "
            f"'{BUILD_ROW_PREFIX}{key}') AND {_ANY_BUILD_ROW_SQL}")


def _retraction_body(key: str) -> str:
    return (f"{_DELETE_BUILD_ROWS_SQL}; "
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES("
            f"'{INVALIDATED_KEY}', '{key} ' || {_UTC_NOW_SQL});")


def _check_key(key: str) -> str:
    if not key or any(ch not in "0123456789abcdef" for ch in key):
        raise ValueError(f"build key must be lowercase hex: {key!r}")
    return key


def install_conversation_fts_guard(conn: sqlite3.Connection, *,
                                   key: "str | None" = None) -> "list[str]":
    """Create this connection's TEMP guard triggers on every existing content
    table (idempotent; under a millisecond). Returns the content tables whose
    triggers were (re)created, so a caller can tell a guard that a migration
    dropped with its table from one that survived.

    Nothing is installed without ``main.cache_meta`` (the body would fail
    every write); a store without it has no state to retract.
    """
    if not _has_cache_meta(conn):
        return []
    key = _check_key(key if key is not None else build_key(conn))
    existing = {
        str(name): (str(tbl), str(sql or ""))
        for name, tbl, sql in conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_temp_master "
            "WHERE type='trigger' AND name LIKE ?", (_GUARD_PREFIX + "%",))
    }
    created: "list[str]" = []
    for _fts, content, _cols in CONTENT_TABLES:
        if not _table_exists(conn, content):
            continue
        made = False
        for suffix, event in _GUARD_EVENTS:
            name = _guard_name(content, suffix)
            current = existing.get(name)
            if current is not None:
                if current[0] == content and key in current[1]:
                    continue
                conn.execute(f"DROP TRIGGER IF EXISTS temp.{name}")
            conn.execute(
                f"CREATE TEMP TRIGGER IF NOT EXISTS {name} AFTER {event} ON "
                f"main.{content} WHEN {_retraction_condition(key)} "
                f"BEGIN {_retraction_body(key)} END")
            made = True
        if made:
            created.append(content)
    return created


def drop_conversation_fts_guard(conn: sqlite3.Connection,
                                content_table: str) -> None:
    """Drop this connection's guard triggers on one content table, so a bare
    ``DELETE`` there can take SQLite's truncate fast path."""
    for suffix, _event in _GUARD_EVENTS:
        try:
            conn.execute(
                "DROP TRIGGER IF EXISTS "
                f"temp.{_guard_name(content_table, suffix)}")
        except sqlite3.OperationalError:
            pass


def retract_unless_listed(conn: sqlite3.Connection, *,
                          key: "str | None" = None) -> bool:
    """The guard's body as statements, in the caller's transaction: when this
    build is not listed and any build row exists, delete every build row and
    write the invalidation row. A read decides first, so the common case (a
    listed build, or a store without build rows) takes no write lock and
    writes nothing. Returns whether it retracted."""
    if not _has_cache_meta(conn):
        return False
    key = _check_key(key if key is not None else build_key(conn))
    cond = _retraction_condition(key)
    if not conn.execute(f"SELECT {cond}").fetchone()[0]:
        return False
    # Over-retraction is always safe (it only costs a re-verification), so a
    # concurrent change between the read and these statements needs no
    # second condition.
    conn.execute(_DELETE_BUILD_ROWS_SQL)
    conn.execute(
        "INSERT OR REPLACE INTO cache_meta(key, value) VALUES(?, ? || "
        f"{_UTC_NOW_SQL})", (INVALIDATED_KEY, f"{key} "))
    return True


def retract(conn: sqlite3.Connection, *, reason: str) -> None:
    """Unconditional retraction in its own committed transaction (a proven
    mismatch): every build row deleted, the invalidation row records
    ``<reason> <UTC time>``. The caller must hold no open transaction."""
    if conn.in_transaction:
        raise RuntimeError("retract() needs a connection with no open "
                           "transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_DELETE_BUILD_ROWS_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES(?, ? || "
            f"{_UTC_NOW_SQL})", (INVALIDATED_KEY, f"{reason} "))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def admit_compatible_build(conn: sqlite3.Connection, *, key: str,
                           fp: "str | None") -> bool:
    """Insert this build's row only if a build row with the same fingerprint
    still exists and fewer than ``MAX_BUILD_ROWS`` exist, evaluated inside the
    insert's own write transaction, under a 250 ms busy timeout. On busy, a
    different fingerprint, an empty (unknown) ``fp`` or an open transaction
    it writes nothing: an unknown fingerprint never matches."""
    key = _check_key(key)
    if not fp or conn.in_transaction or not _has_cache_meta(conn):
        return False
    previous = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    conn.execute(f"PRAGMA busy_timeout = {ADMISSION_BUSY_TIMEOUT_MS}")
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO cache_meta(key, value) SELECT ?, ? "
                "WHERE EXISTS (SELECT 1 FROM cache_meta WHERE key > ? "
                "AND key < ? AND value = ?) AND (SELECT count(*) FROM "
                "cache_meta WHERE key > ? AND key < ?) < ?",
                (BUILD_ROW_PREFIX + key, fp, BUILD_ROW_PREFIX,
                 _BUILD_ROW_UPPER, fp, BUILD_ROW_PREFIX, _BUILD_ROW_UPPER,
                 MAX_BUILD_ROWS))
            written = cur.rowcount > 0
            conn.execute("COMMIT")
            return written
        except sqlite3.OperationalError:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            return False
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute(f"PRAGMA busy_timeout = {previous}")


def prepare_writer_connection(conn: sqlite3.Connection) -> None:
    """The opener's admission and guard install (spec D4): when a build row
    with a recorded fingerprint exists without this build's row, compute the
    fingerprint and try the conditional admission; then install the guard.
    Computes and writes nothing on a store without build rows or whose build
    rows are all ``""`` (unknown): there the guard retracts at this build's
    first write and the next automatic step re-verifies."""
    if not _has_cache_meta(conn):
        return
    key = build_key(conn)
    if not conn.in_transaction:
        listed = conn.execute(
            "SELECT 1 FROM main.cache_meta WHERE key=?",
            (BUILD_ROW_PREFIX + key,)).fetchone() is not None
        if not listed and conn.execute(
                f"SELECT {_ANY_FINGERPRINTED_BUILD_ROW_SQL}").fetchone()[0]:
            admit_compatible_build(
                conn, key=key,
                fp=fingerprint(tokenize_args=CONVERSATION_FTS_TOKENIZE_ARGS))
    install_conversation_fts_guard(conn, key=key)


@contextlib.contextmanager
def write_locked(conn: sqlite3.Connection):
    """Run the body under this connection's write lock, taken before its
    first read (review F8): ``retract_unless_listed`` decides by a read, so
    outside a write transaction another build's certification could commit
    between that read and the caller's write. Inside an open transaction it
    adds nothing. Otherwise it opens one with ``BEGIN IMMEDIATE``; under
    Python's legacy isolation it is left open for the caller to commit, as
    the body's first write would have left its implicit transaction, and
    under autocommit (``isolation_level`` None) it commits after the body.
    A body that raises rolls back the transaction this opened."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        _rollback(conn)
        raise
    if conn.isolation_level is None and conn.in_transaction:
        conn.execute("COMMIT")


def conversation_fts_command(conn: sqlite3.Connection, table: str,
                             command: str) -> None:
    """A direct FTS command (``'rebuild'``, ``'delete-all'``) bypasses the
    content-table triggers, so the guard's retraction for this build runs
    first, in the same write transaction as the command."""
    with write_locked(conn):
        retract_unless_listed(conn)
        conn.execute(f"INSERT INTO {table}({table}) VALUES(?)", (command,))


# ── D5: the verification and repair step ──────────────────────────────────

MODES = ("automatic", "force", "check")
#: Corpus work that clears or refills the indexes; the step never starts,
#: races or certifies around it (spec D5).
DEFERRAL_MARKERS = (
    "conversation_rebuild_claude_pending",
    "conversation_rebuild_codex_pending",
    "conversation_search_split_pending",
    "conversation_title_fts_backfill_pending",
)
FTS5_UNAVAILABLE_KEY = "fts5_unavailable"
CODEX_FTS_UNAVAILABLE_KEY = "codex_fts_unavailable"
#: The backstop (D6) and the cooldown between automatic attempts.
VERIFY_INTERVAL = dt.timedelta(days=30)
ATTEMPT_COOLDOWN = dt.timedelta(hours=24)
DISK_HEADROOM_BYTES = 256 * 1024 * 1024
#: ``NOTICE_CHECKING`` promises minutes; below this store size the check
#: takes seconds at most and the notice is withheld.
NOTICE_CHECKING_MIN_STORE_BYTES = 1024 ** 3
_SHADOW_SUFFIXES = ("_data", "_idx", "_docsize", "_config", "_content")


@dataclasses.dataclass(frozen=True)
class IndexResult:
    name: str
    #: ``consistent`` / ``mismatch`` / ``error``
    before: str
    #: ``none`` / ``rebuilt`` / ``refused``
    action: str
    #: ``consistent`` / ``mismatch`` / ``error`` (a rebuilt index is always
    #: re-checked, review F7)
    after: str
    #: The rebuild's seconds when ``action == "rebuilt"``, else the first
    #: rank-1 check's seconds.
    seconds: float
    #: The SQLite error behind the last ``error`` status (``after``, or
    #: ``before`` when nothing ran later); None otherwise. Text output only.
    error: "str | None" = None


@dataclasses.dataclass(frozen=True)
class VerifyResult:
    #: ``verified`` / ``mismatch`` / ``repair-failed`` / ``disk`` / ``busy`` /
    #: ``deferred`` / ``unavailable`` / ``skipped``
    outcome: str
    indexes: "tuple[IndexResult, ...]"
    deferred_by: "str | None"
    sqlite_version: str
    fingerprint: "str | None"
    state: FtsState
    #: The largest requirement the disk guard evaluated in this run and the
    #: free space measured at that evaluation; None when the guard never ran.
    need_bytes: "int | None" = None
    avail_bytes: "int | None" = None


class StoreOpenError(Exception):
    """``verify_conversation_fts`` could not open conversations.db (the
    opener's error is ``__cause__``). A failure after the store opened is an
    outcome, never this."""


def automatically_eligible(state: FtsState, *, now: dt.datetime) -> bool:
    """Unverified, or verified more than 30 days ago (the backstop); and the
    last attempt more than 24 hours ago. ``force``/``check`` ignore this."""
    verified_at = _parse_utc(state.meta.get("verifiedAt"))
    due = (not state.verified or verified_at is None
           or now - verified_at > VERIFY_INTERVAL)
    last = _parse_utc(state.meta.get("lastAttemptAt"))
    return due and (last is None or now - last > ATTEMPT_COOLDOWN)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def _store_path() -> Path:
    return Path(_cctally_core.CONVERSATIONS_DB_PATH)


def _volume_free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path.parent).free)


def _store_bytes(path: Path) -> int:
    """The store's main-file size; 0 when it cannot be read."""
    try:
        return int(os.stat(path).st_size)
    except OSError:
        return 0


#: The clock the rank-1 statements are timed with (patched by tests).
_rank1_clock = time.monotonic


def _plan(conn: sqlite3.Connection):
    """``(blocked_outcome | None, required content-table triples,
    deferred_by | None)``."""
    keys: "set[str]" = set()
    if _has_cache_meta(conn):
        wanted = (*DEFERRAL_MARKERS, FTS5_UNAVAILABLE_KEY,
                  CODEX_FTS_UNAVAILABLE_KEY)
        marks = ",".join("?" for _ in wanted)
        keys = {str(r[0]) for r in conn.execute(
            f"SELECT key FROM main.cache_meta WHERE key IN ({marks})",
            wanted)}
    if not fts5_module_available() or FTS5_UNAVAILABLE_KEY in keys:
        return "unavailable", (), None
    required = tuple(
        entry for entry in CONTENT_TABLES
        if not (entry[0] == "codex_conversation_fts"
                and CODEX_FTS_UNAVAILABLE_KEY in keys))
    for marker in DEFERRAL_MARKERS:
        if marker in keys:
            return "deferred", required, marker
    for fts, _content, _cols in required:
        if not _table_exists(conn, fts):
            return "deferred", required, fts
    return None, required, None


class _StepLocks:
    """Maintenance EX|NB, then Claude EX|NB, then Codex EX|NB, then the
    maintenance flock downgraded to SH: the retention prune's order
    (``_maybe_prune_conversation_retention``). ``held`` is False on any
    contention; nothing else is taken then."""

    def __init__(self) -> None:
        self._handles: "list" = []
        self.held = False

    def __enter__(self) -> "_StepLocks":
        paths = (_cctally_core.CONVERSATIONS_LOCK_MAINTENANCE_PATH,
                 _cctally_core.CONVERSATIONS_LOCK_PATH,
                 _cctally_core.CONVERSATIONS_LOCK_CODEX_PATH)
        try:
            for path in paths:
                fh = open(path, "w")
                self._handles.append(fh)
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    # Held elsewhere, or a flock error: either way contention.
                    self._release()
                    return self
            try:
                fcntl.flock(self._handles[0], fcntl.LOCK_SH)
            except OSError:
                # A failed downgrade must not keep the maintenance flock
                # exclusive for the whole check: release it all, contention.
                self._release()
                return self
            self.held = True
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc) -> None:
        self._release()

    def _release(self) -> None:
        for fh in reversed(self._handles):
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
        self._handles = []


def _begin(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _write_meta_row(conn: sqlite3.Connection, meta: dict) -> None:
    doc = {"schemaVersion": META_SCHEMA_VERSION}
    doc.update({k: v for k, v in meta.items() if k != "schemaVersion"})
    conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) VALUES(?, ?)",
                 (META_KEY, json.dumps(doc, sort_keys=False)))


def _record_attempt(conn: sqlite3.Connection, meta: dict) -> None:
    """The attempt record (review C1): ``lastAttemptAt`` in the metadata,
    and in the same committed transaction every build row deleted and the
    invalidation row ``checking <UTC time>``. From here until a
    certification commits the store is unverified, so an attempt that is
    interrupted, busy or fails, or whose later writes fail, leaves it
    eligible again after the cooldown; certification (one transaction with
    the rank-1 checks it backs) is the only way back to verified."""
    _begin(conn)
    try:
        _write_meta_row(conn, meta)
        conn.execute(_DELETE_BUILD_ROWS_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES(?, ? || "
            f"{_UTC_NOW_SQL})", (INVALIDATED_KEY, f"{CHECKING_REASON} "))
        conn.execute("COMMIT")
    except BaseException:
        _rollback(conn)
        raise


def _commit_meta(conn: sqlite3.Connection, meta: dict) -> None:
    """The metadata document in its own committed transaction."""
    _begin(conn)
    try:
        _write_meta_row(conn, meta)
        conn.execute("COMMIT")
    except BaseException:
        _rollback(conn)
        raise


def _check_indexes(conn: sqlite3.Connection, required) -> "dict[str, tuple]":
    """Rank 1 over every required index inside the caller's write
    transaction. ``name -> (status, seconds, error)``; ``OperationalError``
    is ``error`` (with its message), any other ``DatabaseError`` is
    ``mismatch``."""
    results: "dict[str, tuple]" = {}
    for fts, _content, _cols in required:
        if not conn.in_transaction:
            # A failed statement that rolled the transaction back: the
            # remaining indexes still get checked, in a new one.
            try:
                _begin(conn)
            except sqlite3.OperationalError as exc:
                results[fts] = ("error", 0.0, str(exc))
                continue
        t0 = _rank1_clock()
        error = None
        try:
            conn.execute(f"INSERT INTO {fts}({fts}, rank) "
                         "VALUES('integrity-check', 1)")
            status = "consistent"
        except sqlite3.OperationalError as exc:
            status, error = "error", str(exc)
        except sqlite3.DatabaseError:
            status = "mismatch"
        results[fts] = (status, _rank1_clock() - t0, error)
    return results


def _recheck(conn: sqlite3.Connection, tables) -> "dict[str, tuple]":
    """Rank 1 over ``tables`` alone, in a write transaction that is rolled
    back: a rebuilt index's state when certification cannot happen anyway
    (another index was refused or failed), so ``after`` is never
    ``unchecked`` (review F7)."""
    entries = [entry for entry in CONTENT_TABLES if entry[0] in tables]
    try:
        _begin(conn)
    except sqlite3.OperationalError as exc:
        return {fts: ("error", 0.0, str(exc)) for fts, _c, _k in entries}
    try:
        return _check_indexes(conn, entries)
    finally:
        _rollback(conn)


def _certify_pass(conn: sqlite3.Connection, required, *, key: str,
                  meta: dict, now: dt.datetime,
                  rebuilt: "dict[str, float]"
                  ) -> "tuple[dict, bool, str | None]":
    """One ``BEGIN IMMEDIATE`` transaction: rank 1 over every required index;
    when all pass, the same transaction replaces every build row with this
    build's, clears the invalidation row and updates the metadata, then
    commits. Otherwise it rolls back and writes nothing.

    The build row's value is this build's fingerprint only when the rank-1
    statements took ``FINGERPRINT_MIN_RANK1_SECONDS`` or more in total
    (computed here, on its own ``:memory:`` connection, while the write
    transaction is open); otherwise ``""`` (unknown). Returns ``(results,
    certified, fingerprint | None)``."""
    try:
        _begin(conn)
    except sqlite3.OperationalError as exc:
        return ({fts: ("error", 0.0, str(exc)) for fts, _c, _k in required},
                False, None)
    try:
        results = _check_indexes(conn, required)
        if not (conn.in_transaction and all(
                status == "consistent" for status, _s, _e in results.values())):
            _rollback(conn)
            return results, False, None
        fp: "str | None" = None
        if sum(secs for _status, secs, _e in results.values()) \
                >= FINGERPRINT_MIN_RANK1_SECONDS:
            fp = fingerprint(tokenize_args=CONVERSATION_FTS_TOKENIZE_ARGS)
        conn.execute(_DELETE_BUILD_ROWS_SQL)
        conn.execute("INSERT INTO cache_meta(key, value) VALUES(?, ?)",
                     (BUILD_ROW_PREFIX + key, fp or ""))
        conn.execute("DELETE FROM cache_meta WHERE key=?", (INVALIDATED_KEY,))
        doc = dict(meta)
        doc.update({
            "verifiedAt": _utc_iso(now),
            "fingerprint": fp,
            "sqliteVersion": sqlite3.sqlite_version,
            "lastResult": {
                fts: ({"result": "rebuilt",
                       "seconds": round(rebuilt[fts] + secs, 3)}
                      if fts in rebuilt else
                      {"result": status, "seconds": round(secs, 3)})
                for fts, (status, secs, _e) in results.items()},
            "reason": None,
        })
        _write_meta_row(conn, doc)
        conn.execute("COMMIT")
        return results, True, fp
    except BaseException:
        _rollback(conn)
        raise


def _index_bytes(conn: sqlite3.Connection, fts: str) -> int:
    """Current size of an index's shadow tables from ``dbstat``; 0 without
    ``dbstat``."""
    total = 0
    for suffix in _SHADOW_SUFFIXES:
        name = fts + suffix
        if not _table_exists(conn, name):
            continue
        try:
            row = conn.execute(
                "SELECT coalesce(sum(pgsize), 0) FROM dbstat "
                "WHERE name = ? AND aggregate = TRUE", (name,)).fetchone()
        except sqlite3.OperationalError:
            try:
                row = conn.execute(
                    "SELECT coalesce(sum(pgsize), 0) FROM dbstat "
                    "WHERE name = ?", (name,)).fetchone()
            except sqlite3.OperationalError:
                return 0
        total += int(row[0] or 0)
    return total


def _source_bytes(conn: sqlite3.Connection, content: str,
                  cols: "tuple[str, ...]") -> int:
    """Indexed source bytes (Σ ``octet_length`` of the indexed columns);
    the store file's size without ``octet_length`` (SQLite < 3.43.0)."""
    fallback = _store_path().stat().st_size
    if sqlite3.sqlite_version_info < (3, 43, 0):
        return fallback
    expr = " + ".join(f"coalesce(octet_length({c}), 0)" for c in cols)
    try:
        row = conn.execute(
            f"SELECT coalesce(sum({expr}), 0) FROM main.{content}").fetchone()
    except sqlite3.OperationalError:
        return fallback
    return int(row[0] or 0)


def disk_requirement(conn: sqlite3.Connection, fts: str) -> int:
    """Free bytes a rebuild of ``fts`` requires on the store's volume: twice
    the larger of its current size and its indexed source bytes, plus 256 MiB
    (the rebuild is one transaction, so the WAL holds the whole new index)."""
    for name, content, cols in CONTENT_TABLES:
        if name == fts:
            return (2 * max(_index_bytes(conn, fts),
                            _source_bytes(conn, content, cols))
                    + DISK_HEADROOM_BYTES)
    raise KeyError(fts)


def verify_conversation_fts(
        *, mode: str,
        now: "Callable[[], dt.datetime] | None" = None,
        free_bytes: "Callable[[Path], int] | None" = None,
        notice: "Callable[[str], None] | None" = None) -> VerifyResult:
    """Verify the conversation FTS indexes against their content with rank 1
    and repair the ones that disagree (spec D5).

    ``mode`` is ``automatic`` (the dashboard and ``cache-sync``: runs only
    when ``automatically_eligible``), ``force`` (always checks, repairs) or
    ``check`` (always checks, never repairs). ``notice`` receives the
    automatic notice lines; the command passes none. Raises
    ``StoreOpenError`` when the store cannot be opened; a lock or I/O
    error on a write after it opened is the outcome ``busy`` (review F6).
    """
    # #787 spec 5.1: owner entry, guarded in its own frame.
    _cctally_core.require_capability("MAINTENANCE", entry="verify_conversation_fts")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    clock = now or _utcnow
    free = free_bytes or _volume_free_bytes
    import _cctally_cache  # lazy: _cctally_cache imports this module
    try:
        conn = _cctally_cache.open_conversations_db(
            attach_cache=False, run_derivations=False)
    except (sqlite3.DatabaseError, OSError) as exc:
        raise StoreOpenError(str(exc)) from exc
    try:
        if conn.in_transaction:
            conn.commit()
        # Every transaction of the step is explicit; Python's implicit
        # transaction handling must not commit between rank 1 and the
        # certification it backs.
        conn.isolation_level = None
        return _run_step(conn, mode, clock, free, notice)
    finally:
        conn.close()


def _run_step(conn, mode, clock, free, notice) -> VerifyResult:
    def say(line: str) -> None:
        if notice is not None:
            notice(line)

    guard: "dict[str, int]" = {}

    def result(outcome, indexes=(), *, deferred_by=None, fp=None
               ) -> VerifyResult:
        return VerifyResult(outcome, tuple(indexes), deferred_by,
                            sqlite3.sqlite_version, fp, read_state(conn),
                            guard.get("need"), guard.get("avail"))

    blocked, required, deferred_by = _plan(conn)
    if blocked:
        return result(blocked, deferred_by=deferred_by)
    if mode == "automatic" and not automatically_eligible(
            read_state(conn), now=clock()):
        return result("skipped")

    with _StepLocks() as locks:
        if not locks.held:
            return result("busy")
        # Recheck under the locks: markers, required indexes, eligibility.
        blocked, required, deferred_by = _plan(conn)
        if blocked:
            return result(blocked, deferred_by=deferred_by)
        state = read_state(conn)
        started = clock()
        if mode == "automatic" and not automatically_eligible(
                state, now=started):
            return result("skipped")
        meta = dict(state.meta)
        meta["lastAttemptAt"] = _utc_iso(started)
        try:
            # Committed before the first check, so an interrupted attempt
            # still starts the cooldown; it also withdraws the verification
            # until this attempt certifies (review C1).
            _record_attempt(conn, meta)
        except sqlite3.OperationalError:
            return result("busy")

        def unchanged(first) -> "list[IndexResult]":
            return [IndexResult(fts, status, "none", status, secs, error)
                    for fts, (status, secs, error) in first.items()]

        def finish(outcome, rows, reason, *, fp=None) -> VerifyResult:
            doc = dict(meta)
            doc["lastResult"] = {
                ix.name: {"result": ("rebuilt" if ix.action == "rebuilt"
                                     else ix.before),
                          "seconds": round(ix.seconds, 3)}
                for ix in rows}
            doc["reason"] = reason
            try:
                _commit_meta(conn, doc)
            except sqlite3.OperationalError:
                # Another writer holds the store: the history document is
                # not written and the attempt is reported as contention.
                return result("busy", rows, fp=fp)
            return result(outcome, rows, fp=fp)

        key = build_key(conn, tokenize_args=CONVERSATION_FTS_TOKENIZE_ARGS)
        if mode == "automatic" and (_store_bytes(_store_path())
                                    >= NOTICE_CHECKING_MIN_STORE_BYTES):
            say(NOTICE_CHECKING)
        try:
            first, certified, fp = _certify_pass(conn, required, key=key,
                                                 meta=meta, now=started,
                                                 rebuilt={})
        except sqlite3.OperationalError:
            return result("busy")
        if certified:
            return result("verified", unchanged(first), fp=fp)

        mismatched = [fts for fts, (status, _s, _e) in first.items()
                      if status == "mismatch"]
        if not mismatched:
            # Only errors (busy, locked): nothing proven either way.
            return finish("busy", unchanged(first), "error", fp=fp)

        # The attempt record already withdrew the verification (review C1);
        # this only names the reason, so its failure changes nothing.
        try:
            retract(conn, reason="mismatch")
        except sqlite3.OperationalError:
            pass
        if mode == "check":
            return finish("mismatch", unchanged(first), "mismatch", fp=fp)

        rebuilt: "dict[str, float]" = {}
        refused: "dict[str, tuple[int, int]]" = {}
        failed: "dict[str, str]" = {}
        for fts in mismatched:
            need = disk_requirement(conn, fts)
            avail = int(free(_store_path()))
            if "need" not in guard or need > guard["need"]:
                guard.update(need=need, avail=avail)
            if avail < need:
                refused[fts] = (need, avail)
                continue
            say(NOTICE_REBUILDING.format(name=fts))
            t0 = time.monotonic()
            try:
                _begin(conn)
                conversation_fts_command(conn, fts, "rebuild")
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                _rollback(conn)
                failed[fts] = str(exc)
                continue
            rebuilt[fts] = time.monotonic() - t0
            say(NOTICE_REBUILT.format(
                name=fts, dur=format_duration(rebuilt[fts])))

        def notify(rows) -> None:
            if mode != "automatic":
                return
            for ix in rows:
                if ix.action == "refused":
                    need, avail = refused[ix.name]
                    say(NOTICE_NOT_REBUILT_DISK.format(
                        name=ix.name, need=format_bytes(need),
                        avail=format_bytes(avail)))
                elif ix.name in mismatched and ix.after != "consistent":
                    say(NOTICE_REPAIR_FAILED.format(name=ix.name))

        if refused or failed:
            again = _recheck(conn, rebuilt) if rebuilt else {}
            rows = []
            for fts, (status, secs, error) in first.items():
                if fts in rebuilt:
                    after, _s, after_error = again[fts]
                    rows.append(IndexResult(fts, status, "rebuilt", after,
                                            rebuilt[fts], after_error))
                elif fts in refused:
                    rows.append(IndexResult(fts, status, "refused",
                                            "mismatch", secs))
                elif fts in failed:
                    rows.append(IndexResult(fts, status, "none", "error",
                                            secs, failed[fts]))
                else:
                    rows.append(IndexResult(fts, status, "none", status,
                                            secs, error))
            notify(rows)
            if refused:
                return finish("disk", rows, "disk", fp=fp)
            return finish("repair-failed", rows, "repair-failed", fp=fp)

        try:
            second, certified, fp = _certify_pass(conn, required, key=key,
                                                  meta=meta, now=clock(),
                                                  rebuilt=rebuilt)
        except sqlite3.OperationalError as exc:
            return result("busy", [
                IndexResult(fts, status,
                            "rebuilt" if fts in rebuilt else "none",
                            "error", rebuilt.get(fts, secs), str(exc))
                for fts, (status, secs, _error) in first.items()])
        rows = [
            IndexResult(fts, status,
                        "rebuilt" if fts in rebuilt else "none",
                        second[fts][0],
                        rebuilt.get(fts, secs), second[fts][2])
            for fts, (status, secs, _error) in first.items()]
        if certified:
            return result("verified", rows, fp=fp)
        notify(rows)
        return finish("repair-failed", rows, "repair-failed", fp=fp)
