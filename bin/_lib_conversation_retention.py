"""#313 P3 + #901: conversation-transcript retention — paced expiry deletion
and paced reclaim.

Prunes ONLY the re-derivable transcript rows:
  * Claude: ``conversation_messages`` + their ``conversation_file_touches`` /
    ``conversation_ai_titles`` / ``conversation_sessions`` browse-rollup rows.
  * Codex: ``codex_conversation_events`` AND the #294 S6 normalized derived rows
    those events feed — ``codex_conversation_messages``,
    ``codex_conversation_file_touches``, and ``codex_conversation_rollups`` (plus
    their FTS postings) — so a prune never strands orphaned browse/search state.

It NEVER touches cost/usage rows (``session_entries`` / ``codex_session_entries``),
the delta-resume cursors (``*_session_files``), or ``codex_conversation_threads``
(F5 — pruning threads disables ``source_analytics``'s whole range via
``_require_joined_metadata``, since that range LEFT JOINs threads for cwd/git).

Eligibility is decided from the AUTHORITATIVE base tables, never the possibly
stale ``conversation_sessions`` rollup (F6): a group is prunable iff it has at
least one dated row and NO row at/after the cutoff. Rows whose timestamps are
entirely NULL are treated conservatively and never pruned in isolation (F12).
NULL identity (``session_id`` / ``conversation_key``) falls back to grouping by
``source_path`` so malformed rows stay bounded rather than orphaned-unbounded
(F12). The FTS5 indexes are external-content and maintained by AFTER-DELETE
triggers, which stay consistent because whole groups are deleted.

#901 (spec §5.4; operator decisions Q2, Q4, Q5, Q7) puts every maintenance
write on ONE durable allowance:

* 4 MiB/min with at most one minute (4 MiB) of credit, accrued by wall clock
  from the durable ``as_of`` and capped, so a restart never refills credit and
  never bypasses the cadence. The balance and the 25-bucket hourly charge
  ledger live in the extended #780 record (``RECLAIM_PENDING_KEY``).
* An OPERATION is one expired group's deletion or one reclaim chunk. It starts
  only from a non-negative balance and is charged EXACTLY its reservation,
  written in the operation's own ``BEGIN IMMEDIATE`` transaction together with
  its mutation: a crash before COMMIT leaves neither, after it both. Overshoot
  becomes debt and nothing runs until it is repaid. There is no reconciliation
  and no deferred-copy record.
* Reclaim never checkpoints: its connection runs ``wal_autocheckpoint = 0`` with
  SQLite's no-checkpoint-on-close setting on (plus ``cache_spill = OFF`` and an
  in-memory temp store), and when those settings cannot be applied the attempt
  runs nothing and records why. It starts only when free pages exceed 2 GiB
  AND 20% of the file, stops at 1 GiB OR 10%, and runs at most once a minute
  under the 2 s deadline. Each chunk (Q9) is planned inside its own
  ``BEGIN IMMEDIATE`` by `_lib_reclaim_planner` in a helper subprocess, runs at
  most 16 ``incremental_vacuum(1)`` steps, and is charged
  (|ID| + UNID + 1) x (2P + 24) + F_r from that plan; a planner refusal rolls
  back with no vacuum and no charge and is reported in memory only.
* Deletion keeps whole-group atomicity and SQLite's automatic PASSIVE checkpoint
  at COMMIT, on a per-visit connection with a 256 MiB cache and an in-memory
  temp store, so a group is deleted spill-free; above 60,000 rows the cache cap
  stays and the statement journal uses the file temp store (Q5). Its charge
  (Q8) is F + A x rows + B x M_cap, M_cap the pointer-map worst case of the
  file's geometry read inside the transaction. A visit stops between groups
  when the balance is negative and persists a continuation cutoff; the daily
  stamp is written only when both providers finish.
* The #780 record is one fixed-size cell (`RECLAIM_RECORD_LENGTH`): every
  writer stores exactly L bytes and none deletes the row, so a chunk's state
  write overwrites it in place and dirties one page.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

import _cctally_core
import _lib_reclaim_planner as _planner
import _lib_sqlite_close
import _lib_write_budget as _budget
import _lib_write_io

UTC = dt.timezone.utc
KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB

# cache_meta throttle key (framework-untracked KV — NO schema migration, F7).
_RETENTION_LAST_PRUNE_KEY = "conversation_retention_last_prune_at"
_RETENTION_THROTTLE_SECONDS = 24 * 60 * 60

#: The extended #780 record: balance, ledger, episode and progress. The key is
#: unchanged so an older binary still reads its legacy fields.
RECLAIM_PENDING_KEY = "conversation_retention_reclaim_pending"
RETENTION_POLICY_VERSION = 1
_LEGACY_RECORD_KEYS = ("freelist_count", "unreclaimed_bytes", "attempted_at",
                       "made_progress", "deadline_hit")

# ── Reservation constants: CANDIDATES (spec §5.4, Q8 and Q9) ──────────────
# Fixed by the §6.3 R-cov calibration: each calibrated part must be at least
# 1.25 x its measured maximum, rounded up to a KiB. A candidate that falls
# short is raised, never left below coverage. Calibration edits THIS block
# only. The analytical page terms (B's 2P + 24 per pointer-map page, and a
# reclaim chunk's planned pages) are computed, never calibrated downwards.
#: The formula version every operation record carries; a record with
#: another version is not recomputed (`recompute_charge`).
RESERVATION_VERSION = 2
#: F: a deletion's fixed part per operation (the WAL header is inside it).
DELETION_FIXED_BYTES = 512 * KiB
#: A: per deleted row, and above `DELETION_FALLBACK_ROW_THRESHOLD` (Q5).
DELETION_PER_ROW_BYTES = 15 * KiB
DELETION_PER_ROW_FALLBACK_BYTES = 50 * KiB
#: B = ceil(5/4 x (2P + 24) / 1 KiB) KiB per pointer-map page of M_cap, from
#: the store's own page size (11 KiB at 4 KiB pages).
POINTER_MAP_FACTOR_NUMERATOR = 5
POINTER_MAP_FACTOR_DENOMINATOR = 4
#: F_r: a reclaim chunk's fixed part (WAL header and framing; Q9). The page
#: term (|ID| + UNID + 1) x (2P + 24) comes from the chunk's own plan.
RECLAIM_FIXED_BYTES = 64 * KiB
# ──────────────────────────────────────────────────────────────────────────

#: I4's deletion write bounds (spec §3; limits on the writes themselves,
#: never calibrated). N_max's growth term is built from the first two.
I4_DELETION_FIXED_BYTES = 512 * KiB
I4_DELETION_PER_ROW_BYTES = 12 * KiB
I4_FALLBACK_PER_ROW_BYTES = 40 * KiB

#: Q5: a group above this many rows keeps the cache cap but uses the file
#: temp store for its statement journal, bounded at 40 KiB per deleted row.
DELETION_FALLBACK_ROW_THRESHOLD = 60_000
#: `PRAGMA cache_size = -262144`: a 256 MiB cap, used only as pages dirty.
DELETION_CACHE_SIZE_KIB = 262_144

RECLAIM_START_BYTES = 2 * GiB
RECLAIM_START_RATIO = 0.20
RECLAIM_STOP_BYTES = 1 * GiB
RECLAIM_STOP_RATIO = 0.10
RECLAIM_MAX_CHUNK_PAGES = 16
RECLAIM_ATTEMPT_INTERVAL_SECONDS = 60
#: The secondary deadline between chunks; a chunk itself is not preemptible.
RECLAIM_DEADLINE_SECONDS = 2.0
#: Doctor FAIL and rebuild refusal, judged on the OBSERVED freelist; reaching
#: it never accelerates automatic writes.
RECLAIM_CEILING_BYTES = 16 * GiB
#: A chunk's whole reservation target (Q9): the plan grows one step at a
#: time while it stays at most this; a single step over it runs alone.
RECLAIM_CHUNK_BUDGET_BYTES = 4 * MiB
#: Every typed reason a reclaim attempt can skip with (spec §5.4 "Refusal"):
#: the connection's settings, the record's precondition, the runtime SQLite,
#: the planner's own refusals and its helper, and an unexpected SQLite error.
RECLAIM_SKIP_REASONS = (
    "no_checkpoint_on_close_unavailable", "connection_settings_unavailable",
    "sqlite_error", "state_record_unnormalized", "state_record_oversize",
    "helper_failed", "helper_timeout") + _planner.REFUSAL_REASONS
#: The planner helper: a fresh interpreter (never a fork of this process),
#: so no raw descriptor of the store is ever opened here. It imports only
#: the standard library.
_PLANNER_COMMAND = [sys.executable,
                    os.path.join(os.path.dirname(os.path.abspath(
                        _planner.__file__)), "_lib_reclaim_planner.py")]
#: The helper's time budget when a chunk runs outside an attempt; inside
#: one it is the rest of the attempt's 2 s secondary deadline.
_PLANNER_DEADLINE_SECONDS = 1.5
_PLANNER_MIN_DEADLINE_SECONDS = 0.05
#: Interpreter start-up allowance on top of the helper's own budget.
_PLANNER_STARTUP_SLACK_SECONDS = 1.0
#: The helper's read budget in bytes (its time budget is the deadline).
_PLANNER_READ_BUDGET_BYTES = 256 * MiB

#: Mirror of `_cctally_store.STORE_POLICY["conversations"]`'s connection
#: settings; `tests/test_retention_pacing.py` pins the equality.
_CONNECTION_BUSY_TIMEOUT_MS = 15_000
_CONNECTION_SYNCHRONOUS = "NORMAL"
_CONNECTION_JOURNAL_SIZE_LIMIT = 128 * MiB


@dataclass(frozen=True)
class PruneStats:
    """Counts from one prune pass. ``complete`` is False when a paced visit
    stopped before every expired group was deleted (a continuation)."""

    claude_sessions: int = 0
    claude_messages: int = 0
    codex_conversations: int = 0
    codex_events: int = 0
    complete: bool = True

    @property
    def total_rows(self) -> int:
        return self.claude_messages + self.codex_events


def _iso(value: "dt.datetime | None") -> "str | None":
    return None if value is None else _budget.iso_z(value)


def _ceil_second(value: dt.datetime) -> dt.datetime:
    """``value`` rounded UP to a whole UTC second (idempotent)."""
    value = value.astimezone(UTC)
    if value.microsecond:
        value = value.replace(microsecond=0) + dt.timedelta(seconds=1)
    return value


def _iso_ceil(value: "dt.datetime | None") -> "str | None":
    """A pacing instant at whole-second precision, rounded UP (901-RW-001).

    The record's instants are 20-character whole seconds. Truncating the
    allowance's ``as_of`` let every reload of the record at the same
    fractional visit instant accrue the dropped fraction again (I4), and
    truncating ``next_attempt_at`` moved the reclaim cadence earlier. Rounding
    up can only withhold accrual and delay a deadline."""
    return None if value is None else _budget.iso_z(_ceil_second(value))


def _parse_utc(value) -> "dt.datetime | None":
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _int_or_none(value) -> "int | None":
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _cutoff_iso(cutoff_utc: dt.datetime) -> str:
    """Whole-second UTC ``...Z`` boundary for lex comparison against the stored
    ``...Z`` timestamps (second-granular; immaterial for a multi-day window)."""
    return cutoff_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── pacing state (the extended #780 record) ───────────────────────────────

@dataclass(frozen=True)
class PacingState:
    balance_bytes: int
    as_of: dt.datetime
    ledger: tuple = ()
    eligible: bool = False
    next_attempt_at: "dt.datetime | None" = None
    continuation_cutoff: "str | None" = None
    last_failure: "dict | None" = None
    progress: "dict | None" = None
    op_seq: int = 0
    legacy: dict = dataclasses.field(default_factory=dict)

    @classmethod
    def from_record(cls, record, now_utc: dt.datetime) -> "PacingState":
        """A legacy or absent record reads as full credit and no charges."""
        now = now_utc.astimezone(UTC)
        legacy = {}
        if isinstance(record, dict):
            legacy = {k: record[k] for k in _LEGACY_RECORD_KEYS if k in record}
        if not isinstance(record, dict) or record.get("policy_version") is None:
            return cls(balance_bytes=_budget.MAINTENANCE_CREDIT_BYTES,
                       as_of=now, legacy=legacy)
        balance = _int_or_none(record.get("balance_bytes"))
        as_of = _parse_utc(record.get("as_of"))
        if balance is None or as_of is None:
            balance, as_of = _budget.MAINTENANCE_CREDIT_BYTES, now
        failure = record.get("last_failure")
        progress = record.get("progress")
        cutoff = record.get("continuation_cutoff")
        return cls(
            balance_bytes=min(balance, _budget.MAINTENANCE_CREDIT_BYTES),
            as_of=as_of,
            ledger=tuple(_budget.serialize_ledger(
                _budget.parse_ledger(record.get("ledger")))),
            eligible=record.get("eligible") is True,
            next_attempt_at=_parse_utc(record.get("next_attempt_at")),
            continuation_cutoff=cutoff if isinstance(cutoff, str) else None,
            last_failure=failure if isinstance(failure, dict) else None,
            progress=progress if isinstance(progress, dict) else None,
            op_seq=max(0, _int_or_none(record.get("op_seq")) or 0),
            legacy=legacy,
        )

    def available(self, now_utc: dt.datetime) -> int:
        elapsed = max(0.0, (now_utc.astimezone(UTC) - self.as_of).total_seconds())
        accrued = self.balance_bytes + int(
            _budget.MAINTENANCE_RATE_BYTES_PER_MINUTE * elapsed / 60.0)
        return min(_budget.MAINTENANCE_CREDIT_BYTES, accrued)

    def charge(self, reservation: int, now_utc: dt.datetime, *,
               started_utc: "dt.datetime | None" = None) -> "PacingState":
        """Subtract the reservation at ``now_utc`` (the accrual instant) and
        add it to the bucket of the hour the operation started,
        ``started_utc`` (PR-13 (a); the accrual instant when omitted)."""
        now = now_utc.astimezone(UTC)
        start = now if started_utc is None else started_utc.astimezone(UTC)
        return dataclasses.replace(
            self,
            balance_bytes=self.available(now) - int(reservation),
            as_of=max(now, self.as_of),
            ledger=tuple(_budget.ledger_add(
                list(self.ledger), at_utc=start,
                charge_bytes=int(reservation))),
            op_seq=self.op_seq + 1,
        )

    def to_record(self) -> dict:
        record = dict(self.legacy)
        record.update({
            "policy_version": RETENTION_POLICY_VERSION,
            "balance_bytes": int(self.balance_bytes),
            "as_of": _iso_ceil(self.as_of),
            "ledger": list(self.ledger),
            "eligible": bool(self.eligible),
            "next_attempt_at": _iso_ceil(self.next_attempt_at),
            "continuation_cutoff": self.continuation_cutoff,
            "last_failure": self.last_failure,
            "progress": self.progress,
            "op_seq": int(self.op_seq),
        })
        return record


def _operation_start(visit_at: dt.datetime, op_clock: float,
                     visit_clock0: "float | None") -> dt.datetime:
    """The operation's start: the visit instant plus the visit clock's
    elapsed time (never negative), or the visit instant itself."""
    if visit_clock0 is None:
        return visit_at
    return visit_at + dt.timedelta(seconds=max(0.0, op_clock - visit_clock0))


# ── the fixed-size record (spec §5.4 "State write", 901-SR-016) ──────────
#
# Every writer stores the record as `json.dumps(record, sort_keys=True)`
# space-padded to exactly `RECLAIM_RECORD_LENGTH` (L) bytes, so a reclaim
# chunk's same-key `UPDATE` replaces the cell by one of the same size: SQLite
# overwrites it in place (`sqlite3BtreeInsert`'s same-size path, audited at
# 3.37.2 and 3.53.4), dirtying exactly the one leaf that holds the row, with
# no rebalance and no page allocated or freed. No writer deletes the row.
#
# L is chosen from the record's BOUNDED fields (`_bounded_record` clamps and
# drops everything else): `max_record_length()` serializes the worst case -
# every integer at its widest, every instant at 20 characters, a 48-character
# failure reason and 25 ledger buckets - which is 3,101 bytes. L = 3,968 is
# 1.279 x that (at least 25% headroom), and the whole cell stays local at
# 4 KiB pages: a 4-byte record header + the 38-byte key + L = 4,010 bytes of
# payload <= U - 35 = 4,061 (no overflow page). Both are asserted at import.
RECLAIM_RECORD_LENGTH = 3968
_RECORD_HEADROOM_NUMERATOR, _RECORD_HEADROOM_DENOMINATOR = 5, 4
_INT64_MIN, _INT64_MAX = -(2 ** 63), 2 ** 63 - 1
_POLICY_VERSION_MAX = 999
_REASON_MAX_CHARS = 48
_LEDGER_KEEP = 25
_NONNEGATIVE_INT_KEYS = ("freelist_count", "unreclaimed_bytes", "op_seq")
_INSTANT_KEYS = ("attempted_at", "as_of", "next_attempt_at",
                 "continuation_cutoff")
#: The pacing instants, rounded UP to a whole second (901-RW-001); the
#: others are observations and keep the truncated form.
_CEIL_INSTANT_KEYS = ("as_of", "next_attempt_at")
_BOOL_KEYS = ("made_progress", "deadline_hit", "eligible")


class StateRecordOversize(ValueError):
    """A serialized record longer than `RECLAIM_RECORD_LENGTH`."""


def _clamp_int(value, low=_INT64_MIN, high=_INT64_MAX) -> "int | None":
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(low, min(high, value))


def _bounded_instant(value, *, ceil: bool = False) -> "str | None":
    parsed = _parse_utc(value)
    if parsed is not None and ceil:
        try:
            parsed = _ceil_second(parsed)
        except OverflowError:
            return None
    if parsed is None or not 1000 <= parsed.year <= 9999:
        return None
    return _budget.iso_z(parsed)


def _bounded_reason(value) -> str:
    if (isinstance(value, str) and 0 < len(value) <= _REASON_MAX_CHARS
            and all(c.islower() or c.isdigit() or c == "_" for c in value)
            and value.isascii()):
        return value
    return "unknown"


def _bounded_record(record: dict) -> dict:
    """The record restricted to its known, bounded fields: what every writer
    stores, so the serialized length can never exceed the computed maximum."""
    out = {}
    for key in _NONNEGATIVE_INT_KEYS:
        if key in record:
            out[key] = _clamp_int(record[key], 0)
    if "balance_bytes" in record:
        out["balance_bytes"] = _clamp_int(record["balance_bytes"])
    if "policy_version" in record:
        out["policy_version"] = _clamp_int(record["policy_version"], 0,
                                           _POLICY_VERSION_MAX)
    for key in _INSTANT_KEYS:
        if key in record:
            out[key] = _bounded_instant(record[key],
                                        ceil=key in _CEIL_INSTANT_KEYS)
    for key in _BOOL_KEYS:
        if key in record:
            out[key] = record[key] is True
    if "ledger" in record:
        buckets = _budget.parse_ledger(record["ledger"])[-_LEDGER_KEEP:]
        out["ledger"] = [
            {"hour": _budget.iso_z(b.hour),
             "charged": _clamp_int(b.charged, 0),
             "largest": _clamp_int(b.largest, 0)} for b in buckets]
    if "last_failure" in record:
        failure = record["last_failure"]
        out["last_failure"] = (
            {"reason": _bounded_reason(failure.get("reason")),
             "at": _bounded_instant(failure.get("at"))}
            if isinstance(failure, dict) else None)
    if "progress" in record:
        progress = record["progress"]
        out["progress"] = (
            {"freelist_pages": _clamp_int(progress.get("freelist_pages"), 0),
             "page_size": _clamp_int(progress.get("page_size"), 0),
             "observed_at": _bounded_instant(progress.get("observed_at"))}
            if isinstance(progress, dict) else None)
    return out


def _record_json(record: dict) -> str:
    return json.dumps(_bounded_record(record), sort_keys=True)


def serialize_reclaim_record(record: dict) -> str:
    """The record as exactly `RECLAIM_RECORD_LENGTH` bytes of space-padded
    JSON; raises `StateRecordOversize` rather than store another length."""
    text = _record_json(record)
    if len(text.encode("utf-8")) > RECLAIM_RECORD_LENGTH:
        raise StateRecordOversize(len(text))
    return text + " " * (RECLAIM_RECORD_LENGTH - len(text.encode("utf-8")))


def max_record_length() -> int:
    """The longest record `_bounded_record` can produce."""
    instant = "9999-12-31T23:59:59Z"
    widest_nonnegative = _INT64_MAX
    worst = {key: widest_nonnegative for key in _NONNEGATIVE_INT_KEYS}
    worst.update({key: instant for key in _INSTANT_KEYS})
    worst.update({key: False for key in _BOOL_KEYS})
    worst.update({
        "balance_bytes": _INT64_MIN,
        "policy_version": _POLICY_VERSION_MAX,
        "ledger": [{"hour": f"9999-12-{1 + h // 24:02d}T{h % 24:02d}:00:00Z",
                    "charged": widest_nonnegative,
                    "largest": widest_nonnegative}
                   for h in range(_LEDGER_KEEP)],
        "last_failure": {"reason": "x" * _REASON_MAX_CHARS, "at": instant},
        "progress": {"freelist_pages": widest_nonnegative,
                     "page_size": widest_nonnegative, "observed_at": instant},
    })
    return len(_record_json(worst).encode("utf-8"))


def _varint_size(value: int) -> int:
    size = 1
    while value >= 0x80 and size < 9:
        value >>= 7
        size += 1
    return size


def record_cell_payload_bytes(length: int = RECLAIM_RECORD_LENGTH) -> int:
    """The `cache_meta(key, value)` record payload of the #780 row: header
    (its size varint + the key's and the value's serial types), key, value."""
    key = len(RECLAIM_PENDING_KEY.encode("utf-8"))
    types = _varint_size(2 * key + 13) + _varint_size(2 * length + 13)
    header = types + 1
    return _varint_size(header) - 1 + header + key + length


def record_fits_locally(usable_size: int,
                        length: int = RECLAIM_RECORD_LENGTH) -> bool:
    """A table-leaf cell keeps its whole payload local up to U - 35 bytes."""
    return record_cell_payload_bytes(length) <= int(usable_size) - 35


assert RECLAIM_RECORD_LENGTH * _RECORD_HEADROOM_DENOMINATOR >= (
    max_record_length() * _RECORD_HEADROOM_NUMERATOR), (
    "the #780 record length needs 25% headroom over its largest record")
assert record_fits_locally(4096), (
    "the #780 record cell must stay local on a 4 KiB page")


def read_reclaim_pending(conn: sqlite3.Connection) -> "dict | None":
    """The durable #780/#901 record (padding stripped), or None when absent
    or unreadable."""
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            (RECLAIM_PENDING_KEY,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    try:
        state = json.loads(str(row[0]).rstrip(" "))
    except (TypeError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def reclaim_record_length(conn: sqlite3.Connection) -> "int | None":
    """The stored record's length in bytes, or None when the row is absent."""
    row = conn.execute(
        "SELECT length(CAST(value AS BLOB)) FROM cache_meta WHERE key=?",
        (RECLAIM_PENDING_KEY,),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _write_reclaim_pending(conn: sqlite3.Connection, state: "dict | None") -> None:
    """Store the record at exactly L bytes. Nothing deletes the row: `None`
    (an ended episode) rewrites the current record with `eligible` false."""
    if state is None:
        current = read_reclaim_pending(conn) or {}
        state = {**current, "eligible": False}
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (RECLAIM_PENDING_KEY, serialize_reclaim_record(state)),
    )


