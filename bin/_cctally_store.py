"""Unified SQLite store opener — one connect+PRAGMA policy, one version gate.

Design spec docs/superpowers/specs/2026-07-22-db-journal-redesign-design.md
§6 (access layer). This module is the single chokepoint every SQLite
connection for the three disposable indexes (stats.db / cache.db /
conversations.db) routes through for its **connection policy** and its
**schema-apply version gate**. ``open_db`` / ``open_cache_db`` /
``open_conversations_db`` stay thin wrappers whose public signatures are
unchanged; they call in here for PRAGMAs and ask ``schema_current`` whether the
full DDL/migration apply may be skipped.

Two responsibilities live here (spec §6.1 / §6.2):

- ``open_index(store)`` — connect to the store's DB path (URI mode per policy),
  install the row factory, and (test-only) install a trace hook. Corruption
  handling stays in each opener (Task 8 moves it to the classifier-gated
  ``HEAL_HOOK`` seam below), so ``open_index`` never probes or recreates.
- ``apply_policy(conn, store)`` — apply the §6.1 PRAGMA policy
  (journal_mode / synchronous / busy_timeout / journal_size_limit /
  auto_vacuum). ``auto_vacuum`` is emitted **before** ``journal_mode`` because
  it only takes on a DB whose first page has not been written yet.
- ``schema_current(conn, store)`` — the §6.2 version gate: one
  ``PRAGMA user_version`` read compared to the store's registry head. When it
  returns ``True`` the opener skips the full schema executescript +
  ``add_column_if_missing`` probes + FTS branch, so the steady-state open is
  connect → PRAGMAs → one ``user_version`` read → done.

**Contract change (Task 2, spec §6.2):** cache/conversations schema changes must
ride a migration-registry bump; a bare ``add_column_if_missing`` in
``_apply_cache_schema`` (or ``_apply_conversations_schema``) no longer reaches
version-current DBs, because the whole schema apply is version-gated and skipped
once ``user_version == registry head``. Add the column via a registered
migration (which bumps the head and re-triggers the apply) instead.

**Lock-order law** (spec §5.2 / §6.4; asserted here as documentation, exercised
by the storm test): maintenance flocks → ``journal.ingest.lock`` → the global
``cache.db.lock`` writer flock → the cache Codex provider flock → conversation
provider flocks (Claude → Codex) → SQLite transactions → ``journal.lock``
(leaf). Never acquire the ingest lock while holding a cache writer/provider
flock; no SQLite write transaction ever spans a flock acquisition.

**Raw-connect escape hatches stay OUT of this module by design** (spec §6.1):
``db checkpoint``'s ``mode=rw`` connect and ``db vacuum``'s exclusive connect
deliberately bypass ``open_index``/``open_db`` so they carry no schema-apply /
migration side effects on maintenance paths.

**#386 narrowed that carve-out for stats.** Skipping the *schema apply* is not
the same as skipping the *opener protocol*: spec §3.1's third clause requires
every opener of the live stats family to observe the repair marker and the
quarantine-pending record under maintenance-SHARED across connect. Doctor's
read-write probes (``bin/_cctally_doctor.py``), ``db backup --db stats``'s
``mode=ro`` source, and ``_db_status_for``'s status connect therefore all route
through ``stats_open_guarded`` with their OWN ``connect`` callable — they keep
their open mode and their freedom from schema side effects while still
participating. The claim that doctor's gather "bypasses the opener" is no longer
true and must not be restored.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import pathlib
import re
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass

import _cctally_core
import _cctally_db


# --------------------------------------------------------------------------
# §6.1 policy table
# --------------------------------------------------------------------------

# WAL size caps (spec §6.1). Mirror the long-standing per-DB constants
# (_cctally_core.STATS_WAL_SIZE_LIMIT_BYTES, _cctally_cache.CACHE_WAL_SIZE_LIMIT_BYTES);
# duplicated here so the store module does not import _cctally_cache (which
# loads this module — that would be a cycle).
_STATS_WAL_SIZE_LIMIT_BYTES = 16 * 1024 * 1024   # 16 MiB
_CACHE_WAL_SIZE_LIMIT_BYTES = 128 * 1024 * 1024  # 128 MiB


@dataclass(frozen=True)
class StorePolicy:
    """The §6.1 connection policy for one store."""

    journal_mode: str            # "WAL"
    synchronous: str             # "NORMAL"
    busy_timeout: int            # milliseconds
    journal_size_limit: int      # bytes
    auto_vacuum: str | None      # "INCREMENTAL", or None to leave unset
    row_factory: str             # "row" (sqlite3.Row) | "tuple"
    uri: bool                    # connect with uri=True (RO ATTACH support)


STORE_POLICY: dict[str, StorePolicy] = {
    # stats.db: auto_vacuum stays unset on normal opens. §6.1: INCREMENTAL only
    # from the first epoch rebuild — a populated DB needs a full VACUUM to
    # change modes, so it is deliberately NOT applied at in-place cutover.
    "stats": StorePolicy(
        journal_mode="WAL", synchronous="NORMAL", busy_timeout=15000,
        journal_size_limit=_STATS_WAL_SIZE_LIMIT_BYTES, auto_vacuum=None,
        row_factory="row", uri=False,
    ),
    "cache": StorePolicy(
        journal_mode="WAL", synchronous="NORMAL", busy_timeout=15000,
        journal_size_limit=_CACHE_WAL_SIZE_LIMIT_BYTES, auto_vacuum="INCREMENTAL",
        row_factory="tuple", uri=False,
    ),
    "conversations": StorePolicy(
        journal_mode="WAL", synchronous="NORMAL", busy_timeout=15000,
        journal_size_limit=_CACHE_WAL_SIZE_LIMIT_BYTES, auto_vacuum="INCREMENTAL",
        row_factory="tuple", uri=True,
    ),
}


# Test-only tracing seam (spec §10 / §6.5 hot-path guards). When non-None it is
# installed via ``conn.set_trace_callback`` on every connection ``open_index``
# hands out, so a test can count the DDL statements a steady-state open runs
# (must be zero once the version gate holds). Production leaves it None.
_TRACE_HOOK = None

# Task 8 seam (spec §6.3): classifier-gated corruption auto-heal. Wired to
# ``_stats_heal_hook`` at the bottom of the module. ``open_db`` calls
# ``HEAL_HOOK("stats", exc)`` from its corruption boundary; a True return means
# "healed — retry the open once".
HEAL_HOOK = None


def _store_path(store: str):
    if store == "stats":
        return _cctally_core.DB_PATH
    if store == "cache":
        return _cctally_core.CACHE_DB_PATH
    if store == "conversations":
        return _cctally_core.CONVERSATIONS_DB_PATH
    raise ValueError(f"unknown store {store!r}")


def _apply_row_factory(conn: sqlite3.Connection, policy: StorePolicy) -> None:
    conn.row_factory = sqlite3.Row if policy.row_factory == "row" else None


def open_index(store: str) -> sqlite3.Connection:
    """Connect to ``store``'s DB (URI mode per policy) + install the row factory.

    Does NOT apply PRAGMAs, probe, or recreate — corruption handling is the
    opener's job (Task 8 moves it to ``HEAL_HOOK``). The test trace hook, when
    armed, is installed here so it captures every statement the opener then runs
    (the gated schema apply included). Callers apply the PRAGMA policy with
    ``apply_policy`` once the connection is confirmed usable.
    """
    policy = STORE_POLICY[store]
    conn = sqlite3.connect(_store_path(store), uri=policy.uri)
    if _TRACE_HOOK is not None:
        conn.set_trace_callback(_TRACE_HOOK)
    _apply_row_factory(conn, policy)
    return conn


def apply_policy(conn: sqlite3.Connection, store: str) -> None:
    """Apply the §6.1 PRAGMA policy for ``store`` to an open connection.

    ``auto_vacuum`` (when set) is emitted first: it only takes effect before the
    first page is written, so it must precede ``journal_mode=WAL`` / any DDL.
    """
    policy = STORE_POLICY[store]
    if policy.auto_vacuum is not None:
        conn.execute(f"PRAGMA auto_vacuum={policy.auto_vacuum}")
    conn.execute(f"PRAGMA journal_mode={policy.journal_mode}")
    apply_connection_policy(conn, store)


def apply_connection_policy(conn: sqlite3.Connection, store: str) -> None:
    """Apply non-schema connection settings without changing journal mode."""
    policy = STORE_POLICY[store]
    conn.execute(f"PRAGMA synchronous={policy.synchronous}")
    conn.execute(f"PRAGMA busy_timeout={policy.busy_timeout}")
    conn.execute(f"PRAGMA journal_size_limit={policy.journal_size_limit}")


# --------------------------------------------------------------------------
# §6.2 version gate
# --------------------------------------------------------------------------

def _expected_head(store: str) -> int:
    """The store's schema head to compare ``user_version`` against.

    cache/conversations gate on their migration-registry length (read live off
    ``_cctally_db`` so a test that mutates the registry is honored). stats
    returns -1 — a value ``user_version`` can never equal — so ``schema_current``
    is always False for it; ``open_db`` keeps its own schema-apply until Task 9
    flips it to the ``STATS_INDEX_EPOCH`` gate.

    conversations joined the framework in Task 10 (spec §7.2): it gates on
    ``len(_CONVERSATIONS_MIGRATIONS)`` (head 1), so an up-to-date conversations.db
    (``user_version == 1``) skips the schema apply on the steady-state open.
    """
    if store == "cache":
        return len(_cctally_db._CACHE_MIGRATIONS)
    if store == "conversations":
        return len(_cctally_db._CONVERSATIONS_MIGRATIONS)
    return -1  # stats: never gated in Task 2


def schema_current(conn: sqlite3.Connection, store: str) -> bool:
    """True when ``store``'s stamped ``user_version`` equals its registry head.

    When True the opener may skip the full DDL executescript +
    ``add_column_if_missing`` probes + FTS branch entirely (spec §6.2). A head
    of ``<= 0`` (a store with no registry yet, e.g. conversations pre-Task-10)
    always returns False so the schema keeps being applied every open.
    """
    head = _expected_head(store)
    if head <= 0:
        return False
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    return user_version == head


# --------------------------------------------------------------------------
# §7.1 stats.db epoch gate (Task 9)
# --------------------------------------------------------------------------

def stats_epoch_enabled() -> bool:
    """True when the stats epoch/cutover machinery should engage (spec §7.1/§8).

    It engages ONLY against the FROZEN production stats registry
    (``len(_STATS_MIGRATIONS) == LEGACY_STATS_HEAD``). Under
    ``CCTALLY_MIGRATION_TEST_MODE`` the injected 14th stats migration lifts the
    count, so this returns False and ``open_db`` keeps the legacy dispatcher
    behavior (no epoch gate, no cutover) — which is exactly what the
    migration-framework golden harness exercises."""
    return len(_cctally_db._STATS_MIGRATIONS) == _cctally_core.LEGACY_STATS_HEAD


def would_block_prod_stats_cutover(path) -> bool:
    """A dev/worktree binary must refuse to CUT OVER a stats.db physically in the
    real prod dir (spec §8, mirrors #146): the epoch stamp would brick the
    installed release via ``DowngradeDetected``. Reuses the path-based prod-guard
    predicate; ``CCTALLY_ALLOW_PROD_MIGRATION=1`` is the escape hatch."""
    return _cctally_db._would_block_prod_stats(path)


# --------------------------------------------------------------------------
# §6.2 one-shot gate for open_db's three open-time backfills (Task 8)
# --------------------------------------------------------------------------
#
# stats.db is NOT epoch-gated until Task 9 (which flips the whole schema+
# migration+backfill region onto ``user_version == STATS_INDEX_EPOCH``). Until
# then, three self-extinguishing open-time writers still run — the
# five_hour_window_key backfill probe, the durable quota-projection schema apply,
# and the historical five_hour_blocks rollup backfill (+ its migration-003
# re-invocation). Each is cheap-but-nonzero per open (probe SELECTs / an
# executescript), and under a multi-agent hook storm that cost recurs many times
# a second (spec §1.3). §6.2 requires the steady-state open to do ZERO
# probe/DDL/backfill work.
#
# We gate all three on a single DB-RESIDENT marker (``stats_open_fixups``) — a
# marker must travel WITH the DB, not a file in APP_DIR, because a fresh index
# built by ``rebuild_stats_index`` (which quarantines the old file) needs the
# quota-projection SCHEMA applied; a stale file marker would leave the fresh DB
# missing those tables. A single-row table is the same framework-untracked
# additive posture as ``journal_cursor`` / ``weekly_credit_floors``. Steady state:
# one guarded SELECT, no DDL. Task 9's epoch gate SUBSUMES this (the whole region
# is skipped once the DB is epoch-current); the marker + these helpers then become
# a harmless inner short-circuit that Task 9 may retire.
#
# Bump ``_STATS_OPEN_FIXUPS_VERSION`` when a NEW open-time backfill is added, so
# existing installs re-run the fixups once to pick it up.
#
# 1 -> 2 (public #5): the quota-projection schema gained
# ``quota_window_blocks.physical_group_key`` / ``physical_group_digest`` and the
# ``quota_projection_ledger_state`` row. An epoch-mismatched index resolves that
# by REBUILD, but a LEGACY index (``user_version <= LEGACY_STATS_HEAD``) takes
# the in-place cutover instead, and the cutover relies on this open-time schema
# apply — which a stamped marker skips outright. Without the bump such a DB is
# stamped at the new epoch while still missing the columns, and every subsequent
# open returns at the steady-state gate before any schema work could add them:
# a permanent `no such column: physical_group_key`.
#
# 2 -> 3 (public #5, I2 review): the same seam, one column later. The periodic
# verification adds `quota_projection_ledger_state.last_full_pass_at`, and a
# legacy index that already ran the fixups at version 2 would skip the schema
# apply that adds it.
#
# 3 -> 4 (#460): scheduled boundary ownership adds
# `quota_projection_ledger_state.next_evaluation_by_root_json` through the same
# in-place legacy cutover seam.
_STATS_OPEN_FIXUPS_VERSION = 4


def stats_open_fixups_current(conn: sqlite3.Connection) -> bool:
    """True when open_db's three open-time backfills have already run for this
    stats.db and need not run (or probe) again (spec §6.2, Task 8).

    A missing ``stats_open_fixups`` table (fresh / pre-gate DB) or a stamped
    version below the binary's expectation returns False, so the fixups run once
    and re-stamp. Guarded so the read is safe before any schema exists — the only
    steady-state cost is this one SELECT."""
    try:
        row = conn.execute(
            "SELECT version FROM stats_open_fixups WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None and int(row[0]) >= _STATS_OPEN_FIXUPS_VERSION


def mark_stats_open_fixups_done(conn: sqlite3.Connection) -> None:
    """Stamp the ``stats_open_fixups`` marker after the three open-time backfills
    ran (spec §6.2). Creates the single-row marker table on demand — this DDL
    runs ONLY when the fixups run (first open / upgrade / rebuild), never on the
    steady-state open — then upserts the current version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stats_open_fixups ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO stats_open_fixups (id, version) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
        (_STATS_OPEN_FIXUPS_VERSION,),
    )


