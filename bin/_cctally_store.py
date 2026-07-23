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
by the storm test): maintenance flocks → ``journal.ingest.lock`` → provider
flocks (cache Claude → cache Codex → conversations Claude → conversations
Codex) → SQLite transactions → ``journal.lock`` (leaf). Never acquire the
ingest lock while holding a provider flock; no SQLite write transaction ever
spans a flock acquisition.

**Raw-connect escape hatches stay OUT of this module by design** (spec §6.1):
``db checkpoint``'s ``mode=rw`` connect, ``db vacuum``'s exclusive connect, and
doctor's read-only gather deliberately bypass the opener so they carry no
schema-apply / migration side effects on maintenance/diagnostic paths.
"""
from __future__ import annotations

import fcntl
import os
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
_STATS_OPEN_FIXUPS_VERSION = 1


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


def _heal_flock_blocking(path) -> int:
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _heal_flock_bounded(path, timeout_s: float) -> int:
    """Bounded EX flock: return the held fd, or (on timeout) the OPEN fd WITHOUT
    the lock held — the caller proceeds best-effort (see the module note)."""
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
                    return fd
                time.sleep(0.02)
    except BaseException:
        os.close(fd)
        raise


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


def _stats_heal_hook(store: str, exc: Exception) -> bool:
    """Classifier-gated corruption auto-heal for stats.db (spec §6.3). Returns
    True when it healed (quarantined + rebuilt) OR a sibling already healed under
    the maintenance lock; False when it DECLINES — a non-corruption
    ``DatabaseError`` (BUSY / disk-full / permission), the dev-checkout-on-prod
    guard, or re-entrancy. A False return leaves ``open_db`` to raise its guided
    ``StatsDbCorruptError``."""
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
        maint_fd = _heal_flock_blocking(_cctally_core.STATS_LOCK_MAINTENANCE_PATH)
        try:
            if _probe_stats_ok(path):
                return True  # a sibling process already healed it — retry the open
            _cctally_db.write_corruption_forensics(path, db_label="stats")
            ingest_fd = _heal_flock_bounded(
                _cctally_core.JOURNAL_INGEST_LOCK_PATH, 5.0)
            try:
                _cctally_db.quarantine_db_family(path)
                import _cctally_journal
                _cctally_journal.rebuild_stats_index()
            finally:
                _heal_release_flock(ingest_fd)
            print(
                f"[heal] stats.db was corrupt ({exc}); quarantined its file family "
                "under quarantine/ (forensics in logs/) and rebuilt a fresh index "
                "from the journal.",
                file=sys.stderr,
            )
            return True
        finally:
            _heal_release_flock(maint_fd)
    except Exception as heal_exc:
        print(f"[heal] stats.db auto-heal failed: {heal_exc}", file=sys.stderr)
        return False
    finally:
        _HEAL_ACTIVE = False


HEAL_HOOK = _stats_heal_hook


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
        maint_fd = _heal_flock_blocking(_cctally_core.STATS_LOCK_MAINTENANCE_PATH)
        try:
            # Locked re-check: a sibling process may have already rebuilt it.
            if _raw_user_version(path) != _cctally_core.STATS_INDEX_EPOCH:
                hw = _cctally_journal.journal_high_water()
                if hw is None or hw[1] == 0:
                    raise _cctally_db.StatsEpochMismatchError(
                        f"stats.db is at index epoch {_raw_user_version(path)}, "
                        f"but this cctally builds epoch "
                        f"{_cctally_core.STATS_INDEX_EPOCH} and no journal is "
                        "present to rebuild from. The journal/ directory is the "
                        "durable source — restore it from backup, then run "
                        "`cctally db rebuild --db stats`.")
                # Preserve the version-ahead DB (nothing deleted), then rebuild a
                # fresh epoch index into the now-absent destination.
                _cctally_db.quarantine_db_family(path)
                _cctally_journal.rebuild_stats_index()
        finally:
            _heal_release_flock(maint_fd)
        return _cctally_core.open_db()
    finally:
        _EPOCH_MISMATCH_ACTIVE = False