def _write_reclaim_state_in_chunk(conn: sqlite3.Connection,
                                  record: dict) -> None:
    """A reclaim chunk's state write: a same-key `UPDATE` of the existing
    L-byte row, never an insert or `REPLACE`, so it overwrites one cell in
    place. Raises `StateRecordOversize` before writing anything."""
    value = serialize_reclaim_record(record)
    cursor = conn.execute("UPDATE cache_meta SET value=? WHERE key=?",
                          (value, RECLAIM_PENDING_KEY))
    if cursor.rowcount != 1:
        raise sqlite3.OperationalError("the #780 record row is missing")


def normalize_reclaim_record(conn: sqlite3.Connection,
                             now_utc: dt.datetime) -> bool:
    """Write the record at length L when it is absent (an older binary's
    deleted-row state) or of another length (a legacy or pre-#901 record),
    in its own state-only transaction outside any chunk. Uncharged, and
    measured under I2 like every other maintenance state write. Returns True
    when it wrote. `conn` must hold no open transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if reclaim_record_length(conn) == RECLAIM_RECORD_LENGTH:
            conn.rollback()
            return False
        record = read_reclaim_pending(conn)
        if not isinstance(record, dict) or record.get("policy_version") is None:
            record = PacingState.from_record(record, now_utc).to_record()
        _write_reclaim_pending(conn, record)
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-int(numerator) // int(denominator))


def page_frame_bytes(page_size: int) -> int:
    """One distinct dirty page's whole cost: its WAL frame (page + 24-byte
    header) and its eventual checkpoint copy (spec I4)."""
    return 2 * int(page_size) + 24


def pointer_map_page_bytes(page_size: int) -> int:
    """B: 1.25 x (2P + 24) rounded up to a KiB, from the store's page size."""
    return _ceil_div(POINTER_MAP_FACTOR_NUMERATOR * page_frame_bytes(page_size),
                     POINTER_MAP_FACTOR_DENOMINATOR * KiB) * KiB