# --------------------------------------------------------------------------
# §6.3 classifier-gated corruption auto-heal (Task 8 Item 3)
# --------------------------------------------------------------------------
#
# The sequence on a POSITIVELY classified stats.db corruption (spec §6.3):
#   classify (SQLITE_CORRUPT / NOTADB / "malformed" — NEVER busy/locked/perm)
#   → dev-checkout-on-prod guard → acquire the MAINTENANCE lock (top of the
#   lock-order law) → locked RE-CHECK (a sibling process may have healed already)
#   → FORENSICS-FIRST (bundle written before evidence is disturbed) → acquire the
#   ingest lock (bounded — the serialized stats writer) → QUARANTINE the damaged
#   family into a timestamped incident dir (never delete evidence) → REBUILD a
#   fresh index from the journal → return True so ``open_db`` retries the open
#   once. A second failure surfaces loudly.
#
# The ingest-lock acquire is BOUNDED (not indefinite): a corruption surfacing
# from *inside* a ``run_stats_ingest`` cycle already holds the ingest lock, so an
# indefinite wait would self-deadlock. The bounded wait proceeds without the lock
# on timeout — correctness does not depend on it, because the rebuild writes a
# scratch index and ATOMICALLY swaps it in while the damaged family is already
# quarantined out of the way (a concurrent writer's writes land on the
# quarantined inode and are discarded).
#
# ``_HEAL_ACTIVE`` is a re-entrancy guard: the rebuild's own ``open_db`` calls use
# ``_target_path`` (auto-heal disarmed) and the post-heal retry opens the FRESH
# index, so this is belt-and-suspenders against any nested corruption looping.

_HEAL_ACTIVE = False


# --------------------------------------------------------------------------
# #386 sanctioned-write context
# --------------------------------------------------------------------------
#
# A stats.db mutation is legal only inside this scope, entered by the ingester
# while it holds ``journal.ingest.lock`` and by maintenance paths while they hold
# ``stats.db.maintenance.lock`` (spec section 3.1's three regimes).
#
# A ``ContextVar``, NOT a module global. The dashboard is threaded, so a
# process-global boolean would let one sanctioned thread authorize an unrelated
# one — precisely the false-positive the review rejected the trace-callback
# design over. ``ContextVar`` values do not propagate into a ``threading.Thread``
# started from inside the scope, which is the property under test in
# ``tests/test_stats_writer_guard_386.py::test_scope_does_not_leak_across_threads``.
#
# ``holds_ingest_lock()`` is deliberately NARROWER than ``in_stats_write_scope()``
# and is not implied by it: maintenance paths are sanctioned writers that do NOT
# hold the ingest lock. The heal path keys its self-deadlock avoidance on the
# narrow fact (see ``_heal_flock_bounded``), so conflating the two would let a
# maintenance caller skip an ingest acquire it never made.

# The two ContextVars live in `_cctally_core` (see the block beside
# `holds_stats_maintenance`): `tests/conftest.py`'s `load_script()` reloads
# every `_cctally_*` sibling but never the kernel, so state kept here would be
# silently reset mid-test and a sanctioned write would then be denied.
_STATS_WRITE_SCOPE = _cctally_core._STATS_WRITE_SCOPE
_INGEST_LOCK_HELD = _cctally_core._STATS_INGEST_LOCK_HELD
_INTERRUPTED_RECOVERY_SUPPRESSED = (
    _cctally_core._STATS_INTERRUPTED_RECOVERY_SUPPRESSED
)


def in_stats_write_scope() -> bool:
    """True when THIS execution context is inside a sanctioned stats-write scope."""
    return _STATS_WRITE_SCOPE.get() > 0


def holds_ingest_lock() -> bool:
    """True when THIS execution context already holds ``journal.ingest.lock``.

    Used by the heal path to distinguish "I am the serialized writer" from
    "someone else holds it": a corruption surfacing from inside a
    ``run_stats_ingest`` cycle already owns the lock, so waiting for it would
    self-deadlock, while any other caller must genuinely wait or decline.
    """
    return _INGEST_LOCK_HELD.get() > 0


@contextlib.contextmanager
def suppress_interrupted_stats_recovery():
    """Keep every nested Doctor stats opener read-only in this context."""
    token = _INTERRUPTED_RECOVERY_SUPPRESSED.set(
        _INTERRUPTED_RECOVERY_SUPPRESSED.get() + 1
    )
    try:
        yield
    finally:
        _INTERRUPTED_RECOVERY_SUPPRESSED.reset(token)


@contextlib.contextmanager
def stats_write_scope(reason: str, *, ingest_lock: bool = False):
    """Mark the enclosed block as a sanctioned stats.db writer.

    ``reason`` is diagnostic only (it names the regime for the Stage 3 guard log).
    ``ingest_lock=True`` additionally asserts that the caller holds
    ``journal.ingest.lock`` for the duration. Nests; restored on exception via
    the ``ContextVar`` tokens, so an unwinding error never leaves the process
    permanently sanctioned.
    """
    depth = _STATS_WRITE_SCOPE.set(_STATS_WRITE_SCOPE.get() + 1)
    held = _INGEST_LOCK_HELD.set(
        _INGEST_LOCK_HELD.get() + (1 if ingest_lock else 0)
    )
    try:
        yield reason
    finally:
        _INGEST_LOCK_HELD.reset(held)
        _STATS_WRITE_SCOPE.reset(depth)


# --------------------------------------------------------------------------
# #386 enforcement — the stats sole-writer authorizer (spec §6.1)
# --------------------------------------------------------------------------
#
# The mechanism is ``Connection.set_authorizer``, NOT ``set_trace_callback``.
# Two independent reasons, both verified rather than assumed:
#
#   1. Python SUPPRESSES exceptions raised inside a trace callback. A trace hook
#      that raises on INSERT does NOT prevent the write — the row commits and
#      `SELECT count(*)` returns 1. An authorizer returning SQLITE_DENY blocks it
#      (count 0). A mechanism that cannot stop the write is diagnostics, not
#      enforcement.
#   2. `_TRACE_HOOK` is only installed by `open_index`, which the stats openers
#      do not go through.
#
# Action CODES, never SQL text. A text classifier mishandles DDL, dynamic table
# names (`UPDATE {table}` in cutover and in eight journal folds), CTEs, and
# comments — and the mutation inventory found 15 dynamic-SQL sites a lexical
# scan cannot resolve at all.
#
# Scoped to the ``main`` schema. Stats connections legitimately create TEMP
# VIEWs outside any write scope (`bin/_cctally_tui.py`), and the dashboard/TUI
# build TEMP views over an ATTACHed cache.db. Those report `temp`/the attach
# alias as `db_name` and are none of this guard's business.
#
# NOT covered, and deliberately so: `PRAGMA user_version = …`, `VACUUM`,
# `os.replace`/`rename`/`unlink`. The first two would require guarding
# SQLITE_PRAGMA (which every `apply_policy` call trips) and the last three are
# invisible to every SQLite hook. 12 of the 14 physical mutation sites are in
# that last class — they are covered by the opener protocol and the lock
# corrections instead. Enforcement and serialization do different jobs here and
# neither substitutes for the other.

_GUARD_MUTATIONS = frozenset({
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_ALTER_TABLE,
})

#: One line per throttle window across all processes. Rotation is still the
#: hard disk-growth bound if the marker is removed or the throttle is disabled.
_GUARD_THROTTLE_S = 60.0
_GUARD_LOG_ROTATE_BYTES = 1024 * 1024
_guard_last_logged = 0.0


def _guard_log_path() -> pathlib.Path:
    """``logs/stats-writer-guard.log`` — the doctor leg's input (spec §6.4)."""
    return pathlib.Path(_cctally_core.HOOK_TICK_LOG_DIR) / "stats-writer-guard.log"


def _guard_rotated_log_path() -> pathlib.Path:
    path = _guard_log_path()
    return path.with_name(path.name + ".1")


def _guard_log_lock_path() -> pathlib.Path:
    path = _guard_log_path()
    return path.with_name(path.name + ".lock")


def _guard_throttle_path() -> pathlib.Path:
    path = _guard_log_path()
    return path.with_name(path.name + ".last")