def pointer_map_cap(page_count: int, usable_size: int, rows: int,
                    page_size: int) -> int:
    """I4's M_cap = ceil(N_max / (floor(U/5) + 1)) + 1, where N_max is the
    page count at the operation's start plus ceil((512 KiB + 12 KiB x rows)
    / P): every other dirty page appended at the end of the file (Q8)."""
    growth = _ceil_div(I4_DELETION_FIXED_BYTES
                       + I4_DELETION_PER_ROW_BYTES * max(0, int(rows)),
                       int(page_size))
    n_max = int(page_count) + growth
    return _ceil_div(n_max, int(usable_size) // 5 + 1) + 1


def deletion_reservation(rows: int, *, page_count: int, usable_size: int,
                         page_size: int) -> int:
    """F + A x rows + B x M_cap (spec §5.4, Q8), from geometry read inside
    the operation's `BEGIN IMMEDIATE`."""
    rows = max(0, int(rows))
    per_row = (DELETION_PER_ROW_FALLBACK_BYTES
               if rows > DELETION_FALLBACK_ROW_THRESHOLD
               else DELETION_PER_ROW_BYTES)
    return (DELETION_FIXED_BYTES + per_row * rows
            + pointer_map_page_bytes(page_size)
            * pointer_map_cap(page_count, usable_size, rows, page_size))


def _record_int(op: dict, key: str) -> "int | None":
    value = op.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def recompute_charge(op: dict) -> "int | None":
    """The reservation an operation record's own inputs imply, or None when
    the record lacks them or carries another formula version. `op` is an
    operation payload (`as_phase_payload`) or a published maintenance record
    with its `phase` ("delete" | "reclaim")."""
    if not isinstance(op, dict):
        return None
    if op.get("reservation_version") != RESERVATION_VERSION:
        return None
    phase = op.get("phase") or op.get("kind")
    if phase == "delete":
        values = [_record_int(op, k) for k in (
            "rows", "page_count", "usable_size", "page_size")]
        if None in values or values[3] <= 0 or values[2] <= 0:
            return None
        rows, page_count, usable_size, page_size = values
        return deletion_reservation(rows, page_count=page_count,
                                    usable_size=usable_size,
                                    page_size=page_size)
    if phase == "reclaim":
        values = [_record_int(op, k) for k in (
            "identified_pages", "unidentified_pages", "page_size",
            "fixed_bytes")]
        if None in values or values[2] <= 0 or values[0] < 1:
            return None
        identified, unidentified, page_size, fixed = values
        return reclaim_reservation(identified, unidentified,
                                   page_size=page_size, fixed_bytes=fixed)
    return None


def reclaim_reservation(identified: int, unidentified: int, *,
                        page_size: int,
                        fixed_bytes: int = RECLAIM_FIXED_BYTES) -> int:
    """(|ID| + UNID + 1) x (2P + 24) + F_r from a chunk's plan (Q9)."""
    return _planner.reservation_bytes(identified, unidentified, page_size,
                                      fixed_bytes)


# ── store geometry, eligibility and the ceiling ──────────────────────────

@dataclass(frozen=True)
class StoreGeometry:
    page_count: int
    freelist_count: int
    page_size: int

    @property
    def free_bytes(self) -> int:
        return self.freelist_count * self.page_size


#: `SQLITE_FCNTL_RESERVE_BYTES` (sqlite3.h): a negative argument only reads
#: the bytes reserved at the end of every page.
_SQLITE_FCNTL_RESERVE_BYTES = 38
#: The most a page can reserve: the conservative usable size when the
#: reserve cannot be read (a smaller U only raises M_cap and the charge).
_MAX_RESERVED_BYTES = 255


def _sqlite_handle(conn) -> "int | None":
    """The `sqlite3 *` behind a CPython connection (through test wrappers
    that delegate via `_conn`), as `bin/_lib_sqlite_close.py` reaches it."""
    real = conn
    for _ in range(4):
        if isinstance(real, sqlite3.Connection):
            break
        real = getattr(real, "_conn", None)
    if (not isinstance(real, sqlite3.Connection)
            or sys.implementation.name != "cpython"):
        return None
    import ctypes

    return ctypes.c_void_p.from_address(
        id(real) + 2 * ctypes.sizeof(ctypes.c_void_p)).value or None


def reserved_bytes(conn) -> "int | None":
    """Bytes reserved per page of `main`, read through the stdlib
    extension's own SQLite (never a raw descriptor of the database file,
    whose close would drop this process's POSIX locks); None when it cannot
    be read."""
    handle = _sqlite_handle(conn)
    if handle is None:
        return None
    try:
        import _sqlite3
        import ctypes

        file_control = ctypes.CDLL(_sqlite3.__file__).sqlite3_file_control
        file_control.argtypes = (ctypes.c_void_p, ctypes.c_char_p,
                                 ctypes.c_int, ctypes.c_void_p)
        file_control.restype = ctypes.c_int
        value = ctypes.c_int(-1)
        rc = file_control(ctypes.c_void_p(handle), b"main",
                          _SQLITE_FCNTL_RESERVE_BYTES, ctypes.byref(value))
    except (OSError, AttributeError, ImportError):
        return None
    if rc != 0 or not 0 <= value.value <= _MAX_RESERVED_BYTES:
        return None
    return int(value.value)


@dataclass(frozen=True)
class DeletionGeometry:
    """The Q8 reservation inputs, read inside the operation's transaction."""
    page_count: int
    page_size: int
    usable_size: int


def deletion_geometry(conn: sqlite3.Connection) -> DeletionGeometry:
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    reserved = reserved_bytes(conn)
    usable = page_size - (_MAX_RESERVED_BYTES if reserved is None else reserved)
    return DeletionGeometry(page_count, page_size, usable)


def store_geometry(conn: sqlite3.Connection) -> "StoreGeometry | None":
    try:
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError):
        return None
    if page_count <= 0 or page_size <= 0 or freelist < 0:
        return None
    return StoreGeometry(page_count, freelist, page_size)


def reclaim_episode_active(geometry: "StoreGeometry | None",
                           episode_active: bool) -> bool:
    """Start above 2 GiB AND 20%; an active episode stops at 1 GiB OR 10%."""
    if geometry is None:
        return False
    free = geometry.free_bytes
    ratio = geometry.freelist_count / geometry.page_count
    if episode_active:
        return free > RECLAIM_STOP_BYTES and ratio > RECLAIM_STOP_RATIO
    return free > RECLAIM_START_BYTES and ratio > RECLAIM_START_RATIO


def observed_reclaim_backlog_bytes(conn: sqlite3.Connection) -> "int | None":
    geometry = store_geometry(conn)
    return None if geometry is None else geometry.free_bytes


def backlog_over_ceiling(backlog_bytes: "int | None") -> bool:
    return backlog_bytes is not None and backlog_bytes >= RECLAIM_CEILING_BYTES


def observed_backlog_over_ceiling(conn: sqlite3.Connection) -> bool:
    """Doctor FAIL and the rebuild refusal: the OBSERVED freelist."""
    return backlog_over_ceiling(observed_reclaim_backlog_bytes(conn))


# ── connections ───────────────────────────────────────────────────────────

def _apply_connection_settings(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout={_CONNECTION_BUSY_TIMEOUT_MS}")
    conn.execute(f"PRAGMA synchronous={_CONNECTION_SYNCHRONOUS}")
    conn.execute(f"PRAGMA journal_size_limit={_CONNECTION_JOURNAL_SIZE_LIMIT}")


def open_deletion_connection(db_path) -> sqlite3.Connection:
    """The per-visit deletion connection: 256 MiB cache, in-memory temp
    store, SQLite's automatic checkpoint and default close. Closed at the end
    of the visit, so the enlarged cache never outlives it."""
    conn = sqlite3.connect(str(db_path),
                           timeout=_CONNECTION_BUSY_TIMEOUT_MS / 1000)
    try:
        _apply_connection_settings(conn)
        conn.execute(f"PRAGMA cache_size = -{DELETION_CACHE_SIZE_KIB}")
        conn.execute("PRAGMA temp_store = MEMORY")
    except BaseException:
        conn.close()
        raise
    return conn


class ReclaimUnavailable(Exception):
    """Reclaim cannot run without checkpointing; carries a typed reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def open_reclaim_connection(db_path) -> sqlite3.Connection:
    """A connection that never checkpoints: not at a commit
    (`wal_autocheckpoint = 0`) and not at its close, even as the last
    connection (no-checkpoint-on-close). Raises `ReclaimUnavailable` when the
    close setting cannot be applied.

    Spec §5.4 (revision 8): `cache_spill = OFF` and `temp_store = MEMORY`, so
    no page of a chunk reaches the WAL before its commit and no statement
    journal touches disk."""
    conn = sqlite3.connect(str(db_path),
                           timeout=_CONNECTION_BUSY_TIMEOUT_MS / 1000)
    try:
        try:
            _apply_connection_settings(conn)
            conn.execute("PRAGMA cache_spill = OFF")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA wal_autocheckpoint = 0")
            applied = (
                int(conn.execute("PRAGMA cache_spill").fetchone()[0]),
                int(conn.execute("PRAGMA temp_store").fetchone()[0]))
        except sqlite3.Error as exc:
            # The primary's disposition 5: a pragma that fails is a typed
            # refusal (§5.4 "a failure to apply the reclaim connection's
            # settings"), never a raw sqlite3.Error from the opener.
            raise ReclaimUnavailable("connection_settings_unavailable") from exc
        if applied != (0, 2):
            raise ReclaimUnavailable("connection_settings_unavailable")
        if int(conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]) != 0:
            raise ReclaimUnavailable("no_checkpoint_on_close_unavailable")
        try:
            _lib_sqlite_close.set_no_checkpoint_on_close(
                conn, True, purpose="paced reclaim")
        except (sqlite3.Error, OSError, AttributeError, TypeError) as exc:
            raise ReclaimUnavailable(
                "no_checkpoint_on_close_unavailable") from exc
    except BaseException:
        conn.close()
        raise
    return conn


def _open_state_connection(db_path) -> sqlite3.Connection:
    """For a state-only record write (episode end, failure): never
    checkpoints at commit; no-checkpoint-on-close where available."""
    conn = sqlite3.connect(str(db_path),
                           timeout=_CONNECTION_BUSY_TIMEOUT_MS / 1000)
    _apply_connection_settings(conn)
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    try:
        _lib_sqlite_close.set_no_checkpoint_on_close(
            conn, True, purpose="retention state")
    except (sqlite3.Error, OSError, AttributeError, TypeError):
        pass
    return conn


# Seams the tests replace to observe each connection a visit opens.
_open_deletion_connection = open_deletion_connection
_open_reclaim_connection = open_reclaim_connection


# ── expired groups ────────────────────────────────────────────────────────

def _prunable_groups(
    conn: sqlite3.Connection, table: str, key_col: str, cutoff: str
) -> list[str]:
    """Group keys with a dated row all before the cutoff. MAX(timestamp_utc)
    IS NOT NULL excludes all-NULL-timestamp groups (F12)."""
    sql = (
        f"SELECT {key_col} FROM {table} "
        f"WHERE {key_col} IS NOT NULL "
        f"GROUP BY {key_col} "
        f"HAVING MAX(timestamp_utc) IS NOT NULL AND MAX(timestamp_utc) < ?"
    )
    return [row[0] for row in conn.execute(sql, (cutoff,))]


def _prunable_null_identity_paths(
    conn: sqlite3.Connection, table: str, key_col: str, cutoff: str
) -> list[str]:
    """F12: NULL-identity rows are pruned per source_path.

    #901 Amendment 19 T1 (spec I1): the per-path ``MAX(timestamp_utc)`` is
    folded in Python over the streamed NULL-identity rows, one entry per
    path, where ``GROUP BY source_path`` sorted every such row through a temp
    b-tree. The class is legacy (current ingest never writes it), but nothing
    bounds how much of it a store retains. Same result: a path qualifies when
    it has a dated row and its latest dated row is before ``cutoff``, in
    ascending path order.
    """
    latest: dict = {}
    for source_path, stamp in conn.execute(
        f"SELECT source_path, timestamp_utc FROM {table} "
        f"WHERE {key_col} IS NULL"
    ):
        if stamp is None:
            continue
        held = latest.get(source_path)
        if held is None or _sqlite_order(stamp) > _sqlite_order(held):
            latest[source_path] = stamp
    bound = _sqlite_order(cutoff)
    return [
        path for path in sorted(latest, key=_sqlite_order)
        if _sqlite_order(latest[path]) < bound
    ]


def _sqlite_order(value) -> tuple:
    """SQLite's cross-type collation for ``MAX``/``<``/``ORDER BY`` on one
    value: NULL < numbers < text (BINARY) < blobs."""
    if value is None:
        return (0, 0)
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        return (2, value.encode("utf-8"))
    return (3, bytes(value))


@dataclass(frozen=True)
class ExpiredGroup:
    provider: str          # "claude" | "codex"
    by_path: bool          # NULL identity, grouped by source_path (F12)
    value: str


_GROUP_TABLES = {
    "claude": ("conversation_messages", "session_id"),
    "codex": ("codex_conversation_events", "conversation_key"),
}


def expired_groups(conn: sqlite3.Connection, cutoff_iso: str) -> "list[ExpiredGroup]":
    """Claude groups first, then Codex: the order the visit deletes them."""
    groups: "list[ExpiredGroup]" = []
    for provider in ("claude", "codex"):
        table, key = _GROUP_TABLES[provider]
        groups += [ExpiredGroup(provider, False, value)
                   for value in _prunable_groups(conn, table, key, cutoff_iso)]
        groups += [ExpiredGroup(provider, True, value)
                   for value in _prunable_null_identity_paths(
                       conn, table, key, cutoff_iso)]
    return groups


def group_row_count(conn: sqlite3.Connection, group: ExpiredGroup) -> int:
    """Rows the reservation charges: `conversation_messages` for a Claude
    group, `codex_conversation_events` for a Codex one."""
    table, key = _GROUP_TABLES[group.provider]
    if group.by_path:
        sql = f"SELECT COUNT(*) FROM {table} WHERE {key} IS NULL AND source_path = ?"
    else:
        sql = f"SELECT COUNT(*) FROM {table} WHERE {key} = ?"
    return int(conn.execute(sql, (group.value,)).fetchone()[0])


def _rowcount(cursor) -> int:
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def _delete_claude_session(conn: sqlite3.Connection, session_id: str) -> int:
    conn.execute("DELETE FROM conversation_file_touches WHERE session_id = ?",
                 (session_id,))
    deleted = _rowcount(conn.execute(
        "DELETE FROM conversation_messages WHERE session_id = ?", (session_id,)))
    conn.execute("DELETE FROM conversation_ai_titles WHERE session_id = ?",
                 (session_id,))
    conn.execute("DELETE FROM conversation_sessions WHERE session_id = ?",
                 (session_id,))
    return deleted


def _delete_claude_null_path(conn: sqlite3.Connection, source_path: str) -> int:
    # NULL-session messages carry no session_id-keyed titles/rollup, and
    # conversation_file_touches requires a NOT NULL session_id, so stray
    # touches are deleted by message_id before the messages themselves.
    ids = [row[0] for row in conn.execute(
        "SELECT id FROM conversation_messages "
        "WHERE session_id IS NULL AND source_path = ?", (source_path,))]
    if ids:
        conn.executemany(
            "DELETE FROM conversation_file_touches WHERE message_id = ?",
            [(i,) for i in ids])
    return _rowcount(conn.execute(
        "DELETE FROM conversation_messages "
        "WHERE session_id IS NULL AND source_path = ?", (source_path,)))


def _delete_codex_conversation_derived(
    conn: sqlite3.Connection, conversation_key: str
) -> None:
    """#294 S6: drop one pruned conversation's normalized derived rows in the
    same transaction as its physical events (§3.2 or-delete, §3.4 partial
    delete riding the per-row FTS trigger, never `'delete-all'`)."""
    conn.execute(
        "DELETE FROM codex_conversation_file_touches WHERE conversation_key = ?",
        (conversation_key,))
    conn.execute(
        "DELETE FROM codex_conversation_messages WHERE conversation_key = ?",
        (conversation_key,))
    conn.execute(
        "DELETE FROM codex_conversation_rollups WHERE conversation_key = ?",
        (conversation_key,))


def _delete_codex_conversation(conn: sqlite3.Connection, key: str) -> int:
    _delete_codex_conversation_derived(conn, key)
    return _rowcount(conn.execute(
        "DELETE FROM codex_conversation_events WHERE conversation_key = ?",
        (key,)))


def _delete_codex_null_path(conn: sqlite3.Connection, source_path: str) -> int:
    # NULL-conversation_key events never gained thread identity, so S6 never
    # normalized them (§4.1) — there are no derived rows to clean up.
    return _rowcount(conn.execute(
        "DELETE FROM codex_conversation_events "
        "WHERE conversation_key IS NULL AND source_path = ?", (source_path,)))


def _delete_group_rows(conn: sqlite3.Connection, group: ExpiredGroup) -> int:
    if group.provider == "claude":
        if group.by_path:
            return _delete_claude_null_path(conn, group.value)
        return _delete_claude_session(conn, group.value)
    if group.by_path:
        return _delete_codex_null_path(conn, group.value)
    return _delete_codex_conversation(conn, group.value)


def prune_conversation_transcripts(
    conn: sqlite3.Connection,
    *,
    cutoff_utc: dt.datetime,
    after_group: "Callable[[], None] | None" = None,
) -> PruneStats:
    """The unpaced kernel: prune every expired group inside the caller's
    transaction. Kept for direct kernel callers; the orchestrator below runs
    each group as a paced, reserved operation instead."""
    cutoff = _cutoff_iso(cutoff_utc)
    claude_sessions, claude_messages = _prune_claude(
        conn, cutoff, after_group=after_group)
    codex_conversations, codex_events = _prune_codex(
        conn, cutoff, after_group=after_group)
    return PruneStats(
        claude_sessions=claude_sessions,
        claude_messages=claude_messages,
        codex_conversations=codex_conversations,
        codex_events=codex_events,
    )


def _prune_provider(conn, provider, cutoff, after_group):
    groups = 0
    rows = 0
    table, key = _GROUP_TABLES[provider]
    for by_path, values in (
            (False, _prunable_groups(conn, table, key, cutoff)),
            (True, _prunable_null_identity_paths(conn, table, key, cutoff))):
        for value in values:
            rows += _delete_group_rows(conn, ExpiredGroup(provider, by_path, value))
            groups += 1
            if after_group is not None:
                after_group()
    return groups, rows


def _prune_claude(conn, cutoff, *, after_group=None) -> "tuple[int, int]":
    return _prune_provider(conn, "claude", cutoff, after_group)


def _prune_codex(conn, cutoff, *, after_group=None) -> "tuple[int, int]":
    return _prune_provider(conn, "codex", cutoff, after_group)


# ── operations (spec §5.4 protocol) ───────────────────────────────────────

@dataclass(frozen=True)
class OperationResult:
    op_id: int
    kind: str                 # "delete" | "reclaim"
    outcome: str              # "ok" | "gated" | "no_progress" | "skipped"
    started_at: dt.datetime
    duration_s: float
    rows: int = 0
    pages: int = 0
    charged_bytes: int = 0
    balance_before_bytes: "int | None" = None
    balance_after_bytes: "int | None" = None
    mode: str = ""
    process_write_bytes: "int | None" = None
    write_status: str = "not_sampled"
    write_overlap: bool = False
    skip_reason: "str | None" = None
    # Q8: the deletion reservation's inputs, so the charge is recomputable
    # from the record alone (`recompute_charge`).
    page_count: "int | None" = None
    usable_size: "int | None" = None
    page_size: "int | None" = None
    pointer_map_cap: "int | None" = None
    reservation_version: "int | None" = None
    # Q9: a reclaim chunk's plan inputs and outcome (§5.5). The planned page
    # set itself is receipt-only detail and stays out of the record.
    planner_version: "int | None" = None
    sqlite_source_id: "str | None" = None
    freelist_count: "int | None" = None
    steps: "int | None" = None
    kind_free: "int | None" = None
    kind_overflow: "int | None" = None
    kind_leaf: "int | None" = None
    kind_interior: "int | None" = None
    identified_pages: "int | None" = None
    unidentified_pages: "int | None" = None
    fixed_bytes: "int | None" = None
    plan_digest: "str | None" = None
    reader_status: "str | None" = None
    inspected_bytes: "int | None" = None
    inspection_ms: "int | None" = None
    freelist_reduction: "int | None" = None
    page_count_reduction: "int | None" = None

    def as_phase_payload(self) -> dict:
        return {
            "op_id": self.op_id,
            "started_at": _iso(self.started_at),
            "duration_s": self.duration_s,
            "rows": self.rows,
            "pages_reclaimed": self.pages,
            "charged_bytes": self.charged_bytes,
            "process_write_bytes": self.process_write_bytes,
            "write_status": self.write_status,
            "write_overlap": self.write_overlap,
            "balance_before_bytes": self.balance_before_bytes,
            "balance_after_bytes": self.balance_after_bytes,
            "outcome": self.outcome,
            "mode": self.mode,
            "page_count": self.page_count,
            "usable_size": self.usable_size,
            "page_size": self.page_size,
            "pointer_map_cap": self.pointer_map_cap,
            "reservation_version": self.reservation_version,
            "skip_reason": self.skip_reason,
            "planner_version": self.planner_version,
            "sqlite_source_id": self.sqlite_source_id,
            "freelist_count": self.freelist_count,
            "steps": self.steps,
            "kind_free": self.kind_free,
            "kind_overflow": self.kind_overflow,
            "kind_leaf": self.kind_leaf,
            "kind_interior": self.kind_interior,
            "identified_pages": self.identified_pages,
            "unidentified_pages": self.unidentified_pages,
            "fixed_bytes": self.fixed_bytes,
            "plan_digest": self.plan_digest,
            "reader_status": self.reader_status,
            "inspected_bytes": self.inspected_bytes,
            "inspection_ms": self.inspection_ms,
            "freelist_reduction": self.freelist_reduction,
            "page_count_reduction": self.page_count_reduction,
        }


def _result(kind, outcome, state, started_at, t0, clock, observation, **kw):
    return OperationResult(
        op_id=state.op_seq, kind=kind, outcome=outcome, started_at=started_at,
        duration_s=max(clock() - t0, 0.0),
        process_write_bytes=observation.process_write_bytes,
        write_status=observation.status, write_overlap=observation.overlap,
        **kw)


def delete_group(conn: sqlite3.Connection, group: ExpiredGroup, *,
                 now_utc: dt.datetime, cutoff_iso: str,
                 clock: "Callable[[], float]" = time.monotonic,
                 visit_clock0: "float | None" = None) -> OperationResult:
    """One whole expired group as one paced operation (spec §5.4 steps 1–4).

    ``visit_clock0`` is the visit clock at the visit's start: the charge's
    ledger bucket is the operation's own start, ``now_utc`` plus the elapsed
    visit time (PR-13 (a)), while the allowance accrues at ``now_utc``."""
    started_at = now_utc.astimezone(UTC)
    rows = group_row_count(conn, group)
    fallback = rows > DELETION_FALLBACK_ROW_THRESHOLD
    mode = "fallback" if fallback else "spill_free"
    # temp_store may only change outside a transaction; the provider flocks
    # the visit holds keep `rows` exact until the DELETE.
    conn.execute(f"PRAGMA temp_store = {'FILE' if fallback else 'MEMORY'}")
    interval = _lib_write_io.begin_interval("maintenance")
    t0 = clock()
    op_started = _operation_start(started_at, t0, visit_clock0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = PacingState.from_record(read_reclaim_pending(conn), started_at)
        before = state.available(started_at)
        if before < 0:
            conn.rollback()
            observation = _lib_write_io.end_interval(interval)
            return _result("delete", "gated", state, started_at, t0, clock,
                           observation, balance_before_bytes=before,
                           balance_after_bytes=before, mode=mode)
        # Q8: the geometry is read inside this transaction, before the
        # reservation is recorded, so M_cap bounds this operation's file.
        geometry = deletion_geometry(conn)
        cap = pointer_map_cap(geometry.page_count, geometry.usable_size,
                              rows, geometry.page_size)
        reservation = deletion_reservation(
            rows, page_count=geometry.page_count,
            usable_size=geometry.usable_size, page_size=geometry.page_size)
        charged = dataclasses.replace(
            state.charge(reservation, started_at, started_utc=op_started),
            continuation_cutoff=cutoff_iso)
        _write_reclaim_pending(conn, charged.to_record())
        _delete_group_rows(conn, group)
        conn.commit()
    except BaseException:
        conn.rollback()
        _lib_write_io.end_interval(interval)
        raise
    observation = _lib_write_io.end_interval(interval)
    _lib_write_io.mark_deletion(observation, rows=rows)
    return _result("delete", "ok", charged, started_at, t0, clock, observation,
                   rows=rows, charged_bytes=reservation,
                   balance_before_bytes=before,
                   balance_after_bytes=charged.balance_bytes, mode=mode,
                   page_count=geometry.page_count,
                   usable_size=geometry.usable_size,
                   page_size=geometry.page_size, pointer_map_cap=cap,
                   reservation_version=RESERVATION_VERSION)


def _run_incremental_vacuum_chunk(conn: sqlite3.Connection, steps: int) -> None:
    """Run ``steps`` ``incremental_vacuum(1)`` statements INSIDE the caller's
    transaction, with no other SQL between them.

    One step per statement: a statement limited to one page is complete
    after its first step on every Python, so CPython 3.11's single-step
    zero-column cursor cannot shorten it, and no `executescript` is needed -
    that call would COMMIT the open transaction first and split the
    reservation from its mutation."""
    for _ in range(int(steps)):
        conn.execute("PRAGMA incremental_vacuum(1)").fetchall()


def _sqlite_version_info() -> tuple:
    """The runtime SQLite's version (a seam G3q injects)."""
    return tuple(sqlite3.sqlite_version_info)


class _ChunkRefused(Exception):
    def __init__(self, reason: str, *, inspected_bytes=None,
                 inspection_ms=None):
        super().__init__(reason)
        self.reason = reason
        self.inspected_bytes = inspected_bytes
        self.inspection_ms = inspection_ms


def _request_plan(db_path, *, page_size: int, page_count: int,
                  freelist_count: int, max_steps: int,
                  deadline_s: float) -> "_planner.ReclaimPlan":
    """Plan the chunk in a fresh helper process while this process holds
    the chunk's `BEGIN IMMEDIATE` (spec §5.4 "Read path"). Raises
    `_ChunkRefused` with a typed reason; never opens the store itself."""
    deadline_s = max(_PLANNER_MIN_DEADLINE_SECONDS, float(deadline_s))
    request = {"db": str(db_path), "pageSize": int(page_size),
               "pageCount": int(page_count),
               "freelistCount": int(freelist_count),
               "maxSteps": int(max_steps),
               "budgetBytes": RECLAIM_CHUNK_BUDGET_BYTES,
               "fixedBytes": RECLAIM_FIXED_BYTES,
               "readBudgetBytes": _PLANNER_READ_BUDGET_BYTES,
               "deadlineMs": int(deadline_s * 1000)}
    try:
        child = subprocess.run(
            list(_PLANNER_COMMAND) + ["--plan"], input=json.dumps(request),
            capture_output=True, text=True, close_fds=True,
            timeout=deadline_s + _PLANNER_STARTUP_SLACK_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise _ChunkRefused("helper_timeout") from exc
    except OSError as exc:
        raise _ChunkRefused("helper_failed") from exc
    if child.returncode != 0:
        raise _ChunkRefused("helper_failed")
    try:
        answer = json.loads(child.stdout)
    except ValueError as exc:
        raise _ChunkRefused("helper_failed") from exc
    if not isinstance(answer, dict):
        raise _ChunkRefused("helper_failed")
    if answer.get("ok") is not True:
        reason = answer.get("reason")
        raise _ChunkRefused(
            reason if reason in _planner.REFUSAL_REASONS else "helper_failed",
            inspected_bytes=_record_int(answer, "inspectedBytes"),
            inspection_ms=_record_int(answer, "inspectionMs"))
    try:
        plan = _planner.ReclaimPlan.from_wire(answer.get("plan"))
    except (ValueError, TypeError, KeyError) as exc:
        raise _ChunkRefused("helper_failed") from exc
    if ((plan.page_size, plan.page_count, plan.freelist_count)
            != (int(page_size), int(page_count), int(freelist_count))
            or plan.steps > int(max_steps)
            or plan.fixed_bytes != RECLAIM_FIXED_BYTES):
        raise _ChunkRefused("wal_endpoint_mismatch")
    return plan


def _plan_fields(plan: "_planner.ReclaimPlan") -> dict:
    return dict(
        planner_version=_planner.PLANNER_VERSION,
        page_size=plan.page_size, usable_size=plan.usable_size,
        page_count=plan.page_count, freelist_count=plan.freelist_count,
        steps=plan.steps, kind_free=plan.kinds["free"],
        kind_overflow=plan.kinds["overflow"], kind_leaf=plan.kinds["leaf"],
        kind_interior=plan.kinds["interior"],
        identified_pages=len(plan.identified),
        unidentified_pages=plan.unidentified, fixed_bytes=plan.fixed_bytes,
        plan_digest=plan.digest, reader_status=plan.reader_status,
        inspected_bytes=plan.inspected_bytes,
        inspection_ms=plan.inspection_ms,
        reservation_version=RESERVATION_VERSION)


def reclaim_chunk(conn: sqlite3.Connection, *, now_utc: dt.datetime,
                  pages: int = RECLAIM_MAX_CHUNK_PAGES,
                  next_attempt_at: "dt.datetime | None" = None,
                  clock: "Callable[[], float]" = time.monotonic,
                  deadline_s: "float | None" = None,
                  visit_clock0: "float | None" = None) -> OperationResult:
    """One planned reclaim chunk of at most ``pages`` (<= 16) steps as one
    paced operation (spec §5.4, Q9), on a connection from
    `open_reclaim_connection` (it never checkpoints).

    In ONE `BEGIN IMMEDIATE` transaction: the record precondition, then the
    plan (a helper subprocess reads the snapshot this transaction holds),
    then the planned `incremental_vacuum(1)` steps with no other SQL between
    the plan's reads and the first step, then the frozen reservation's
    state write (a same-key `UPDATE`), then `COMMIT`. Any refusal rolls back
    with no vacuum and no charge and returns `outcome="skipped"` with its
    typed `skip_reason`."""
    started_at = now_utc.astimezone(UTC)
    max_steps = max(1, min(int(pages), RECLAIM_MAX_CHUNK_PAGES))
    deadline = (_PLANNER_DEADLINE_SECONDS if deadline_s is None
                else float(deadline_s))
    interval = _lib_write_io.begin_interval("maintenance")
    t0 = clock()
    op_started = _operation_start(started_at, t0, visit_clock0)
    source_id = None
    state = PacingState(balance_bytes=0, as_of=started_at)
    before = None

    def skipped(reason, **fields):
        conn.rollback()
        observation = _lib_write_io.end_interval(interval)
        return _result("reclaim", "skipped", state, started_at, t0, clock,
                       observation, balance_before_bytes=before,
                       balance_after_bytes=before, mode="reclaim",
                       skip_reason=reason, reader_status=fields.pop(
                           "reader_status", reason),
                       sqlite_source_id=source_id, **fields)

    try:
        conn.execute("BEGIN IMMEDIATE")
        if reclaim_record_length(conn) != RECLAIM_RECORD_LENGTH:
            # The in-place state write needs the L-byte row; the attempt
            # normalizes it in its own transaction before any chunk.
            return skipped("state_record_unnormalized", reader_status=None)
        state = PacingState.from_record(read_reclaim_pending(conn), started_at)
        before = state.available(started_at)
        geometry = store_geometry(conn)
        free = 0 if geometry is None else geometry.freelist_count
        if before < 0 or free <= 0:
            conn.rollback()
            observation = _lib_write_io.end_interval(interval)
            return _result("reclaim", "gated" if before < 0 else "no_progress",
                           state, started_at, t0, clock, observation,
                           balance_before_bytes=before,
                           balance_after_bytes=before, mode="reclaim")
        source_id = str(conn.execute(
            "SELECT sqlite_source_id()").fetchone()[0])[:96]
        if not _planner.sqlite_audited(_sqlite_version_info()):
            return skipped("sqlite_unaudited")
        if int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]) != 2:
            return skipped("unsupported_geometry")
        reserved = reserved_bytes(conn)
        usable = geometry.page_size - (
            _MAX_RESERVED_BYTES if reserved is None else reserved)
        if reserved != 0 or not record_fits_locally(usable):
            return skipped("unsupported_geometry")
        db_path = _resolve_main_db_path(conn)
        try:
            plan = _request_plan(
                db_path, page_size=geometry.page_size,
                page_count=geometry.page_count, freelist_count=free,
                max_steps=max_steps, deadline_s=deadline)
        except _ChunkRefused as refused:
            return skipped(refused.reason,
                           inspected_bytes=refused.inspected_bytes,
                           inspection_ms=refused.inspection_ms)
        # No other SQL between the plan's reads and the first step.
        _run_incremental_vacuum_chunk(conn, plan.steps)
        after = store_geometry(conn)
        reservation = plan.reservation_bytes
        remaining = after.freelist_count
        charged = dataclasses.replace(
            state.charge(reservation, started_at, started_utc=op_started),
            eligible=True,
            next_attempt_at=next_attempt_at or state.next_attempt_at,
            last_failure=None,
            progress={"freelist_pages": remaining,
                      "page_size": after.page_size,
                      "observed_at": _iso(started_at)},
            legacy={**state.legacy,
                    "freelist_count": remaining,
                    "unreclaimed_bytes": remaining * after.page_size,
                    "attempted_at": _iso(started_at),
                    "made_progress": True, "deadline_hit": False},
        )
        try:
            _write_reclaim_state_in_chunk(conn, charged.to_record())
        except StateRecordOversize:
            return skipped("state_record_oversize", **{
                k: v for k, v in _plan_fields(plan).items()
                if k not in ("reader_status",)})
        conn.commit()
    except BaseException:
        conn.rollback()
        _lib_write_io.end_interval(interval)
        raise
    observation = _lib_write_io.end_interval(interval)
    freelist_reduction = free - remaining
    return _result("reclaim", "ok", charged, started_at, t0, clock,
                   observation, pages=freelist_reduction,
                   charged_bytes=reservation, balance_before_bytes=before,
                   balance_after_bytes=charged.balance_bytes, mode="reclaim",
                   sqlite_source_id=source_id,
                   freelist_reduction=freelist_reduction,
                   page_count_reduction=geometry.page_count - after.page_count,
                   **_plan_fields(plan))