def _guard_rotate_if_needed(path: pathlib.Path) -> bool:
    """Keep one rotated generation; return False when rotation cannot proceed."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if size < _GUARD_LOG_ROTATE_BYTES:
        return True
    try:
        os.replace(path, _guard_rotated_log_path())
    except OSError:
        return False
    return True


def _log_unsanctioned_write(action, table) -> None:
    global _guard_last_logged
    now_monotonic = time.monotonic()
    if (
        _GUARD_THROTTLE_S > 0
        and _guard_last_logged
        and now_monotonic - _guard_last_logged < _GUARD_THROTTLE_S
    ):
        return
    lock_fd = None
    try:
        path = _guard_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(
            _guard_log_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        # This diagnostic must never park the command whose write triggered it.
        # One storm participant records the incident; contenders fail soft.
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        marker = _guard_throttle_path()
        if _GUARD_THROTTLE_S > 0:
            try:
                marker_age = time.time() - marker.stat().st_mtime
            except FileNotFoundError:
                marker_age = _GUARD_THROTTLE_S
            if 0 <= marker_age < _GUARD_THROTTLE_S:
                _guard_last_logged = now_monotonic
                return

        if not _guard_rotate_if_needed(path):
            return
        line = (
            f"{_cctally_core.now_utc_iso()}\tunsanctioned stats write\t"
            f"action={action}\ttable={table}\n"
        ).encode("utf-8", errors="replace")
        log_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(log_fd, line)
        finally:
            os.close(log_fd)
        marker.touch(mode=0o600, exist_ok=True)
        _guard_last_logged = now_monotonic
    except OSError:
        pass
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)


def _guard_should_raise() -> bool:
    """Raise on a dev checkout or under pytest; log-only on installed builds.

    ``_is_dev_checkout()``, deliberately NOT ``DEV_MODE`` — CLAUDE.md forbids
    collapsing those two predicates, and this is the checkout question, not the
    verbosity one.
    """
    return bool(
        _cctally_core._is_dev_checkout()
        or os.environ.get("PYTEST_CURRENT_TEST")
    )


def arm_stats_authorizer(conn: sqlite3.Connection) -> None:
    """Deny mutations of the stats ``main`` schema outside a sanctioned scope."""

    def _auth(action, arg1, arg2, db_name, trigger):
        if action not in _GUARD_MUTATIONS:
            return sqlite3.SQLITE_OK
        if db_name not in (None, "main"):
            return sqlite3.SQLITE_OK
        if in_stats_write_scope():
            return sqlite3.SQLITE_OK
        if _guard_should_raise():
            return sqlite3.SQLITE_DENY
        _log_unsanctioned_write(action, arg1)
        return sqlite3.SQLITE_OK

    conn.set_authorizer(_auth)


# --------------------------------------------------------------------------
# #386 the open-time mutation regime (spec §3.1's second clause, Gaps C/GAP-1..4)
# --------------------------------------------------------------------------
#
# `open_db`'s post-gate body runs the full schema DDL, the quota-projection
# schema, the migration dispatcher, two backfills, the fixups marker and the
# in-place cutover. Every one of those is a mutation, and before #386 they ran
# under NO lock whatever command reached them — 57 production `open_db` call
# sites, any of which could be racing another. Spec §3.1: "First-open, legacy,
# epoch and administrative mutation … must acquire stats.db.maintenance.lock
# (exclusive) BEFORE mutating, regardless of the command that reached them."
#
# Re-entrant on the same signal the opener uses, because the common case is
# reaching here from a caller that ALREADY holds the lock (`run_stats_ingest`'s
# legacy branch takes it exclusive before `open_db()`; `rebuild_stats_index`
# opens its scratch under the rebuild's hold).

#: Open-time mutation is a one-shot path (first open / legacy / post-rebuild),
#: not the steady-state hot path — the epoch gate returns before it. So the wait
#: is generous where the opener's is short. On expiry we DECLINE; proceeding
#: unlocked would be the "described itself as serialized while running
#: unserialized" defect this session removed from the heal path.
_STATS_OPEN_TIME_MAINTENANCE_WAIT_S = 30.0


@contextlib.contextmanager
def stats_open_time_guard(*, live: bool = True):
    """Hold maintenance-exclusive + the sanctioned scope across open-time DDL.

    ``live=False`` marks a ``_target_path`` (scratch) build: it enters the
    sanctioned scope but takes NO flock, mirroring the divergence
    ``stats_open_guarded`` already documents. Two reasons, both concrete:

      1. A scratch index is not the live family, so serializing it against live
         maintenance buys nothing — and every scratch build already runs under a
         HELD maintenance exclusive (rebuild, rederive) or against a private
         temp copy (`db rederive`'s preview snapshot). Taking the LIVE lock on a
         second fd from inside a held exclusive is the self-deadlock
         `holds_stats_maintenance` exists to prevent.
      2. Creating `stats.db.maintenance.lock` is itself a persistent side
         effect. `db rederive`'s preview has a literal zero-persistent-write
         contract ("without creating any coordination files"), pinned by
         `tests/test_rederive_command.py::test_preview_is_write_free_…`, and its
         snapshot open would otherwise mint that file in the real APP_DIR.
    """
    if not live or _cctally_core.holds_stats_maintenance():
        with stats_write_scope("open-time"):
            yield
        return
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        str(_cctally_core.STATS_LOCK_MAINTENANCE_PATH),
        os.O_RDWR | os.O_CREAT, 0o600,
    )
    acquired = False
    try:
        deadline = time.monotonic() + _STATS_OPEN_TIME_MAINTENANCE_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise _cctally_db.StatsDbMaintenanceError(
                        _STATS_OPEN_MAINTENANCE_TIMEOUT_MSG
                    )
                time.sleep(0.02)
        _cctally_core.note_stats_maintenance_acquired()
        try:
            with stats_write_scope("open-time"):
                yield
        finally:
            _cctally_core.note_stats_maintenance_released()
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


# --------------------------------------------------------------------------
# #386 opener half of the physical-replacement protocol
# --------------------------------------------------------------------------
#
# Spec section 3.1, third clause: EVERY opener of the live stats family --
# read-only consumers included -- observes the repair marker and the
# quarantine-pending record under maintenance-SHARED, held across the marker
# checks AND the connect. That is what makes the pending record's claim to
# "block every opener" true, and it is the half stats never had: `open_db`
# checked `stats.db.repairing` three times around a bare `sqlite3.connect` and
# consulted no pending record at all, so a destructive maintenance path could
# publish its record, scan for handles, and still have a brand-new opener arrive
# in the window before the first rename (spec section 1.1 Gap A's TOCTOU).
#
# Modelled directly on `bin/_cctally_cache.py::_cache_open_guarded`. Two
# deliberate divergences from the cache version, both narrowing:
#
#   1. Repair-marker STALENESS reclaim is NOT ported. The cache upgrades to
#      exclusive, asks `_repair_marker_is_live`, and reclaims a dead owner's
#      marker. Stats has always simply raised, and every existing stats
#      regression asserts that. Changing it is a behaviour change with its own
#      test surface and belongs to whoever owns `db repair`, not to a
#      corruption-prevention pass. Consequence, recorded rather than fixed: a
#      SIGKILLed `db repair --db stats` still strands its marker.
#   2. `_target_path` (scratch) opens skip the flock entirely and keep the
#      pre-#386 marker-only behaviour. A scratch index is not the live family,
#      and every scratch open happens under a HELD maintenance exclusive
#      (rebuild, rederive) -- taking shared on a second fd there is the
#      self-deadlock described in `_cctally_core.holds_stats_maintenance`.


#: Bounded wait for ``stats.db.maintenance.lock`` on the OPENER path (#386).
#
# Stage 2 took this flock UNTIMED, on the path every command uses — every
# `statusline` render and every detached `hook-tick`. Stage 2 also made the
# exclusive holders long: `db rebuild` replays the whole journal, `db vacuum`
# rewrites the file, `db rederive --yes` runs a full scratch replay, `db repair`
# shells out to `sqlite3 .recover`. During any of them EVERY stats open parked
# forever — the #297-class `database is locked` stall that spec §5.2 ground 4
# rejected the checkpoint policy over, reintroduced by the corruption fix.
#
# Bounding it is strictly safe: the TOCTOU guarantee is "you cannot open WHILE
# exclusive is held", and failing after a timeout preserves it — we simply
# decline instead of waiting. Every caller already handles the exception
# (`main()` maps it to exit 3, `doctor` degrades, `cmd_db_status` reports
# `_open_error`).
#
# 5 s is chosen against the two failure directions: it is well under the 15 s
# `busy_timeout` the DB already tolerates (so the opener is never the slowest
# thing on the hot path), and it is orders of magnitude above the handshake it
# must NOT trip — marker publish + drain scan + rename is milliseconds.
_STATS_OPEN_MAINTENANCE_WAIT_S = 5.0

#: How long the opener will wait to UPGRADE to exclusive to resume a pending
#: quarantine. Same reasoning; kept separate so the two can diverge.
_STATS_OPEN_RESUME_WAIT_S = 5.0

_STATS_OPEN_MAINTENANCE_TIMEOUT_MSG = (
    "stats.db maintenance is in progress; retry after the maintenance command "
    "exits"
)


def _flock_bounded(lock_fh, operation: int, timeout_s: float) -> bool:
    """Poll for ``operation`` on ``lock_fh`` until ``timeout_s`` expires.

    ``True`` when the lock is held, ``False`` on expiry (nothing acquired, so
    the caller must not release). Same shape as ``_heal_flock_bounded``, but it
    operates on an already-open file object rather than minting an fd, because
    the opener holds one file object across its whole marker/pending/connect
    sequence.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(lock_fh, operation | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def _stats_repair_marker(db_path) -> pathlib.Path:
    """The live stats repair marker, whatever `db_path` is.

    Deliberately `with_name`, not `<db_path>.repairing`: a scratch/rebuild
    target sitting beside the live DB must still observe the LIVE marker, which
    is the pre-#386 behaviour this preserves byte for byte.
    """
    return pathlib.Path(db_path).with_name("stats.db.repairing")


def _stats_publication_marker(db_path) -> pathlib.Path:
    """The durable publication marker for ``db_path`` (#496 S1 F1)."""
    return pathlib.Path(str(db_path) + ".publication")


def _remove_stats_publication_marker(db_path) -> None:
    import _cctally_journal

    try:
        _stats_publication_marker(db_path).unlink()
    except FileNotFoundError:
        pass
    _cctally_journal._fsync_dir(pathlib.Path(db_path).parent)


def _read_stats_publication_marker(db_path) -> "dict | None":
    """The marker's state as a MAPPING, or None when no marker exists.

    A marker that is present but unreadable, or whose bytes are valid JSON that
    is not an object (`null`, `[]`), reads as an empty mapping: it exists, and
    it records nothing. `json.loads` returns whatever the bytes decode to, so
    calling `.get(...)` on the raw result raises `AttributeError` on those
    shapes — which, from `_raise_settled_publication_failure`, escapes the heal
    hook's `except Exception` and surfaces as a raw traceback from `open_db`.
    """
    try:
        state = json.loads(_stats_publication_marker(db_path).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _discard_pending_stats_publication_marker(db_path) -> None:
    """Drop a PENDING marker whose own publication never replaced anything.

    Such a marker's pinned high-water describes an index that was never
    published, so validating the live destination against it would condemn a
    healthy index (measured: destination cursor 303 against a pinned
    high-water of 2852).

    Callers must establish that fact first — see
    `_pending_stats_publication_never_replaced`. **The maintenance lock does
    NOT establish it.** A rebuild that dies releases its flock, so a scratch on
    disk can belong to a strictly later run than the marker beside it, and the
    two are then unrelated.

    A `priorFailure` block is restored rather than dropped. It records a verdict
    a PREVIOUS publication owed on bytes that are still live, carried forward by
    the run whose marker this is; because that run never replaced the
    destination, those bytes are exactly what a later opener would connect to.

    A `failed` marker is a settled verdict about the CURRENT destination and is
    never discarded here.
    """
    state = _read_stats_publication_marker(db_path)
    if state is None or str(state.get("status") or "") != "pending":
        return
    prior = state.get("priorFailure")
    if isinstance(prior, dict) and prior:
        import _cctally_journal

        _cctally_db._atomic_write_private_json(
            _stats_publication_marker(db_path), prior
        )
        _cctally_journal._fsync_dir(pathlib.Path(db_path).parent)
        return
    _remove_stats_publication_marker(db_path)


def _pending_stats_publication_never_replaced(db_path) -> bool:
    """Whether a PENDING marker's own publication never became live.

    The marker STATES which protocol it belongs to, so the discriminator is
    selected rather than inferred (#496 S3 §5), and the two never generalize
    over each other.

    **Physical replacement** answers with the scratch. `os.replace` is the only
    thing that consumes ANOTHER run's scratch, so a marker still naming a live
    scratch pathname proves that run never reached publication: the live
    destination is the untouched predecessor and the marker is stale. When the
    scratch is gone the replacement DID happen and the verdict on the published
    bytes is still owed, so the marker must be resolved rather than discarded.

    The stronger form of that claim — that `os.replace` is the only consumer of
    any scratch — is false, and the difference is confined to the run's OWN
    process. `_cctally_journal._cleanup_new_correction_scratches` removes the
    scratch this run just created when `_recover_completed_correction`'s
    rebuild raises, so a failing `os.replace` in that path leaves the marker
    naming a scratch that its own process then deleted, and this predicate
    reads that as "replaced". The proxy is used only across processes, where
    that cleanup cannot reach, so the weaker property is the one it needs.

    A marker carrying no `scratchPath` cannot prove it published, so it is
    treated as never-replaced. No released binary has ever written one — the
    marker and this field ship together — so the branch exists only to keep an
    unreadable marker from wedging every open.

    **In-place publication** answers with the publication's own stamp, because
    it attaches the scratch read-only and the scratch survives commit and
    rollback identically. Only `PROVEN_PREDECESSOR` discards; `INDETERMINATE`
    fails closed and the marker is resolved instead.

    Must be consulted BEFORE stale-artifact cleanup removes the scratch. It is
    also what makes artifact-first recovery stamp-aware: a scratch surviving a
    COMMITTED in-place publish is a spent artifact beside an owed verdict, not
    an interrupted rebuild.
    """
    state = _read_stats_publication_marker(db_path)
    if not state:
        return True
    if str(state.get("status") or "") != "pending":
        return False
    if str(state.get("mechanism") or "replace") == "in_place":
        import _cctally_journal

        return _cctally_journal.in_place_publication_proven_predecessor(
            db_path, state
        )
    scratch = state.get("scratchPath")
    if not isinstance(scratch, str) or not scratch:
        return True
    return pathlib.Path(scratch).exists()


def _stats_publication_failed_error(
    db_path, record_path, mechanism=None,
) -> _cctally_db.StatsPublicationFailedError:
    """The guided error for a settled publication failure.

    The wording is selected by the mechanism the marker RECORDS, because the two
    mechanisms leave different things on disk: physical replacement preserves
    the damaged predecessor under `quarantine/`, and an in-place publication
    preserves nothing at all. A marker written before the field existed reads as
    `replace`, which is what those markers describe.
    """
    template = (
        _cctally_core.STATS_PUBLICATION_FAILED_IN_PLACE_MSG
        if str(mechanism or "replace") == "in_place"
        else _cctally_core.STATS_PUBLICATION_FAILED_MSG
    )
    return _cctally_db.StatsPublicationFailedError(
        template.format(path=db_path, record=record_path or "<unrecorded>")
    )


def _raise_settled_publication_failure(db_path) -> None:
    """Re-raise a `failed` publication verdict the caller's `except` swallowed.

    Only a marker already written as `failed` reaches this; every other state
    returns and leaves the caller's behaviour unchanged.
    """
    state = _read_stats_publication_marker(db_path)
    if not state:
        return
    if str(state.get("status") or "") == "failed":
        raise _stats_publication_failed_error(
            db_path, state.get("recordPath"), state.get("mechanism")
        )


def _resolve_stats_publication_marker(db_path: pathlib.Path) -> None:
    """Honour a durable publication marker (#496 S1 F1).

    Caller holds maintenance EXCLUSIVE, which is what makes the pending case
    safe: a rebuild that is still in flight owns the lock, so reaching here
    proves its outcome is settled.

    Order of precedence is established by the caller: a `.rebuilding-*` scratch
    artifact is classified FIRST, so the existing interrupted-rebuild recovery
    keeps taking precedence and clears any stale marker when it republishes.

    This function refuses and reports. It never decides to rebuild — choosing
    when to rebuild is firing policy.
    """
    import _cctally_journal

    marker = _stats_publication_marker(db_path)
    state = _read_stats_publication_marker(db_path)
    if state is None:
        return
    status = str(state.get("status") or "")
    record_path = state.get("recordPath")
    mechanism = state.get("mechanism")

    if status == "failed":
        raise _stats_publication_failed_error(db_path, record_path, mechanism)
    if status != "pending":
        _remove_stats_publication_marker(db_path)
        return

    # The discriminator runs on EVERY path into this function, not only the
    # one the opener reaches with a surviving `.rebuilding-*` family beside
    # the marker. For a `replace` marker the two agree — a missing scratch
    # proves `os.replace` ran, so this returns False and resolution proceeds
    # exactly as before. For an `in_place` marker scratch absence proves
    # NOTHING, and without this the record's pinned high-water would be
    # validated against a generation that was never published: a publication
    # the stamp shows never committed would condemn its own healthy
    # predecessor and refuse every ordinary open. `INDETERMINATE` still fails
    # closed, so an unreadable stamp resolves rather than discards.
    if _pending_stats_publication_never_replaced(db_path):
        _discard_pending_stats_publication_marker(db_path)
        return

    record = None
    if isinstance(record_path, str):
        try:
            record = json.loads(pathlib.Path(record_path).read_text())
        except (OSError, ValueError):
            record = None
    if not isinstance(record, dict) or "highWater" not in record:
        # Without its record the marker cannot be judged, and validating
        # against a guessed high-water would condemn a healthy index. This
        # marker is diagnostic scaffolding; it must not wedge every open.
        print(
            "[stats] discarding an unresolvable stats.db publication marker "
            f"(rebuild record: {record_path!r})",
            file=sys.stderr,
        )
        _remove_stats_publication_marker(db_path)
        return

    raw = record.get("highWater")
    high_water = (
        (str(raw[0]), int(raw[1]))
        if isinstance(raw, (list, tuple)) and len(raw) == 2
        else None
    )
    error = _cctally_journal.validate_published_stats_index(db_path, high_water)
    if error is None:
        _remove_stats_publication_marker(db_path)
        return

    state.update({"status": "failed", "error": error})
    try:
        _cctally_db._atomic_write_private_json(marker, state)
        record["status"] = "failed"
        record["postPublicationValidation"] = {"ok": False, "error": error}
        _cctally_db._atomic_write_private_json(
            pathlib.Path(record_path), record
        )
    except OSError:
        pass
    raise _stats_publication_failed_error(db_path, record_path, mechanism)


def _resume_pending_quarantine(db_path: pathlib.Path) -> None:
    """Finish a strict quarantine that a previous owner did not complete.

    Caller holds maintenance EXCLUSIVE. Fails closed: if any handle on the
    family is still open -- or the platform cannot tell us -- we refuse rather
    than rename files out from under a live mapping, which is precisely the
    "cctally performs file-family surgery with live mappings" class that spec
    section 1.2 identifies as where SQLite's crash guarantees stop applying.
    """
    try:
        open_pids = _cctally_db._db_family_open_pids(db_path)
        if open_pids is None:
            raise OSError(
                "could not verify that the database family has no open handles"
            )
        if open_pids:
            raise OSError(
                "database family is still open in process(es) "
                + ", ".join(str(pid) for pid in sorted(open_pids))
            )
        # Resumes the SAME incident from the pending record -- never a second
        # incident dir, never a recreation.
        _cctally_db.quarantine_db_family(db_path, strict=True)
    except OSError as exc:
        raise sqlite3.OperationalError(
            f"stats.db pending quarantine could not resume: {exc}"
        ) from exc


_STATS_REBUILD_ARTIFACT_RE = re.compile(
    r"^(?P<base>stats\.db\.rebuilding-\d{8}T\d{6}_\d{6})"
    r"(?P<sidecar>-wal|-shm)?$"
)
_STATS_QUARANTINE_INCIDENT_RE = re.compile(
    r"^stats\.db-(?:\d{8}T\d{6}Z|\d{8}T\d{6}_\d{6})$"
)


def _stats_rebuild_artifact_bases(db_path: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Return only Task A's exact scratch-family bases beside ``db_path``."""
    bases: set[pathlib.Path] = set()
    for candidate in db_path.parent.glob(f"{db_path.name}.rebuilding-*"):
        match = _STATS_REBUILD_ARTIFACT_RE.fullmatch(candidate.name)
        if match is not None:
            bases.add(db_path.parent / match.group("base"))
    return tuple(sorted(bases))


def _rebuild_artifact_time(path: pathlib.Path) -> "dt.datetime | None":
    match = _STATS_REBUILD_ARTIFACT_RE.fullmatch(path.name)
    if match is None:
        return None
    try:
        return dt.datetime.strptime(
            match.group("base").removeprefix("stats.db.rebuilding-"),
            "%Y%m%dT%H%M%S_%f",
        ).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _quarantine_incident_time(path: pathlib.Path) -> "dt.datetime | None":
    timestamp = path.name.removeprefix("stats.db-")
    try:
        if timestamp.endswith("Z"):
            return dt.datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=dt.timezone.utc
            )
        return dt.datetime.strptime(timestamp, "%Y%m%dT%H%M%S_%f").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        return None


def _has_completed_stats_quarantine_incident(
    db_path: pathlib.Path, artifacts: tuple[pathlib.Path, ...]
) -> bool:
    """Positive evidence that the same legacy rebuild removed the live family."""
    root = _cctally_core.APP_DIR / "quarantine"
    if not root.is_dir():
        return False
    artifact_times = tuple(
        timestamp
        for path in artifacts
        if (timestamp := _rebuild_artifact_time(path)) is not None
    )
    if not artifact_times:
        return False
    for manifest_path in sorted(root.glob(f"{db_path.name}-*/manifest.json")):
        if _STATS_QUARANTINE_INCIDENT_RE.fullmatch(
            manifest_path.parent.name
        ) is None:
            continue
        incident_time = _quarantine_incident_time(manifest_path.parent)
        if incident_time is None or not any(
            dt.timedelta(0) <= artifact_time - incident_time <= dt.timedelta(minutes=5)
            for artifact_time in artifact_times
        ):
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (
            manifest.get("complete") is True
            and manifest.get("originalPath") == str(db_path)
            and db_path.name in (manifest.get("movedFiles") or ())
        ):
            return True
    return False


def stats_interrupted_rebuild_evidence(
    db_path: pathlib.Path,
) -> "dict | None":
    """Read-only classification for Doctor; never creates or reclaims files."""
    db_path = pathlib.Path(db_path)
    artifacts = _stats_rebuild_artifact_bases(db_path)
    lock_path = pathlib.Path(_cctally_core.STATS_LOCK_MAINTENANCE_PATH)
    if (
        not artifacts
        or not lock_path.exists()
        or not _has_completed_stats_quarantine_incident(db_path, artifacts)
    ):
        return None
    import _cctally_journal

    high_water = _cctally_journal.journal_high_water()
    if high_water is None or high_water[1] == 0:
        return None
    lock_fh = open(lock_path, "r+")
    acquired = False
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError):
            return {
                "live": True,
                "artifacts": [path.name for path in artifacts],
                "journalHighWater": [high_water[0], high_water[1]],
            }
        return {
            "live": False,
            "artifacts": [path.name for path in artifacts],
            "journalHighWater": [high_water[0], high_water[1]],
            "destinationExists": db_path.exists(),
        }
    finally:
        if acquired:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def _remove_stale_stats_rebuild_artifacts(
    artifacts: tuple[pathlib.Path, ...],
) -> None:
    """Remove only exact scratch families, failing loudly on incomplete cleanup."""
    for artifact in artifacts:
        for suffix in ("", "-wal", "-shm"):
            candidate = pathlib.Path(f"{artifact}{suffix}")
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
    _cctally_journal = __import__("_cctally_journal")
    _cctally_journal._fsync_dir(artifacts[0].parent)
    leftovers = [
        str(pathlib.Path(f"{artifact}{suffix}"))
        for artifact in artifacts
        for suffix in ("", "-wal", "-shm")
        if pathlib.Path(f"{artifact}{suffix}").exists()
    ]
    if leftovers:
        raise OSError(
            "stale stats rebuild artifacts remain after cleanup: "
            + ", ".join(leftovers)
        )


def _recover_or_reclaim_interrupted_stats_rebuild(
    db_path: pathlib.Path, artifacts: tuple[pathlib.Path, ...]
) -> bool:
    """Recover the legacy crash shape or reclaim a proven-stale scratch.

    Caller holds maintenance EXCLUSIVE. A fully journal-consistent destination
    proves every exact scratch family stale and needs cleanup only. Rebuilding
    an absent or inconsistent destination additionally requires the completed
    prebuild-quarantine incident that distinguishes the legacy interruption
    from an unrelated file.
    """
    if not artifacts:
        return False
    import _cctally_journal

    if _cctally_db._would_block_prod_stats(db_path):
        raise _cctally_db.ProdMigrationRefused(
            "stats.db", "interrupted-rebuild-recovery"
        )
    high_water = _cctally_journal.journal_high_water()
    matching_incident = _has_completed_stats_quarantine_incident(
        db_path, artifacts
    )
    if not matching_incident:
        # Task A never removes the old destination before publication. With no
        # matching legacy prebuild-quarantine incident, exact scratch names are
        # unpublished Task A artifacts and are safe to reclaim under the
        # caller's maintenance EXCLUSIVE hold.
        #
        # The marker beside them is a separate question, decided BEFORE the
        # cleanup destroys the evidence: a scratch here need not belong to the
        # marker's run at all, because a crashed rebuild releases its flock and
        # a later run can leave its own scratch behind.
        stale_marker = _pending_stats_publication_never_replaced(db_path)
        _remove_stale_stats_rebuild_artifacts(artifacts)
        if stale_marker:
            _discard_pending_stats_publication_marker(db_path)
        else:
            _resolve_stats_publication_marker(db_path)
        return True
    if db_path.exists() and _cctally_journal.stats_index_matches_journal_prefix(
        db_path, high_water
    ):
        _remove_stale_stats_rebuild_artifacts(artifacts)
        # This branch just PROVED the destination is a fully valid
        # materialization of the journal prefix, which is strictly stronger
        # than any publication marker's own check, so the proof supersedes
        # whatever the marker recorded.
        _remove_stats_publication_marker(db_path)
        return True
    if high_water is None or high_water[1] == 0:
        return False
    ingest_fd = _cctally_journal._acquire_ingest_lock("authoritative", 10.0)
    if ingest_fd is None:
        raise _cctally_db.StatsDbMaintenanceError(
            "stats.db interrupted-rebuild recovery timed out waiting for "
            "journal ingest serialization; retry after active cctally commands exit"
        )
    try:
        with stats_write_scope("maintenance-interrupted-rebuild"):
            _cctally_journal.rebuild_stats_index(
                context=_cctally_journal.RebuildContext(
                    trigger="interrupted-rebuild-recovery"
                ),
                high_water=high_water,
            )
        _remove_stale_stats_rebuild_artifacts(artifacts)
        _discard_pending_stats_publication_marker(db_path)
        return True
    finally:
        _cctally_journal._release_ingest_lock(ingest_fd)


def stats_open_guarded(
    db_path=None, *, connect=None, recover_interruptions: bool = True
) -> sqlite3.Connection:
    """Open stats.db while excluding a destructive maintenance handshake (#386).

    The shared maintenance flock covers the marker/pending checks AND the
    connect. A destructive maintenance path owns the exclusive side; after it
    publishes its marker/pending record, no new opener can escape into the live
    family while it verifies that pre-marker handles have drained.

    ``connect`` lets a caller keep its own open mode (``mode=ro`` for
    ``db backup``, ``mode=rw`` for ``db status``) while still participating; it
    receives the path and returns the connection. Defaults to
    ``sqlite3.connect``.
    """
    db_path = pathlib.Path(
        db_path if db_path is not None else _cctally_core.DB_PATH
    )
    marker = _stats_repair_marker(db_path)
    _connect = connect if connect is not None else sqlite3.connect

    live = db_path == pathlib.Path(_cctally_core.DB_PATH)
    if not live or _cctally_core.holds_stats_maintenance():
        # Scratch target, or this context already owns the exclusive side.
        # Pre-#386 behaviour, unchanged.
        if marker.exists():
            raise _cctally_db.StatsDbMaintenanceError()
        conn = _connect(db_path)
        arm_stats_authorizer(conn)
        return conn

    pending = _cctally_db._quarantine_pending_path(db_path)
    lock_path = pathlib.Path(_cctally_core.STATS_LOCK_MAINTENANCE_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "a+")
    try:
        for _attempt in range(2):
            conn = None
            # BOUNDED, never blocking (#386 Stage 2 review P1-1) — see
            # _STATS_OPEN_MAINTENANCE_WAIT_S.
            if not _flock_bounded(
                lock_fh, fcntl.LOCK_SH, _STATS_OPEN_MAINTENANCE_WAIT_S
            ):
                raise _cctally_db.StatsDbMaintenanceError(
                    _STATS_OPEN_MAINTENANCE_TIMEOUT_MSG
                )
            if marker.exists():
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                raise _cctally_db.StatsDbMaintenanceError()
            if pending.exists():
                # Drop shared BEFORE taking exclusive so two resumers cannot
                # deadlock while upgrading; recheck under exclusive because a
                # live owner may have completed it in the gap.
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                if not _flock_bounded(
                    lock_fh, fcntl.LOCK_EX, _STATS_OPEN_RESUME_WAIT_S
                ):
                    raise _cctally_db.StatsDbMaintenanceError(
                        _STATS_OPEN_MAINTENANCE_TIMEOUT_MSG
                    )
                try:
                    if marker.exists():
                        raise _cctally_db.StatsDbMaintenanceError()
                    if pending.exists():
                        _resume_pending_quarantine(db_path)
                finally:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                continue
            artifacts = _stats_rebuild_artifact_bases(db_path)
            if (
                artifacts
                and recover_interruptions
                and _INTERRUPTED_RECOVERY_SUPPRESSED.get() == 0
            ):
                # A live rebuild owns maintenance EXCLUSIVE, so reaching this
                # shared hold proves the owner is gone. Upgrade without holding
                # shared, then re-check every fact under exclusive before any
                # file-family mutation or live connect.
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                if not _flock_bounded(
                    lock_fh, fcntl.LOCK_EX, _STATS_OPEN_RESUME_WAIT_S
                ):
                    raise _cctally_db.StatsDbMaintenanceError(
                        _STATS_OPEN_MAINTENANCE_TIMEOUT_MSG
                    )
                # Recovery calls `rebuild_stats_index`, whose in-place publisher
                # reopens the live destination through `stats_open_guarded`.
                # `flock` conflicts are per open-file-DESCRIPTION and apply
                # WITHIN a process, so without this the nested SHARED request
                # would conflict with the EXCLUSIVE hold taken on the line
                # above and time out against the branch's own lock.
                _cctally_core.note_stats_maintenance_acquired()
                recovered = False
                try:
                    current_artifacts = _stats_rebuild_artifact_bases(db_path)
                    try:
                        recovered = (
                            _recover_or_reclaim_interrupted_stats_rebuild(
                                db_path, current_artifacts
                            )
                        )
                    except (
                        _cctally_db.ProdMigrationRefused,
                        _cctally_db.StatsDbMaintenanceError,
                        # Recovery may resolve a publication marker whose run
                        # DID replace the destination; that verdict carries its
                        # own guided wording and must not be reworded into a
                        # maintenance-in-progress error.
                        _cctally_db.StatsPublicationFailedError,
                    ):
                        raise
                    except Exception as exc:
                        raise _cctally_db.StatsDbMaintenanceError(
                            "stats.db interrupted-rebuild recovery failed: "
                            f"{exc}. The best usable index was preserved; stale "
                            "artifact cleanup may be incomplete. Run "
                            "`cctally doctor`, resolve "
                            "the reported journal problem, then run "
                            "`cctally db rebuild --db stats`."
                        ) from exc
                finally:
                    _cctally_core.note_stats_maintenance_released()
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                if recovered:
                    continue
                if not _flock_bounded(
                    lock_fh, fcntl.LOCK_SH, _STATS_OPEN_MAINTENANCE_WAIT_S
                ):
                    raise _cctally_db.StatsDbMaintenanceError(
                        _STATS_OPEN_MAINTENANCE_TIMEOUT_MSG
                    )
                if marker.exists() or pending.exists():
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                    continue
            # #496 S1 F1: a durable publication marker, honoured AFTER the
            # scratch-artifact classification above so that path keeps
            # precedence. Steady state costs one stat() on a file that does not
            # exist. Suppressed exactly where interrupted recovery is, so
            # doctor's read-only gather stays read-only.
            if (
                recover_interruptions
                and _INTERRUPTED_RECOVERY_SUPPRESSED.get() == 0
                and _stats_publication_marker(db_path).exists()
            ):
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                if not _flock_bounded(
                    lock_fh, fcntl.LOCK_EX, _STATS_OPEN_RESUME_WAIT_S
                ):
                    raise _cctally_db.StatsDbMaintenanceError(
                        _STATS_OPEN_MAINTENANCE_TIMEOUT_MSG
                    )
                try:
                    _resolve_stats_publication_marker(db_path)
                finally:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                if not _flock_bounded(
                    lock_fh, fcntl.LOCK_SH, _STATS_OPEN_MAINTENANCE_WAIT_S
                ):
                    raise _cctally_db.StatsDbMaintenanceError(
                        _STATS_OPEN_MAINTENANCE_TIMEOUT_MSG
                    )
                if marker.exists() or pending.exists():
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                    continue
            try:
                conn = _connect(db_path)
                # Re-check inside the same shared hold: cheap, and it closes the
                # window between the checks above and a slow connect.
                if marker.exists() or pending.exists():
                    conn.close()
                    conn = None
                    raise _cctally_db.StatsDbMaintenanceError()
                # #386 enforcement: EVERY stats connection this module hands out
                # carries the authorizer. Arming HERE and nowhere else is what
                # keeps raw `sqlite3.connect` escape hatches (the storm suite's
                # `_grow_wal`, `db checkpoint`'s `mode=rw`) unaffected — a
                # broader arming point would make their writes unsanctioned and
                # the correct fix would then be to narrow the arming, never to
                # weaken the guard.
                arm_stats_authorizer(conn)
                return conn
            except BaseException:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                raise
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
        raise _cctally_db.StatsDbMaintenanceError()
    finally:
        lock_fh.close()


def _acquire_stats_maintenance_reentrant(path) -> "int | None":
    """Take ``stats.db.maintenance.lock`` EXCLUSIVE unless we already hold it.

    Returns the held fd, or ``None`` when THIS execution context already owns the
    lock (in which case the caller must not release anything).

    #386 Stage 2 review P1-2. ``flock`` conflicts are per open-file-DESCRIPTION
    and apply WITHIN a process: holding SHARED on one fd and then requesting
    EXCLUSIVE on a second fd of the same file blocks the process against itself,
    indefinitely. ``run_stats_ingest`` holds maintenance SHARED across its entire
    cycle, and this helper's caller — the epoch resolver — is reachable from a
    nested ``open_db()`` inside that cycle. Without this check that nested open
    is an unconditional self-deadlock. The corruption heal applies the same
    ownership-first rule through ``_acquire_stats_maintenance_for_heal``, which
    additionally BOUNDS the acquire.

    Proceeding on a shared hold is a deliberate, narrow weakening: the caller
    still runs ``_stats_family_drained`` before any physical replacement, which
    is a WHOLE-SYSTEM handle scan and therefore catches any sibling that could
    be harmed. The alternative — hanging forever — is strictly worse.
    """
    if _cctally_core.holds_stats_maintenance():
        return None
    return _heal_flock_blocking(path)


def _release_stats_maintenance_reentrant(fd: "int | None") -> None:
    """Release what ``_acquire_stats_maintenance_reentrant`` took, if anything."""
    if fd is not None:
        _heal_release_maintenance_flock(fd)


def _heal_flock_blocking(path) -> int:
    """Blocking EX flock. Both call sites target STATS_LOCK_MAINTENANCE_PATH, so
    a successful acquire also records the #386 maintenance hold — pair it with
    ``_heal_release_maintenance_flock``, never the plain release.

    Callers must reach this through ``_acquire_stats_maintenance_reentrant``, so
    a context that already owns the lock never requests it a second time.
    """
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    if str(path) == str(_cctally_core.STATS_LOCK_MAINTENANCE_PATH):
        _cctally_core.note_stats_maintenance_acquired()
    return fd


def _heal_release_maintenance_flock(fd: int) -> None:
    """Release a stats maintenance flock taken by ``_heal_flock_blocking``.

    Distinct from ``_heal_release_flock`` because that helper is also used for
    the INGEST fd, which carries no maintenance hold to unwind.
    """
    try:
        _cctally_core.note_stats_maintenance_released()
    finally:
        _heal_release_flock(fd)


_HEAL_MAINTENANCE_WAIT_S = 5.0


def _acquire_stats_maintenance_for_heal(
    timeout_s: float = _HEAL_MAINTENANCE_WAIT_S,
) -> "tuple[int | None, bool]":
    """Ownership-first, mode-aware maintenance for the corruption heal (§6).

    Returns ``(fd, True)`` when the heal may proceed — ``fd`` is ``None`` when
    an existing hold was REUSED and nothing was acquired — and ``(None,
    False)`` when the bounded acquire expired.

    **Ownership-first, not mode-first.** ``flock`` conflicts are per open-file-
    description and apply WITHIN a process, so requesting the lock a second
    time on a second descriptor blocks this process against itself whenever the
    hold it already owns is EXCLUSIVE (`_cctally_core` documents that at the
    maintenance tracker, and ``run_stats_ingest`` can hold exclusive when it
    calls ``open_db()``). The tracker is a depth counter that records THAT a
    hold exists and never which mode, and it does not need to: the rule reuses
    any hold whatever its mode, so the two cases never have to be told apart.
    Upgrading a shared hold to exclusive is the one operation that would need
    the mode, and it is exactly the second acquire that deadlocks.

    **Bounded, never blocking, when nothing is held.** The heal runs inside an
    ordinary open, and the detached worker owns maintenance EXCLUSIVE for the
    whole of its rebuild. An unbounded acquire here would make every statusline
    and dashboard open that meets corruption wait out that rebuild — the
    blocking this architecture exists to remove. A timeout means some OTHER
    holder owns it, and failing soft is correct: decline, and let a later open
    retry.
    """
    if _cctally_core.holds_stats_maintenance():
        return (None, True)
    fd = _heal_flock_bounded(
        _cctally_core.STATS_LOCK_MAINTENANCE_PATH, timeout_s
    )
    if fd is None:
        return (None, False)
    _cctally_core.note_stats_maintenance_acquired()
    return (fd, True)


def _release_stats_maintenance_for_heal(fd: "int | None") -> None:
    """Release what ``_acquire_stats_maintenance_for_heal`` took, if anything."""
    if fd is not None:
        _heal_release_maintenance_flock(fd)


def _heal_flock_bounded(path, timeout_s: float) -> "int | None":
    """Bounded EX flock. Returns the HELD fd, or ``None`` on timeout.

    #386: this previously returned the OPEN fd *without* the lock held and let
    the caller proceed "best-effort", which meant the heal path described itself
    as serialized while running unserialized — and no caller could tell the two
    outcomes apart, because both were an ``int``.

    The re-entrancy case that motivated the old behaviour is real and is NOT
    solved by simply aborting on timeout: a corruption surfacing from INSIDE a
    ``run_stats_ingest`` cycle already holds ``journal.ingest.lock``, so an
    indefinite (or fail-closed) wait would deadlock the process against itself.
    That case is now detected EXPLICITLY at the call sites via
    ``holds_ingest_lock()`` — the ingester enters ``stats_write_scope(...,
    ingest_lock=True)`` around its cycle — so a timeout here means some OTHER
    holder has it, and failing soft is correct: decline the heal and let a later
    open retry.

    Spec §5.1 says "a timeout aborts the operation rather than continuing
    unlocked". Read literally that would reintroduce the self-deadlock; the
    plan's Stage 2 correction (and this docstring) is the operative version.
    """
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return None
                time.sleep(0.02)
    except BaseException:
        os.close(fd)
        raise


def _stats_storm_test_pause(point: str) -> None:
    """Private process-control seam for the #386 stats writer-storm harness.

    Production is a zero-cost string comparison. A test arms one exact point plus
    a marker path, waits for the marker, and then drives the SIGSTOPped child
    from the parent. Mirrors `_cctally_cache._cache_storm_test_pause`, which has
    carried the cache half of this since #344.

    This is the ONLY way to hit spec section 1.1 Gap A's window deterministically:
    the instant after the handle scan says "drained" and before the first rename,
    which is exactly where a new opener must not be able to arrive.
    """
    if os.environ.get("CCTALLY_TEST_STATS_STORM_PAUSE_AT") != point:
        return
    marker = os.environ.get("CCTALLY_TEST_STATS_STORM_MARKER")
    if not marker:
        return
    pathlib.Path(marker).write_text(f"{os.getpid()}\n")
    os.kill(os.getpid(), signal.SIGSTOP)


def _stats_family_drained(path) -> "str | None":
    """``None`` when no handle is open on the stats family; else why not.

    Physical replacement renames files out from under whatever has them mapped.
    SQLite's crash guarantees stop applying at that point (spec §1.2), so the
    drain check is a precondition, not a nicety — and "the platform could not
    tell us" is a refusal, not a pass.
    """
    open_pids = _cctally_db._db_family_open_pids(path)
    if open_pids is None:
        return "could not verify that the database family has no open handles"
    if open_pids:
        return (
            "family is still open in process(es) "
            + ", ".join(str(pid) for pid in sorted(open_pids))
        )
    return None


def _heal_release_flock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _probe_stats_ok(path) -> bool:
    """Raw read-only probe (NEVER ``open_db`` — no schema/heal side effects) of
    whether stats.db opens cleanly. Used for the locked re-check."""
    if not path.exists():
        return False
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            # Force SQLite to read the database header.  A constant-only
            # ``SELECT 1`` can succeed on Linux without touching a corrupt file,
            # falsely reporting that a sibling already healed the index.
            c.execute("PRAGMA schema_version").fetchone()
        finally:
            c.close()
        return True
    except sqlite3.DatabaseError:
        return False


def _probe_stats_integrity_ok(path) -> bool:
    """Positive whole-index re-check for a post-query corruption report.

    The ordinary locked probe intentionally stays cheap because it serves the
    open-time boundary.  A dashboard leg has already observed a corruption
    error after that boundary, so its sibling-healed re-check must exercise the
    index B-trees rather than repeat ``PRAGMA schema_version``.
    """

    if not path.exists():
        return False
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = c.execute("PRAGMA quick_check").fetchall()
        finally:
            c.close()
        return rows == [("ok",)]
    except sqlite3.DatabaseError:
        return False


def _stats_heal_hook(
    store: str,
    exc: Exception,
    *,
    post_query: bool = False,
) -> bool:
    """Classifier-gated corruption auto-heal for stats.db (spec §6.3). Returns
    True when it healed (quarantined + rebuilt) OR a sibling already healed under
    the maintenance lock; False when it DECLINES — a non-corruption
    ``DatabaseError`` (BUSY / disk-full / permission), the dev-checkout-on-prod
    guard, or re-entrancy. A False return leaves ``open_db`` to raise its guided
    ``StatsDbCorruptError``.

    It can also RAISE ``_cctally_db.StatsPublicationFailedError`` (#496 S1 F1),
    and both callers depend on that: ``_cctally_tui._tui_heal_post_query_stats``
    catches it and degrades, while ``_cctally_core.open_db`` deliberately lets
    it propagate. The heal that replaced the index and then failed to validate
    it must report that itself, because ``open_db``'s decline branch would tell
    the user the database was "Not auto-recreated" — false once replacement has
    occurred."""
    global _HEAL_ACTIVE
    if store != "stats":
        return False
    if not _cctally_db._is_sqlite_corruption_error(exc):
        return False  # BUSY / disk-full / permission / SQL error — never heal
    if _HEAL_ACTIVE:
        return False
    path = _cctally_core.DB_PATH
    if _cctally_db._would_block_prod_stats(path):
        print(
            "[heal] refusing to auto-heal the prod stats.db from a dev checkout; "
            "run the installed binary or `cctally db repair --db stats --yes`.",
            file=sys.stderr,
        )
        return False
    # No journal content ⇒ nothing to rebuild FROM. A rebuild would produce an
    # EMPTY index — data loss for a pre-cutover corrupt DB whose legacy history
    # was never journaled (spec §9: `db repair` is the transitional path there).
    # Decline so open_db raises its guided StatsDbCorruptError → `db repair`.
    import _cctally_journal
    hw = _cctally_journal.journal_high_water()
    if hw is None or hw[1] == 0:
        return False
    _HEAL_ACTIVE = True
    try:
        # Ownership-first and BOUNDED (#496 S3 §6). A hold this context already
        # owns is reused whatever its mode; otherwise exclusive is acquired
        # within a bound, because the detached worker owns maintenance for the
        # whole of its rebuild and an ordinary open must never wait that out.
        maint_fd, acquired = _acquire_stats_maintenance_for_heal()
        if not acquired:
            probe = _probe_stats_integrity_ok if post_query else _probe_stats_ok
            if probe(path):
                # Some other maintenance owner already republished a readable
                # index while we waited — retry the open rather than decline.
                return True
            print(
                "[heal] stats.db auto-heal declined: another maintenance "
                "owner holds stats.db.maintenance.lock; a later open will "
                "retry.",
                file=sys.stderr,
            )
            return False
        try:
            probe = _probe_stats_integrity_ok if post_query else _probe_stats_ok
            if probe(path):
                return True  # a sibling process already healed it — retry the open
            # Forensics FIRST — before anything disturbs the evidence. The
            # trigger pair is what arms the #496 S1 forensics-time WAL capture
            # and what lets the quarantine incident name the bundle that
            # preceded it.
            #
            forensics = _cctally_db.write_corruption_forensics(
                path,
                db_label="stats",
                trigger_origin="corruption-heal",
                trigger_exception=exc,
                return_result=True,
            )
            request = _build_stats_heal_request(
                exc, forensics, post_query=post_query, high_water=hw,
            )
            # F4, first point (#496 S3 §7). Classifier gating stays a
            # PRECONDITION; this narrows within classified triggers exactly as
            # the cache path does at `_cctally_cache.py`. A disposition other
            # than CONFIRMED declines: no deferral, no worker, no replacement,
            # and a printed reason naming the bundle.
            confirmed = (
                forensics is not None
                and forensics.disposition
                is _cctally_db.CorruptionProbeDisposition.CONFIRMED
                and forensics.path is not None
            )
            if not confirmed:
                bundle = (
                    str(forensics.path)
                    if forensics is not None and forensics.path is not None
                    else "unavailable"
                )
                reason = (
                    forensics.reason if forensics is not None else "unavailable"
                )
                append_stats_heal_event({
                    **build_stats_heal_event(request, "unconfirmed"),
                    "outcome": "declined-unconfirmed",
                    "declineReason": reason,
                })
                print(
                    "[heal] stats.db auto-heal declined for classified "
                    f"trigger: corruption was not confirmed ({reason}; "
                    f"forensics: {bundle}); leaving the stats.db file family "
                    "untouched.",
                    file=sys.stderr,
                )
                return False
            append_stats_heal_event(build_stats_heal_event(request, "confirmed"))
        finally:
            # Released BEFORE deferring: the worker takes maintenance
            # EXCLUSIVE as a fresh process holding nothing, and a caller still
            # holding it here would make that acquire wait for a request it is
            # itself in the middle of filing.
            _release_stats_maintenance_for_heal(maint_fd)
        outcome = defer_stats_corruption_heal(request)
        # F15 (#496 S3 §7). Detachment supplies the timing for free: report at
        # DETECTION, naming the absolute forensics path and the heal id. It
        # cannot name an incident path, because the quarantine directory is
        # allocated only during preservation, after the worker has chosen
        # physical fallback and begun it; the worker adds that to the ring.
        bundle = request.get("forensicsPath") or "unavailable"
        print(
            f"[heal] stats.db is corrupt ({exc}); nothing was replaced by this "
            f"command. A rebuild from the journal was scheduled to run in the "
            f"background as heal {request['healId']}. Forensics: {bundle}.",
            file=sys.stderr,
        )
        # Escalation is REPORT-ONLY: no halt, and no throttle beyond the
        # admission marker's existing retry interval. Halting auto-heal after
        # N occurrences was considered and rejected (§3 Q4).
        recurrence = stats_heal_recurrence()
        if recurrence >= _STATS_HEAL_RECURRENCE_THRESHOLD:
            days = int(_STATS_HEAL_RECURRENCE_WINDOW_S // 86400)
            print(
                f"[heal] this is a recurring stats.db corruption: "
                f"{recurrence} heals in the last {days} days. The heal still "
                f"runs; the bundles in {_cctally_core.LOG_DIR} and the events "
                f"in {_stats_heal_ring_path()} are the evidence to report.",
                file=sys.stderr,
            )
        # The heal no longer runs on the caller's thread, so it no longer has
        # a boolean to return. The signal derives from `BaseException` for the
        # reason `StatsRebuildDeferred` records: a broad `except Exception`
        # fallback would turn "the index is being rebuilt" into a misleading
        # partial report.
        raise _cctally_db.StatsHealDeferred(
            outcome,
            heal_id=request["healId"],
            forensics_path=request.get("forensicsPath"),
        )
    except Exception as heal_exc:
        print(f"[heal] stats.db auto-heal failed: {heal_exc}", file=sys.stderr)
        # A post-publication validation failure has ALREADY replaced the index.
        # Declining here sends `open_db` to its pre-existing branch, which tells
        # the user the database was "Not auto-recreated" and to run
        # `db repair --db stats --yes` — both false once replacement occurred.
        # The durable marker makes the NEXT process say the right thing; the
        # process that caused the failure must say it too (#496 S1 F1).
        # Narrow by construction: it fires only on a `failed` marker, so every
        # other heal failure keeps its existing behaviour.
        _raise_settled_publication_failure(path)
        return False
    finally:
        _HEAL_ACTIVE = False


HEAL_HOOK = _stats_heal_hook


# --------------------------------------------------------------------------
# #496 S3 §6 — the detached corruption heal
# --------------------------------------------------------------------------
#
# The hook writes forensics and files a REQUEST; a detached worker does the
# rebuild. Admission copies the three layers of `defer_stats_epoch_rebuild` — a
# non-blocking admission flock whose loser returns immediately, a pending
# marker with a retry window, and a worker-active probe that refreshes the
# marker instead of spawning a duplicate — over its OWN files, so the two
# deferrals can never suppress each other.
#
# The epoch path's marker is an empty touched file. This one is a durable JSON
# document, because the worker runs later and in another process and needs
# facts the hook established at detection: the heal id that correlates the
# durable event record, the trigger evidence, the forensics bundle, the
# journal information to revalidate, and — load-bearing — WHICH PROBE to run.
# A `post_query` detection was established by a failed `quick_check` against a
# file SQLite opens happily, so a worker that always used the cheap readability
# probe would exit on exactly the readable-but-corrupt population this
# architecture exists to serve.

STATS_CORRUPTION_HEAL_COMMAND = "_stats-corruption-heal"
_STATS_HEAL_RETRY_SECONDS = 60.0
_STATS_HEAL_WORKER_MAINTENANCE_WAIT_S = 120.0
_STATS_HEAL_PROBE_INTEGRITY = "integrity"
_STATS_HEAL_PROBE_READABILITY = "readability"


def _stats_heal_path(name: str) -> pathlib.Path:
    return pathlib.Path(_cctally_core.APP_DIR) / name


def _stats_heal_marker_path() -> pathlib.Path:
    return _stats_heal_path("stats-corruption-heal.pending")


def _stats_heal_admission_path() -> pathlib.Path:
    return _stats_heal_path("stats-corruption-heal.admission.lock")


def _stats_heal_worker_path() -> pathlib.Path:
    return _stats_heal_path("stats-corruption-heal.worker.lock")


def _stats_heal_log_path() -> pathlib.Path:
    return pathlib.Path(_cctally_core.LOG_DIR) / "stats-corruption-heal.log"


def _new_heal_id() -> str:
    """A collision-free correlation id readable in a log line."""
    return (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + os.urandom(4).hex()
    )


def _build_stats_heal_request(
    exc: BaseException,
    forensics,
    *,
    post_query: bool,
    high_water,
) -> dict:
    """The durable request the worker acts on (#496 S3 §6)."""
    return {
        "schemaVersion": 1,
        "healId": _new_heal_id(),
        "detectedAtUtc": _cctally_core.now_utc_iso(),
        "postQuery": bool(post_query),
        "probeKind": (
            _STATS_HEAL_PROBE_INTEGRITY
            if post_query
            else _STATS_HEAL_PROBE_READABILITY
        ),
        "triggerError": _cctally_db._bounded_forensics_text(
            exc, _cctally_db._FORENSICS_EXCEPTION_MESSAGE_MAX
        ),
        "triggerType": type(exc).__name__,
        "forensicsPath": (
            str(forensics.path)
            if forensics is not None and forensics.path is not None
            else None
        ),
        "forensicsDisposition": (
            forensics.disposition.value if forensics is not None else None
        ),
        "journalHighWater": (
            [str(high_water[0]), int(high_water[1])]
            if high_water is not None
            else None
        ),
    }


def _read_stats_heal_request() -> "dict | None":
    try:
        payload = json.loads(_stats_heal_marker_path().read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _unlink_stats_heal_marker() -> None:
    try:
        _stats_heal_marker_path().unlink()
    except FileNotFoundError:
        pass


def _stats_heal_worker_active() -> bool:
    """Probe the worker flock without waiting or disturbing its owner."""
    try:
        fd = os.open(
            _stats_heal_worker_path(), os.O_WRONLY | os.O_CREAT, 0o600
        )
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)


def _log_stats_heal(
    outcome: str, *, heal_id: "str | None" = None,
    error: BaseException | None = None,
) -> None:
    """Append one path-safe worker result line.

    Follows `stats-epoch-rebuild.log`'s restraint for exception text: the class
    plus a numeric SQLite/OS code, never free-form message text that may carry
    private paths. The heal id is our own generated token and carries nothing.
    """
    try:
        log_path = _stats_heal_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        detail = ""
        if heal_id:
            detail += f" heal={heal_id}"
        if error is not None:
            code = getattr(error, "sqlite_errorcode", None)
            if code is None:
                code = getattr(error, "errno", None)
            detail += f" error={type(error).__name__}"
            if code is not None:
                detail += f" code={int(code)}"
        line = (
            f"{_cctally_core.now_utc_iso()} worker=stats-corruption-heal "
            f"result={outcome}{detail}\n"
        ).encode("utf-8")
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        pass


def defer_stats_corruption_heal(request: dict) -> str:
    """Schedule one retryable detached corruption heal without blocking."""
    try:
        pathlib.Path(_cctally_core.APP_DIR).mkdir(parents=True, exist_ok=True)
        admission_fd = os.open(
            _stats_heal_admission_path(), os.O_WRONLY | os.O_CREAT, 0o600
        )
    except OSError:
        return "failed"
    try:
        try:
            fcntl.flock(admission_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "pending"
        marker = _stats_heal_marker_path()
        try:
            age = time.time() - marker.stat().st_mtime
        except FileNotFoundError:
            age = None
        except OSError:
            return "failed"
        if age is not None and age < _STATS_HEAL_RETRY_SECONDS:
            return "pending"
        if _stats_heal_worker_active():
            # A real rebuild outlives the marker retry interval. Refresh the
            # admission stamp instead of launching a process that can only lose
            # the worker flock and exit.
            try:
                os.utime(marker, None)
            except OSError:
                pass
            return "pending"
        try:
            _cctally_db._atomic_write_private_json(marker, request)
        except OSError:
            return "failed"
        from _cctally_update import _spawn_detached
        if _spawn_detached(STATS_CORRUPTION_HEAL_COMMAND):
            return "spawned"
        _unlink_stats_heal_marker()
        return "failed"
    finally:
        try:
            fcntl.flock(admission_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(admission_fd)


def _run_stats_corruption_heal(request: dict) -> str:
    """The worker's body, under its own maintenance-EXCLUSIVE hold.

    Three orderings here are load-bearing (#496 S3 §6):

    * the authoritative probe runs UNDER exclusive, not before it, because
      epoch rebuilds, operator rebuilds and other maintenance classes hold
      distinct worker flocks and would otherwise race it;
    * the no-journal guard is re-checked under the lock, because the hook
      checked it before spawning and `rebuild_stats_index` accepts a `None`
      high-water and would build an EMPTY scratch — rebuilding a pre-cutover
      index to empty is exactly the silent data loss that guard exists to
      prevent;
    * the probe is the one the DETECTION established, carried in the request.
    """
    import _cctally_journal

    path = _cctally_core.DB_PATH
    heal_id = str(request.get("healId") or "")
    if _cctally_db._would_block_prod_stats(path):
        return "prod-refused"
    maint_fd = _heal_flock_bounded(
        _cctally_core.STATS_LOCK_MAINTENANCE_PATH,
        _STATS_HEAL_WORKER_MAINTENANCE_WAIT_S,
    )
    if maint_fd is None:
        return "maintenance-busy"
    _cctally_core.note_stats_maintenance_acquired()
    try:
        probe = (
            _probe_stats_integrity_ok
            if str(request.get("probeKind") or "")
            == _STATS_HEAL_PROBE_INTEGRITY
            else _probe_stats_ok
        )
        if probe(path):
            # F4's second point: a re-probe under a lock the hook never held
            # finds the index intact, so nothing is replaced.
            return "declined-readable"
        high_water = _cctally_journal.journal_high_water()
        if high_water is None or high_water[1] == 0:
            return "declined-no-journal"
        if holds_ingest_lock():
            ingest_fd = None
        else:
            ingest_fd = _heal_flock_bounded(
                _cctally_core.JOURNAL_INGEST_LOCK_PATH, 10.0
            )
            if ingest_fd is None:
                return "ingest-busy"
        try:
            with stats_write_scope("maintenance-heal"):
                result = _cctally_journal.rebuild_stats_index(
                    context=_cctally_journal.RebuildContext(
                        trigger="corruption-heal",
                        trigger_error=str(request.get("triggerError") or ""),
                        forensics_path=request.get("forensicsPath"),
                    ),
                    high_water=high_water,
                )
        finally:
            if ingest_fd is not None:
                _heal_release_flock(ingest_fd)
        _record_stats_heal_outcome(heal_id, "rebuilt", result=result)
        return "success"
    finally:
        _heal_release_maintenance_flock(maint_fd)


def cmd_stats_corruption_heal_internal(args) -> int:
    """Hidden detached worker: heal one corrupt stats index exactly once."""
    del args
    try:
        pathlib.Path(_cctally_core.APP_DIR).mkdir(parents=True, exist_ok=True)
        worker_fd = os.open(
            _stats_heal_worker_path(), os.O_WRONLY | os.O_CREAT, 0o600
        )
    except OSError as exc:
        _log_stats_heal("error", error=exc)
        return 0
    try:
        try:
            fcntl.flock(worker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        request = _read_stats_heal_request()
        if not request:
            _unlink_stats_heal_marker()
            _log_stats_heal("no-request")
            return 0
        heal_id = str(request.get("healId") or "")
        try:
            outcome = _run_stats_corruption_heal(request)
        except Exception as exc:
            # Retryable: the marker stays so a later detection is admitted
            # once its retry window expires.
            _record_stats_heal_outcome(heal_id, "failed", error=exc)
            _log_stats_heal("error", heal_id=heal_id, error=exc)
            return 0
        if outcome in ("maintenance-busy", "ingest-busy"):
            _log_stats_heal(outcome, heal_id=heal_id)
            return 0
        if outcome != "success":
            _record_stats_heal_outcome(heal_id, outcome)
        _unlink_stats_heal_marker()
        _log_stats_heal(outcome, heal_id=heal_id)
        return 0
    finally:
        try:
            fcntl.flock(worker_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(worker_fd)


# --------------------------------------------------------------------------
# F6 — the bounded durable heal ring (#496 S3 §7)
# --------------------------------------------------------------------------
#
# Stderr alone does not work as the accountability channel: the statusline's
# background writer forks with stderr at `/dev/null` and wraps its body in
# `except BaseException: pass`, so a heal firing from there is invisible. The
# ring is the durable channel; the stderr line remains for interactive callers.
#
# **A non-blocking flock is wrong here and would defeat the guarantee.** The
# writer-guard log may drop a line under contention because it is advisory;
# this ring is the only durable notification that a heal happened, so a loser
# that silently discarded its event would make the accountability claim false.
# The acquire is therefore a BOUNDED WAIT, and an expiry is reported rather
# than swallowed.
#
# It holds absolute paths because F15 requires the user be told them, so it
# stays a private `0600` file in the user's own data directory, like the
# incident `manifest.json` beside it. Bounded by COUNT so it cannot grow, which
# is also what makes it survive S6's retention by construction.

_STATS_HEAL_RING_CAPACITY = 50
_STATS_HEAL_RING_WAIT_S = 10.0
_STATS_HEAL_RECURRENCE_THRESHOLD = 3
_STATS_HEAL_RECURRENCE_WINDOW_S = 7 * 86400.0


def _stats_heal_ring_path() -> pathlib.Path:
    return pathlib.Path(_cctally_core.LOG_DIR) / "stats-heal-events.json"


def _stats_heal_ring_lock_path() -> pathlib.Path:
    return pathlib.Path(_cctally_core.LOG_DIR) / "stats-heal-events.lock"


def build_stats_heal_event(request: dict, disposition: str) -> dict:
    """One ring entry, as the DETECTION knows it.

    `changed` is `unknown` and stays that way. `RebuildResult` carries row
    counts and replay diagnostics but no comparison against the index it
    replaced, and `conflicts` / `protocol_violations` report replay ambiguity
    and omitted correction batches — which is not "the rebuilt index differs
    from the live one". Recording them is still required, because a user is
    entitled to know a rebuild reported conflicts.

    `incidentPath` is `None` here and can only be `None` here: the quarantine
    directory is allocated during preservation, after the worker has chosen
    physical fallback and begun it (#496 S3 §7 F15).
    """
    return {
        "schemaVersion": 1,
        "healId": str(request.get("healId") or ""),
        "detectedAtUtc": str(
            request.get("detectedAtUtc") or _cctally_core.now_utc_iso()
        ),
        "updatedAtUtc": _cctally_core.now_utc_iso(),
        "trigger": {
            "origin": "corruption-heal",
            "type": request.get("triggerType"),
            "error": request.get("triggerError"),
            "postQuery": bool(request.get("postQuery")),
        },
        "disposition": disposition,
        "forensicsPath": request.get("forensicsPath"),
        "incidentPath": None,
        "publicationMechanism": None,
        "outcome": "detected",
        "changed": "unknown",
    }


def _report_unreadable_stats_heal_ring(reason: str) -> None:
    """Report a ring file that exists but cannot be read as a ring.

    A ring that reads as empty is indistinguishable from a ring that never
    recorded anything, and the next writer overwrites it — so without this the
    accountability history would disappear with nothing said. Both channels are
    used because neither reaches every caller: the worker's streams are
    `/dev/null`, and an interactive caller does not read the heal log.
    """
    _log_stats_heal(f"ring-unreadable-{reason}")
    print(
        f"[heal] the stats.db heal event log at {_stats_heal_ring_path()} "
        f"could not be read ({reason}) and reports no history; the next "
        "recorded heal replaces it.",
        file=sys.stderr,
    )


def _read_stats_heal_ring() -> list:
    try:
        payload = json.loads(_stats_heal_ring_path().read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        _report_unreadable_stats_heal_ring(type(exc).__name__)
        return []
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        _report_unreadable_stats_heal_ring("NoEventList")
        return []
    return [e for e in events if isinstance(e, dict)]


def read_stats_heal_events() -> list:
    """Every retained heal event, oldest first. Public: S6's F14 reads this."""
    return _read_stats_heal_ring()


def _write_stats_heal_ring(events: list) -> None:
    _cctally_db._atomic_write_private_json(
        _stats_heal_ring_path(),
        {"schemaVersion": 1, "events": events[-_STATS_HEAL_RING_CAPACITY:]},
    )


def _mutate_stats_heal_ring(mutate) -> bool:
    """Read-modify-write the ring under a BOUNDED wait for its lock."""
    try:
        pathlib.Path(_cctally_core.LOG_DIR).mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    fd = _heal_flock_bounded(
        _stats_heal_ring_lock_path(), _STATS_HEAL_RING_WAIT_S
    )
    if fd is None:
        # Loud, never silent: the ring is the accountability guarantee, so a
        # writer that could not take the lock says so rather than discarding
        # its event.
        print(
            "[heal] could not record a stats.db heal event: the heal event "
            "log stayed locked; the heal itself is unaffected.",
            file=sys.stderr,
        )
        return False
    try:
        events = _read_stats_heal_ring()
        mutated = mutate(events)
        if mutated is None:
            return False
        _write_stats_heal_ring(mutated)
        return True
    except OSError:
        return False
    finally:
        _heal_release_flock(fd)


def append_stats_heal_event(entry: dict) -> bool:
    """Append one detection entry. Bounded by count, oldest dropped first."""
    def mutate(events):
        events.append(entry)
        return events

    return _mutate_stats_heal_ring(mutate)


def update_stats_heal_event(heal_id: str, **fields) -> bool:
    """Update the entry MATCHING ``heal_id``, and no other.

    Admission coalesces several detections into one run, so an update keyed by
    anything else (position, recency) would settle a heal whose worker never
    ran and hide the one that died.
    """
    if not heal_id:
        return False

    def mutate(events):
        for event in events:
            if event.get("healId") == heal_id:
                event.update(fields)
                event["updatedAtUtc"] = _cctally_core.now_utc_iso()
                return events
        return None

    return _mutate_stats_heal_ring(mutate)


def stats_heal_recurrence(
    window_s: float = _STATS_HEAL_RECURRENCE_WINDOW_S,
) -> int:
    """How many heals were detected inside the trailing window."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=window_s)
    count = 0
    for event in _read_stats_heal_ring():
        try:
            detected = dt.datetime.fromisoformat(
                str(event.get("detectedAtUtc") or "").replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if detected.tzinfo is None:
            detected = detected.replace(tzinfo=dt.timezone.utc)
        if detected >= cutoff:
            count += 1
    return count


def _record_stats_heal_outcome(
    heal_id: str, outcome: str, *, result=None, error: BaseException | None = None,
) -> None:
    """Settle the durable heal event this worker owns."""
    fields: dict = {"outcome": outcome}
    if result is not None:
        incident = getattr(result, "quarantine_dir", None)
        fields["incidentPath"] = str(incident) if incident is not None else None
        # An in-place publish never preserves, so `quarantine_dir is None` is
        # an exact discriminator for which mechanism published (#496 S3 §4.1).
        fields["publicationMechanism"] = (
            "replace" if incident is not None else "in_place"
        )
        fields["conflicts"] = len(getattr(result, "conflicts", ()) or ())
        fields["protocolViolations"] = len(
            getattr(result, "protocol_violations", ()) or ()
        )
        fields["rowsTotal"] = sum(
            int(v) for v in (getattr(result, "rows_by_table", {}) or {}).values()
        )
    if error is not None:
        # Structural only, like `stats-epoch-rebuild.log`: never free-form
        # exception text, which can carry private paths.
        fields["error"] = type(error).__name__
    if not update_stats_heal_event(heal_id, **fields):
        # The ring is the accountability record this session added, so a
        # verdict that never reached it must not vanish silently. The durable
        # log is the channel that works here: this runs only in the detached
        # worker, whose stdout and stderr are `/dev/null`.
        _log_stats_heal(f"ring-update-lost-{outcome}", heal_id=heal_id)


# --------------------------------------------------------------------------
# §7.1 stats.db epoch-mismatch resolution (Task 9)
# --------------------------------------------------------------------------
#
# A stats.db whose ``user_version`` is neither legacy (<= LEGACY_STATS_HEAD) nor
# the current ``STATS_INDEX_EPOCH`` — a future epoch written by a newer binary,
# or any stray value > 13 — resolves by journal REBUILD (spec §7.1). This is
# DISJOINT from the corruption heal path: the DB is readable, only its version is
# wrong. The version-ahead DB is quarantined (nothing deleted), then a fresh
# index is rebuilt from the journal and swapped in. A mismatch with NO journal is
# a HARD ERROR (``StatsEpochMismatchError``) — never a silent rebuild-to-empty.

_EPOCH_MISMATCH_ACTIVE = False
STATS_EPOCH_REBUILD_COMMAND = "_stats-epoch-rebuild"
_STATS_EPOCH_REBUILD_RETRY_SECONDS = 60.0


def _stats_epoch_rebuild_path(name: str) -> pathlib.Path:
    return pathlib.Path(_cctally_core.APP_DIR) / name


def _stats_epoch_rebuild_marker_path() -> pathlib.Path:
    return _stats_epoch_rebuild_path("stats-epoch-rebuild.pending")


def _stats_epoch_rebuild_admission_path() -> pathlib.Path:
    return _stats_epoch_rebuild_path("stats-epoch-rebuild.admission.lock")


def _stats_epoch_rebuild_worker_path() -> pathlib.Path:
    return _stats_epoch_rebuild_path("stats-epoch-rebuild.worker.lock")


def _stats_epoch_rebuild_log_path() -> pathlib.Path:
    return pathlib.Path(_cctally_core.LOG_DIR) / "stats-epoch-rebuild.log"


def _unlink_stats_epoch_marker() -> None:
    try:
        _stats_epoch_rebuild_marker_path().unlink()
    except FileNotFoundError:
        pass


def _stats_epoch_rebuild_worker_active() -> bool:
    """Probe the worker flock without waiting or disturbing its owner."""
    try:
        fd = os.open(
            _stats_epoch_rebuild_worker_path(),
            os.O_WRONLY | os.O_CREAT,
            0o600,
        )
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)


def _log_stats_epoch_rebuild(outcome: str, *, error: Exception | None = None) -> None:
    """Append one path-safe worker result line.

    Detached worker streams are `/dev/null`; this small log is its only
    diagnostic. Error detail is deliberately structural (class plus numeric
    SQLite/OS code), never free-form exception text that may carry private
    paths or `key=value` fragments.
    """
    try:
        log_path = _stats_epoch_rebuild_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        detail = ""
        if error is not None:
            code = getattr(error, "sqlite_errorcode", None)
            if code is None:
                code = getattr(error, "errno", None)
            detail = f" error={type(error).__name__}"
            if code is not None:
                detail += f" code={int(code)}"
        line = (
            f"{_cctally_core.now_utc_iso()} worker=stats-epoch-rebuild "
            f"result={outcome}{detail}\n"
        ).encode("utf-8")
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        pass


def defer_stats_epoch_rebuild() -> str:
    """Schedule one retryable detached epoch worker without blocking callers."""
    try:
        pathlib.Path(_cctally_core.APP_DIR).mkdir(parents=True, exist_ok=True)
        admission_fd = os.open(
            _stats_epoch_rebuild_admission_path(),
            os.O_WRONLY | os.O_CREAT,
            0o600,
        )
    except OSError:
        return "failed"
    try:
        try:
            fcntl.flock(admission_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "pending"
        marker = _stats_epoch_rebuild_marker_path()
        try:
            age = time.time() - marker.stat().st_mtime
        except FileNotFoundError:
            age = None
        except OSError:
            return "failed"
        if age is not None and age < _STATS_EPOCH_REBUILD_RETRY_SECONDS:
            return "pending"
        if _stats_epoch_rebuild_worker_active():
            # A representative replay outlives the marker retry interval.
            # Refresh the admission stamp instead of launching a process that
            # can only lose the worker flock and exit.
            try:
                os.utime(marker, None)
            except OSError:
                pass
            return "pending"
        try:
            marker_fd = os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(marker_fd)
            os.utime(marker, None)
        except OSError:
            return "failed"
        from _cctally_update import _spawn_detached
        if _spawn_detached(STATS_EPOCH_REBUILD_COMMAND):
            return "spawned"
        _unlink_stats_epoch_marker()
        return "failed"
    finally:
        try:
            fcntl.flock(admission_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(admission_fd)


def cmd_stats_epoch_rebuild_internal(args) -> int:
    """Hidden detached worker: converge a pending stats epoch exactly once."""
    del args
    try:
        pathlib.Path(_cctally_core.APP_DIR).mkdir(parents=True, exist_ok=True)
        worker_fd = os.open(
            _stats_epoch_rebuild_worker_path(),
            os.O_WRONLY | os.O_CREAT,
            0o600,
        )
    except OSError as exc:
        _log_stats_epoch_rebuild("error", error=exc)
        return 0
    try:
        try:
            fcntl.flock(worker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        if not stats_epoch_rebuild_pending():
            _unlink_stats_epoch_marker()
            _log_stats_epoch_rebuild("current")
            return 0
        try:
            conn = resolve_stats_epoch_mismatch()
            conn.close()
        except Exception as exc:
            _log_stats_epoch_rebuild("error", error=exc)
            return 0
        _unlink_stats_epoch_marker()
        _log_stats_epoch_rebuild("success")
        return 0
    finally:
        try:
            fcntl.flock(worker_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(worker_fd)


def _raw_user_version(path) -> int:
    """Read ``PRAGMA user_version`` via a raw RO connect (no open_db side
    effects). -1 when the file cannot be read."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return c.execute("PRAGMA user_version").fetchone()[0]
        finally:
            c.close()
    except sqlite3.DatabaseError:
        return -1


def stats_epoch_rebuild_pending(path=None) -> bool:
    """Whether an ordinary live open would require whole-journal replay.

    Missing indexes are cheap fresh installs. Unreadable indexes belong to the
    corruption classifier, and legacy indexes take the one-time cutover path.
    Only a readable post-legacy index at a non-current epoch is deferrable.
    """
    candidate = pathlib.Path(
        _cctally_core.DB_PATH if path is None else path
    )
    try:
        if not candidate.exists():
            return False
    except OSError:
        return False
    version = _raw_user_version(candidate)
    if version < 0 or version <= _cctally_core.LEGACY_STATS_HEAD:
        return False
    return version != _cctally_core.STATS_INDEX_EPOCH


def resolve_stats_epoch_mismatch():
    """Resolve a version-mismatched stats.db by journal rebuild (spec §7.1).
    Called by ``open_db`` after ``conn.close()`` — returns a fresh steady-state
    ``open_db()`` connection over the rebuilt index."""
    global _EPOCH_MISMATCH_ACTIVE
    import _cctally_journal

    path = _cctally_core.DB_PATH
    if _cctally_db._would_block_prod_stats(path):
        # A dev/worktree binary must not rebuild the real prod stats.db (#146).
        raise _cctally_db.ProdMigrationRefused("stats.db", "epoch-rebuild")
    if _EPOCH_MISMATCH_ACTIVE:
        raise _cctally_db.StatsEpochMismatchError(
            "stats.db epoch rebuild did not converge to epoch "
            f"{_cctally_core.STATS_INDEX_EPOCH}; refusing to loop. Inspect the "
            "journal and run `cctally db rebuild --db stats`.")
    _EPOCH_MISMATCH_ACTIVE = True
    try:
        maint_fd = _acquire_stats_maintenance_reentrant(
            _cctally_core.STATS_LOCK_MAINTENANCE_PATH)
        try:
            # Locked re-check: a sibling process may have already rebuilt it.
            if _raw_user_version(path) != _cctally_core.STATS_INDEX_EPOCH:
                hw, journal_has_bytes = (
                    _cctally_journal._journal_rebuild_snapshot()
                )
                if hw is None or not journal_has_bytes:
                    raise _cctally_db.StatsEpochMismatchError(
                        f"stats.db is at index epoch {_raw_user_version(path)}, "
                        f"but this cctally builds epoch "
                        f"{_cctally_core.STATS_INDEX_EPOCH} and no journal is "
                        "present to rebuild from. The journal/ directory is the "
                        "durable source — restore it from backup, then run "
                        "`cctally db rebuild --db stats`.")
                # Total lock order is maintenance -> ingest. The epoch bump can
                # be discovered by an ordinary open or an ingest caller, so it
                # must take the ingest lock here before building and publishing
                # the replacement; callers must never enter this resolver while
                # already holding that later lock. #386: if this context DOES
                # already hold it, say so rather than deadlocking against
                # ourselves.
                if holds_ingest_lock():
                    ingest_fd = None
                else:
                    ingest_fd = _cctally_journal._acquire_ingest_lock(
                        "authoritative", 10.0
                    )
                    if ingest_fd is None:
                        raise _cctally_db.StatsEpochMismatchError(
                            "timed out waiting for journal ingest serialization "
                            "during stats.db epoch rebuild"
                        )
                try:
                    # Append the idempotent account coordinator input, then let
                    # the common rebuild cutover preserve the version-ahead
                    # family and atomically publish the current epoch.
                    with stats_write_scope("maintenance-epoch"):
                        _cctally_journal.run_epoch_transition()
                finally:
                    if ingest_fd is not None:
                        _cctally_journal._release_ingest_lock(ingest_fd)
        finally:
            _release_stats_maintenance_reentrant(maint_fd)
        return _cctally_core.open_db()
    finally:
        _EPOCH_MISMATCH_ACTIVE = False