# ── the visit ─────────────────────────────────────────────────────────────

def _retention_due(conn: sqlite3.Connection, now_utc: dt.datetime) -> bool:
    """True iff the prune has never run or last ran more than 24h ago."""
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            (_RETENTION_LAST_PRUNE_KEY,),
        ).fetchone()
    except sqlite3.Error:
        return True
    if row is None or row[0] is None:
        return True
    try:
        last = dt.datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None or last.utcoffset() is None:
        last = last.replace(tzinfo=UTC)
    return (now_utc - last).total_seconds() >= _RETENTION_THROTTLE_SECONDS


def _stamp_retention_prune(conn: sqlite3.Connection, now_utc: dt.datetime) -> None:
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_RETENTION_LAST_PRUNE_KEY, now_utc.astimezone(UTC).isoformat()),
    )


def _resolve_main_db_path(conn: sqlite3.Connection):
    """The file behind this connection's ``main`` schema; the conversations
    path for an in-memory connection."""
    try:
        for _seq, name, file_name in conn.execute("PRAGMA database_list"):
            if name == "main" and file_name:
                return file_name
    except sqlite3.Error:
        pass
    return _cctally_core.CONVERSATIONS_DB_PATH


def _report(record_phase, result: OperationResult) -> None:
    if record_phase is not None:
        record_phase(result.kind, result.as_phase_payload())


def _update_record(db_path, mutate) -> None:
    """A state-only record write (no mutation, no charge) on a connection
    that never checkpoints; `mutate(record) -> record | None` (None = no
    change, no write)."""
    conn = _open_state_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            record = read_reclaim_pending(conn)
            updated = mutate(record)
            if updated is None:
                conn.rollback()
                return
            _write_reclaim_pending(conn, updated)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def _end_reclaim_episode(db_path) -> None:
    def mutate(record):
        if not isinstance(record, dict) or record.get("eligible") is not True:
            return None
        return {**record, "eligible": False}
    _update_record(db_path, mutate)


def _record_reclaim_failure(db_path, reason: str, now_utc: dt.datetime) -> None:
    """Durable only when the reason changes, so repeated skips rewrite
    nothing."""
    def mutate(record):
        record = dict(record) if isinstance(record, dict) else {}
        prior = record.get("last_failure")
        if isinstance(prior, dict) and prior.get("reason") == reason:
            return None
        if record.get("policy_version") is None:
            record = PacingState.from_record(record, now_utc).to_record()
        record["last_failure"] = {"reason": reason, "at": _iso(now_utc)}
        return record
    _update_record(db_path, mutate)


def _stamp_retention_complete(conn: sqlite3.Connection, now_utc: dt.datetime) -> None:
    """Both providers finished: the daily stamp, and the continuation
    cleared, in one small transaction on the deletion connection."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _stamp_retention_prune(conn, now_utc)
        record = read_reclaim_pending(conn)
        if isinstance(record, dict) and record.get("continuation_cutoff") is not None:
            _write_reclaim_pending(conn, {**record, "continuation_cutoff": None})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _run_deletion_visit(db_path, *, now, retention_days, state, record_phase,
                        clock) -> PruneStats:
    visit_clock0 = clock()
    cutoff_iso = state.continuation_cutoff or _cutoff_iso(
        now - dt.timedelta(days=int(retention_days)))
    counts = {"claude_sessions": 0, "claude_messages": 0,
              "codex_conversations": 0, "codex_events": 0}
    complete = True
    conn = _open_deletion_connection(db_path)
    try:
        groups = expired_groups(conn, cutoff_iso)
        for index, group in enumerate(groups):
            result = delete_group(conn, group, now_utc=now,
                                  cutoff_iso=cutoff_iso, clock=clock,
                                  visit_clock0=visit_clock0)
            _report(record_phase, result)
            if result.outcome != "ok":
                complete = False
                break
            if group.provider == "claude":
                counts["claude_sessions"] += 1
                counts["claude_messages"] += result.rows
            else:
                counts["codex_conversations"] += 1
                counts["codex_events"] += result.rows
            if (result.balance_after_bytes is not None
                    and result.balance_after_bytes < 0
                    and index + 1 < len(groups)):
                complete = False
                break
        if complete:
            _stamp_retention_complete(conn, now)
    finally:
        conn.close()
    return PruneStats(**counts, complete=complete)


#: A refused attempt writes nothing durable, so its next attempt time is
#: kept here, per store, to hold the one-minute cadence (spec §5.4). A durable
#: failure (the reclaim connection unavailable, a SQLite error) records its
#: reason only when it changes and writes no `next_attempt_at`, so it holds
#: the same cadence here too (#901 Amendment 19 PR-4).
_REFUSED_UNTIL: "dict[str, dt.datetime]" = {}

#: The reasons that come from the planner and its helper (in-memory only);
#: the connection-settings and SQLite-error skips stay durable, as before.
_IN_MEMORY_SKIP_REASONS = frozenset(_planner.REFUSAL_REASONS) | {
    "helper_failed", "helper_timeout", "state_record_unnormalized",
    "state_record_oversize"}


def _refusal_due(db_path, now: dt.datetime) -> bool:
    until = _REFUSED_UNTIL.get(str(db_path))
    return until is None or now >= until


def _publish_refusal(db_path, reason: "str | None", now: dt.datetime) -> None:
    """The in-memory pass record of a refused attempt (no durable write):
    `writeIo.reclaimRefusal` for the doctor, and the next attempt time."""
    try:
        if reason is None:
            _REFUSED_UNTIL.pop(str(db_path), None)
            _lib_write_io.clear_reclaim_refusal()
        else:
            _REFUSED_UNTIL[str(db_path)] = now + dt.timedelta(
                seconds=RECLAIM_ATTEMPT_INTERVAL_SECONDS)
            _lib_write_io.set_reclaim_refusal(reason, now)
    except Exception:  # noqa: BLE001 — telemetry must not fail maintenance
        pass


def _hold_after_failure(db_path, now: dt.datetime) -> None:
    """A durable reclaim failure waits one interval before the next attempt
    (spec §5.4: "attempted at most once per minute"). In memory, like a
    refusal's hold, so a repeated failure still rewrites nothing; it publishes
    no refusal, because the failure is already the record's `last_failure`.
    The next chunk that runs clears it (`_publish_refusal(..., None, ...)`)."""
    _REFUSED_UNTIL[str(db_path)] = now + dt.timedelta(
        seconds=RECLAIM_ATTEMPT_INTERVAL_SECONDS)


def _run_reclaim_attempt(db_path, *, now, record_phase, clock) -> None:
    next_attempt = now + dt.timedelta(seconds=RECLAIM_ATTEMPT_INTERVAL_SECONDS)
    try:
        conn = _open_reclaim_connection(db_path)
    except ReclaimUnavailable as exc:
        _hold_after_failure(db_path, now)
        _record_reclaim_failure(db_path, exc.reason, now)
        _report(record_phase, OperationResult(
            op_id=0, kind="reclaim", outcome="skipped", started_at=now,
            duration_s=0.0, mode="reclaim", skip_reason=exc.reason,
            reader_status=exc.reason))
        return
    started = clock()
    try:
        # §5.4 "State write": a legacy-length or absent record is normalized
        # in its own uncharged transaction before the first chunk.
        normalize_reclaim_record(conn, now)
        while clock() - started < RECLAIM_DEADLINE_SECONDS:
            if not reclaim_episode_active(store_geometry(conn), True):
                conn.close()
                conn = None
                _end_reclaim_episode(db_path)
                break
            remaining = RECLAIM_DEADLINE_SECONDS - (clock() - started)
            result = reclaim_chunk(conn, now_utc=now, next_attempt_at=next_attempt,
                                   clock=clock, deadline_s=remaining,
                                   visit_clock0=started)
            _report(record_phase, result)
            if result.outcome == "skipped" and (
                    result.skip_reason in _IN_MEMORY_SKIP_REASONS):
                _publish_refusal(db_path, result.skip_reason, now)
                break
            if result.outcome == "ok":
                _publish_refusal(db_path, None, now)
            if result.outcome != "ok" or (result.balance_after_bytes or 0) < 0:
                break
    except sqlite3.Error:
        _hold_after_failure(db_path, now)
        _record_reclaim_failure(db_path, "sqlite_error", now)
    finally:
        if conn is not None:
            conn.close()


def _publish_record(record) -> None:
    """Hand the latest ledger to this process's `writeIo.maintenance`."""
    try:
        _lib_write_io.set_maintenance_record(record)
    except Exception:  # noqa: BLE001 — telemetry must not fail maintenance
        pass


def _maybe_prune_conversation_retention(
    conn: sqlite3.Connection,
    *,
    now_utc: dt.datetime,
    retention_days: int,
    force: bool = False,
    record_phase: "Callable[[str, dict], None] | None" = None,
    clock: "Callable[[], float]" = time.monotonic,
) -> "PruneStats | None":
    """One paced, flock-serialized retention visit (F7 + F9 + #901).

    Returns the deletion's :class:`PruneStats` when a deletion visit ran
    (``complete`` False on a continuation), or ``None`` when no deletion ran
    (retention disabled, nothing due, the allowance in debt, a lock
    contended, or a reclaim-only visit). The one exception is a forced
    (explicit) visit that is due but finds the allowance in debt: it returns
    an empty ``PruneStats(complete=False)`` so the caller reports the
    allowance rather than mistaking the gate for lock contention.

    Concurrency (F7) is unchanged: the MAINTENANCE flock is claimed
    EXCLUSIVE non-blocking and downgraded to SHARED once both provider flocks
    (Claude then Codex, non-blocking) are held, so a rival visit or `db
    vacuum` still cannot claim it while fail-closed panel readers can. Every
    flock is held for the whole visit. A visit with nothing due, or with the
    allowance in debt, takes no provider flock and writes nothing.

    ``force`` bypasses only the 24 h throttle (a from-zero replay). It never
    bypasses the allowance. ``conn`` must hold no provider flock and no open
    transaction; the visit reads through it and opens its own deletion and
    reclaim connections. ``retention_days <= 0`` disables retention.
    """
    if retention_days is None or retention_days <= 0:
        return None
    core = _cctally_core
    try:
        core.APP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    now = now_utc.astimezone(UTC)
    db_path = _resolve_main_db_path(conn)
    maint_fh = open(core.CONVERSATIONS_LOCK_MAINTENANCE_PATH, "w")
    try:
        try:
            fcntl.flock(maint_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return None  # another visit holds it; skip cleanly, do NOT stamp
        record = read_reclaim_pending(conn)
        _publish_record(record)
        state = PacingState.from_record(record, now)
        episode = reclaim_episode_active(store_geometry(conn), state.eligible)
        episode_ended = state.eligible and not episode
        deletion_due = (force or state.continuation_cutoff is not None
                        or _retention_due(conn, now))
        reclaim_due = (episode and (state.next_attempt_at is None
                                    or now >= state.next_attempt_at)
                       and _refusal_due(db_path, now))
        funded = state.available(now) >= 0
        # An explicit prune gated by the allowance deletes nothing; say so
        # (an empty continuation), never the lock-contention None.
        gated = PruneStats(complete=False) if (
            force and deletion_due and not funded) else None
        if not episode_ended and not (funded and (deletion_due or reclaim_due)):
            return gated
        claude_fh = open(core.CONVERSATIONS_LOCK_PATH, "w")
        try:
            try:
                fcntl.flock(claude_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return None  # a Claude sync is mid-flight; retry next cycle
            codex_fh = open(core.CONVERSATIONS_LOCK_CODEX_PATH, "w")
            try:
                try:
                    fcntl.flock(codex_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    return None  # a Codex sync is mid-flight; retry next cycle
                # Downgrade the maintenance flock to SHARED for the visit
                # proper, AFTER both provider flocks (a flock conversion is not
                # atomic; the provider flocks make the window harmless).
                try:
                    fcntl.flock(maint_fh, fcntl.LOCK_SH)
                except OSError:
                    pass  # keep the exclusive hold; correctness is unchanged
                if episode_ended:
                    _end_reclaim_episode(db_path)
                stats = None
                if funded and deletion_due:
                    stats = _run_deletion_visit(
                        db_path, now=now, retention_days=retention_days,
                        state=state, record_phase=record_phase, clock=clock)
                if funded and reclaim_due:
                    _run_reclaim_attempt(db_path, now=now,
                                         record_phase=record_phase, clock=clock)
                _publish_record(read_reclaim_pending(conn))
                return stats if stats is not None else gated
            finally:
                try:
                    fcntl.flock(codex_fh, fcntl.LOCK_UN)
                except OSError:
                    pass
                codex_fh.close()
        finally:
            try:
                fcntl.flock(claude_fh, fcntl.LOCK_UN)
            except OSError:
                pass
            claude_fh.close()
    finally:
        try:
            fcntl.flock(maint_fh, fcntl.LOCK_UN)
        except OSError:
            pass
        maint_fh.close()
