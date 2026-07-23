"""Stats.db / cache.db migration framework, dispatcher, error-banner render, `cctally db` subcommands.

Eager I/O sibling: bin/cctally loads this at startup. The framework
registers its three production stats.db migration handlers
(`001_five_hour_block_models_backfill_v1`,
`002_five_hour_block_projects_backfill_v1`,
`003_merge_5h_block_duplicates_v1`) and the conditional test-only
migrations (gated on `CCTALLY_MIGRATION_TEST_MODE=1` +
`HARNESS_FAKE_HOME_BASE`) at module-load time. Subsequent imports
through `_load_sibling` hit `sys.modules` cache and reuse the
populated registry — re-imports under SourceFileLoader DO NOT
re-execute the decorator chain, which preserves the
"registry length == NNN" invariant per-DB.

Holds:
- ``_MIGRATION_IDENT_RE``, ``add_column_if_missing`` — idempotent
  column-shape guard. Used by ``open_db`` / ``open_cache_db`` from
  bin/cctally via the eager re-export.
- ``_MIGRATION_NAME_RE``, ``Migration`` (frozen dataclass),
  ``DowngradeDetected`` exception, ``_STATS_MIGRATIONS`` /
  ``_CACHE_MIGRATIONS`` registries, ``_make_migration_decorator``,
  ``stats_migration`` / ``cache_migration`` decorator factories,
  ``_LEGACY_MARKER_ALIASES_BY_DB`` + ``_bootstrap_rename_legacy_markers``,
  ``_run_pending_migrations`` (the dispatcher).
- The three production migration handlers + the test-only
  migration-registration block.
- ``_log_migration_error``, ``_clear_migration_error_log_entries``,
  ``_render_migration_error_banner``,
  ``_print_migration_error_banner_if_needed`` — the migration-error
  sentinel surface (single source of truth per CLAUDE.md gotcha).
- ``cmd_db_status`` / ``cmd_db_skip`` / ``cmd_db_unskip`` plus
  helpers (``_db_status_for``, ``_db_status_failed_names_from_log``,
  ``_db_status_format_row``, ``_db_resolve_migration_name``,
  ``_db_path_for_label``).

What stays in bin/cctally (reached via the ``_cctally()`` accessor):
- ``open_db`` / ``open_cache_db`` — DB-open primitives that CALL
  the dispatcher; they're the boundary owners, not internal to the
  migration system.
- ``_compute_block_totals`` — Z-high callable consumed by migration
  handlers; reached via the back-ref shim at the top of this module.

Path constants reached via ``_cctally_core.X`` at call time:
``DB_PATH`` / ``CACHE_DB_PATH`` / ``LOG_DIR`` /
``MIGRATION_ERROR_LOG_PATH``. After the data-globals promotion
(2026-05-22, issue #84) ``_cctally_core`` is the single source of
truth and the only legal monkeypatch target; tests redirecting
``HOME`` via ``redirect_paths`` propagate without a sibling-side
seed block in ``bin/cctally``.

Kernel helpers (``now_utc_iso``, ``parse_iso_datetime``, ``eprint``)
are direct-imported from ``_cctally_core`` per spec §3.3.

§5.6 audit: zero monkeypatch sites on any moved symbol — the
extraction is pure-mechanical. No Option C call-site rewrites
required for test propagation.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import enum
import fcntl
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17-cctally-core-kernel-extraction.md §3.3: kernel symbols
# import from _cctally_core. The legacy shim functions for these names
# are deleted.
import _cctally_core
from _cctally_core import (
    eprint,
    now_utc_iso,
    parse_iso_datetime,
)

# #279 S2 F2: demonstration adopter of the stdlib-logging chokepoint —
# the migration gate-defer diagnostic routes through _lib_log so
# CCTALLY_DEBUG verbosity is decided in one place.
import _lib_log

# Stats migration 008 needs the same per-entry cost computation used by
# the live cost-report path. Direct import keeps the kernel single-sourced
# (no shim drift); _lib_pricing is a stdlib-only leaf module so no cycle
# risk. Other siblings (_cctally_record, _cctally_dashboard) follow the
# same direct-import pattern.
from _lib_pricing import _calculate_entry_cost


# Module-level back-ref shim for the one Z-high callable that STAYS in
# bin/cctally. Resolves `sys.modules['cctally'].X` at CALL TIME (not
# bind time), so monkeypatches on cctally's namespace propagate into the
# moved code unchanged. `_compute_block_totals` is Z-high (reaches into
# _cctally_cache via get_claude_session_entries) and is explicitly listed
# in spec §3.7's stays-on-shim allowlist.
#
# Path constants (`MIGRATION_ERROR_LOG_PATH`, `LOG_DIR`, `DB_PATH`,
# `CACHE_DB_PATH`) are accessed via `_cctally_core.X` at call time —
# the canonical sibling pattern after the data-globals promotion
# (2026-05-22, issue #84). `_cctally_core` is the single source of
# truth and the only legal monkeypatch target; bin/cctally no longer
# seeds duplicates into this module's namespace.
def _compute_block_totals(*args, **kwargs):
    return sys.modules["cctally"]._compute_block_totals(*args, **kwargs)


# === BEGIN MOVED REGIONS ===
# Regions below are inserted verbatim from bin/cctally. Bare-name
# references to `now_utc_iso(...)`, `parse_iso_datetime(...)`,
# `_compute_block_totals(...)`, and `eprint(...)` resolve to the shims
# / direct imports above. Path-constant references
# (`MIGRATION_ERROR_LOG_PATH`, `LOG_DIR`, `DB_PATH`, `CACHE_DB_PATH`)
# are read as `_cctally_core.X` at call time (post-#84 canonical
# sibling pattern; no `c = _cctally()` binding required for path
# reads, since `_cctally_core` is direct-imported above).

# === Region 1: add_column_if_missing (was bin/cctally:8584-8621) ===

_MIGRATION_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    decl: str,
) -> bool:
    """Add a column iff it doesn't already exist. Returns True if added.

    Idempotent guard for column-shape evolution; NOT a migration. Use this
    when the new column is nullable / has a sensible default and there is
    no data backfill required (or the backfill is a separate
    @stats_migration / @cache_migration). For data-shape changes (backfill,
    dedup, rename), write a real registered migration instead — see
    _STATS_MIGRATIONS / _CACHE_MIGRATIONS in this file.

    f-string SQL is safe because `table` and `column` come from in-script
    literals only; the regex check rejects names that don't match
    ^[a-zA-Z_][a-zA-Z0-9_]*$ as belt-and-suspenders against future misuse.
    """
    if not _MIGRATION_IDENT_RE.match(table):
        raise ValueError(f"invalid identifier (table): {table!r}")
    if not _MIGRATION_IDENT_RE.match(column):
        raise ValueError(f"invalid identifier (column): {column!r}")
    # PRAGMA table_info column 1 is `name`. Index by position (r[1]) so
    # the helper works whether the caller's connection set
    # row_factory=sqlite3.Row (open_db) or left it as the default tuple
    # factory (open_cache_db).
    cols = {
        str(r[1])
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


# === Region 2: Migration framework + dispatcher (was bin/cctally:10952-11229) ===

_MIGRATION_NAME_RE = re.compile(r"^\d{3}_[a-z0-9_]+$")


@dataclass(frozen=True)
class Migration:
    """A single registered migration.

    seq:     1-based; equals position in the registry at registration time.
    name:    "NNN_descriptive_name", written into schema_migrations.
    handler: callable(conn) that owns its own BEGIN/COMMIT/ROLLBACK.
    """
    seq: int
    name: str
    handler: Callable[[sqlite3.Connection], None]


_STATS_MIGRATIONS: list[Migration] = []
_CACHE_MIGRATIONS: list[Migration] = []
# conversations.db joins the framework (DB journal redesign spec §7.2). Shared
# list object — re-exported by ``bin/cctally`` + ``bin/_cctally_cache`` the same
# way as ``_CACHE_MIGRATIONS`` (mutate in place, never rebind).
_CONVERSATIONS_MIGRATIONS: list[Migration] = []


class DowngradeDetected(Exception):
    """Raised by the dispatcher when PRAGMA user_version > len(registry).

    Means the DB was last touched by a newer cctally that has since been
    downgraded. The framework refuses to open because newer migrations
    may have written shapes the older code can't read.
    """

    def __init__(self, db_label: str, db_version: int, max_known: int):
        self.db_label = db_label
        self.db_version = db_version
        self.max_known = max_known
        db_key = "cache" if db_label.startswith("cache") else "stats"
        super().__init__(
            f"{db_label} is at version {db_version} but this cctally "
            f"only knows up to {max_known}. A newer/unreleased cctally likely "
            f"touched this data dir. Run `cctally db recover --db {db_key}` to "
            f"revert it to the known schema head (cache.db is re-derivable and "
            f"recovers without --yes; stats.db needs --yes and may require a "
            f"re-record afterward)."
        )


class ProdMigrationRefused(Exception):
    """Raised by the dispatcher when a git-checkout binary would forward-migrate
    the REAL prod data dir (~/.local/share/cctally), which would brick the
    installed release with DowngradeDetected (issue #142).

    Escape hatch: set CCTALLY_ALLOW_PROD_MIGRATION=1. The guard is
    connection-scoped + password-DB-resolved (see _would_block_prod_migration
    + _cctally_core._real_prod_data_dir) so it never fires on :memory:/temp/
    fake-HOME test connections. Spec:
    docs/superpowers/specs/2026-06-05-prod-migration-guard-design.md."""

    def __init__(self, db_label: str, next_migration: str):
        self.db_label = db_label
        self.next_migration = next_migration
        super().__init__(
            f"cctally: refusing to apply migration '{next_migration}' "
            f"({db_label}) to the prod data dir (~/.local/share/cctally) from "
            f"a dev checkout — a checkout may carry migrations your installed "
            f"cctally can't read, which would brick it (DowngradeDetected). "
            f"Point CCTALLY_DATA_DIR at a scratch/dev dir, or run the installed "
            f"binary. Override with CCTALLY_ALLOW_PROD_MIGRATION=1."
        )


class StatsDbCorruptError(sqlite3.DatabaseError):
    """stats.db failed the open-time probe (#279 S1 F4).

    Subclasses ``sqlite3.DatabaseError`` so graceful-degrade sites (doctor,
    dashboard/TUI background threads, the 5h-anchor fallback) keep treating it
    as a DB failure exactly as before — but command-level handlers that map DB
    errors to OTHER exit codes must re-raise it so the global staged diagnosis
    wins (``cmd_record_credit`` does; its documented DB-error exit is 3). NOT
    auto-recreated: stats.db is the non-re-derivable DB (recorded usage
    history), unlike cache.db.
    """


class StatsDbMaintenanceError(sqlite3.OperationalError):
    """A guided repair owns stats.db; new cctally opens must stay out."""

    def __init__(self) -> None:
        super().__init__(
            "stats.db repair is in progress; retry after the repair command exits"
        )


class StatsEpochMismatchError(sqlite3.DatabaseError):
    """A stats.db at a version-mismatched index epoch that CANNOT be rebuilt
    (DB journal redesign §7.1). Raised when ``user_version`` is neither legacy
    (<= 13) nor the current ``STATS_INDEX_EPOCH`` AND no journal is present to
    rebuild from — a hard error with guidance, never a silent rebuild-to-empty.
    Subclasses ``sqlite3.DatabaseError`` so graceful-degrade sites treat it as a
    DB failure; ``main()`` maps it to a staged exit 3."""


_SQLITE_CORRUPTION_MESSAGES = (
    "database disk image is malformed",
    "file is not a database",
    "malformed database schema",
)


def _is_sqlite_corruption_error(value: object) -> bool:
    """Recognize SQLITE_CORRUPT / SQLITE_NOTADB, including string-only hops.

    Some orchestration layers intentionally serialize an inner DB failure into
    a structured ``reason`` string.  Prefer SQLite's numeric code when the
    exception still carries it, then use the narrow canonical messages for the
    string-only boundary (#314).
    """
    code = getattr(value, "sqlite_errorcode", None)
    if isinstance(code, int):
        primary = code & 0xFF
        if primary in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
            return True
    text = str(value).casefold()
    return any(token in text for token in _SQLITE_CORRUPTION_MESSAGES)


def _stats_corruption_guidance() -> str:
    return (
        "stats.db is corrupt — run `cctally db repair --db stats --yes`. "
        "The repair command preserves the corrupt original before replacing "
        "anything; do not copy or restore the live DB by hand."
    )


class MigrationGateNotMet(Exception):
    """Migration cannot run yet because a cross-DB prerequisite is unsatisfied.

    Dispatcher treats this as transient: do NOT write to
    ``migration-errors.log``, do NOT mark the migration as skipped, do
    NOT render the error banner. Retry on the next open.

    Used by cross-DB migrations whose body needs to verify that a
    sibling DB's migration has applied AND that downstream ingest has
    repopulated the data the body depends on. The canonical use case
    is stats migration 008 (recompute weekly_cost_snapshots) which
    needs cache migration 001 (dedup wipe) AND a post-wipe
    ``sync_cache`` cycle before it can safely re-sum cost.

    Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §D4.
    """


@dataclass(frozen=True)
class UpgradeGateInputs:
    """Frozen inputs to ``resolve_upgrade_gate`` (cctally-dev#93, spec D1).

    All fields are derived by the thin I/O shell ``_gate_001_post_ingest_completed``;
    the resolver itself does no I/O.
    """
    cache_001_state: str            # "applied" | "skipped" | "pending"
    walk_complete_since_001: bool   # cache_meta marker present; missing table -> False
    cache_has_entries: bool         # session_entries non-empty; missing table -> False
    caller_has_historical_rows: bool
    disk_state: str                 # "jsonl_present" | "pruned" | "absent" (REASON only)
    marker_state_readable: bool     # False -> schema_migrations missing OR any read transiently locked


class GateAction(enum.Enum):
    PROCEED = "proceed"   # run the recompute body
    DEFER = "defer"       # raise MigrationGateNotMet; stays pending, retried next open


@dataclass(frozen=True)
class GateResolution:
    action: GateAction
    reason: str


def resolve_upgrade_gate(inp: UpgradeGateInputs) -> GateResolution:
    """Pure decision function — the D3 truth table. First matching row wins.

    Spec: docs/superpowers/specs/2026-05-23-migration-gate-state-machine-design.md D1/D3.
    """
    # Row 1 — marker state unreadable (missing schema_migrations or transient lock).
    if not inp.marker_state_readable:
        return GateResolution(
            GateAction.DEFER,
            "cache.db migration state unreadable (no schema_migrations table yet, "
            "or transiently locked); retry on next open.",
        )
    # Row 2 — 001 not applied.
    if inp.cache_001_state == "pending":
        return GateResolution(
            GateAction.DEFER,
            "cache.db migration 001_dedup_highest_wins not yet applied; run any "
            "JSONL-reading command (e.g. `cctally weekly`) once, or "
            "`cctally db skip 001_dedup_highest_wins` to defer.",
        )
    # Rows 3/4 — 001 skipped.
    if inp.cache_001_state == "skipped":
        if inp.caller_has_historical_rows:
            return GateResolution(
                GateAction.DEFER,
                "cache.db migration 001_dedup_highest_wins is skipped while historical "
                "rows remain; deferring to avoid recomputing over stale pre-dedup "
                "session_entries. Run `cctally db unskip 001_dedup_highest_wins` then "
                "any JSONL-reading command once.",
            )
        return GateResolution(
            GateAction.PROCEED,
            "001 skipped and no historical rows to protect; proceed (body no-ops).",
        )
    # cache_001_state == "applied" below.
    # Row 5 — nothing to protect.
    if not inp.caller_has_historical_rows:
        return GateResolution(
            GateAction.PROCEED,
            "no historical rows to protect; proceed (body no-ops).",
        )
    # Row 6 — complete, non-empty post-001 cache.
    if inp.walk_complete_since_001 and inp.cache_has_entries:
        return GateResolution(
            GateAction.PROCEED,
            "complete, non-empty post-001 walk observed; proceed.",
        )
    # Row 7 — DEFER; reason branches on disk_state (decision is identical).
    if not inp.walk_complete_since_001:
        if inp.disk_state == "jsonl_present":
            reason = ("post-001 ingest walk not yet complete; run any JSONL-reading "
                      "command (e.g. `cctally weekly`) once and retry.")
        elif inp.disk_state == "pruned":
            reason = ("no complete post-001 walk and projects/ holds no JSONL; restore "
                      "the JSONL or `cctally db skip` this migration to accept stale "
                      "aggregates.")
        else:  # absent
            reason = ("no complete post-001 walk and no projects/ dir resolves; check "
                      "CLAUDE_CONFIG_DIR or `cctally db skip` this migration.")
    else:  # walk complete but cache empty (rebuild/truncation over pruned disk)
        reason = ("cache is empty after a rebuild over pruned disk; refusing to zero "
                  "historical aggregates. Restore the JSONL or `cctally db skip` this "
                  "migration.")
    return GateResolution(GateAction.DEFER, reason)


def _make_migration_decorator(registry: list[Migration], db_label: str, name: str):
    """Internal helper — builds the @stats_migration / @cache_migration decorators.

    Enforces three invariants at registration time. Checks run in this
    order so the developer sees the most actionable message first:
      1. Name matches ^\\d{3}_[a-z0-9_]+$ (well-formed) — typos beat
         contiguity errors.
      2. Name is unique within this registry — re-registration of an
         existing migration is a copy-paste bug, not a numbering bug.
      3. Numeric prefix matches len(registry) + 1 exactly (contiguity)
         — final defense against gaps / out-of-order edits.
    Failure of any → RuntimeError at script load (not silently mis-applied).
    """
    def deco(fn):
        if not _MIGRATION_NAME_RE.match(name):
            raise RuntimeError(
                f"{db_label} migration name '{name}' is invalid; "
                f"must match {_MIGRATION_NAME_RE.pattern}"
            )
        if any(m.name == name for m in registry):
            raise RuntimeError(
                f"{db_label} migration '{name}' duplicated"
            )
        seq = len(registry) + 1
        prefix = f"{seq:03d}_"
        if not name.startswith(prefix):
            raise RuntimeError(
                f"{db_label} migration #{seq} must be named '{prefix}…' "
                f"but got '{name}'"
            )
        registry.append(Migration(seq=seq, name=name, handler=fn))
        return fn
    return deco


def stats_migration(name: str):
    """Register a stats.db migration. Use as @stats_migration("NNN_descriptive_name").

    HANDLER CONTRACT — idempotency is MANDATORY, not best-effort. The
    dispatcher applies a handler and stamps its ``schema_migrations`` marker in
    a TWO-TRANSACTION window (``m.handler(conn)`` commits the data change, THEN
    ``_stamp_applied`` writes the marker in a separate transaction — see
    ``_run_pending_migrations``). A crash BETWEEN those two commits leaves the
    data change applied but unmarked, so the NEXT ``open_db()`` re-invokes the
    handler. A handler that is not idempotent (re-derives from already-migrated
    state, double-applies, or raises on a second run) corrupts or bricks the DB
    on that retry. Every handler MUST therefore be safe to run again on its own
    output: probe-then-return on the already-applied shape (a fast-path guard
    like ``if 'col' in cols(...): return``), or use ``INSERT OR IGNORE`` /
    ``UPDATE … WHERE not-yet-migrated`` / upsert idioms. The same contract
    covers a ``MigrationGateNotMet`` deferral: the dispatcher leaves the
    migration pending and re-runs it on a later open, which is just another
    handler retry. Carve-out: cache ``001_dedup_highest_wins`` stamps its marker
    ATOMICALLY inside its own ``BEGIN IMMEDIATE`` (the destructive wipe and the
    stamp must commit together), so it has no two-transaction window — but it is
    still idempotent (its in-txn ``already_applied`` re-check). Per-migration
    goldens (``tests/test_*_per_migration_goldens.py``) pin the second-invocation
    no-op; the registry-completeness guard enforces one per migration.
    """
    return _make_migration_decorator(_STATS_MIGRATIONS, "stats.db", name)


def cache_migration(name: str):
    """Register a cache.db migration. Use as @cache_migration("NNN_descriptive_name").

    HANDLER CONTRACT — idempotency is MANDATORY. See ``stats_migration`` above
    for the full statement: the dispatcher's handler-commit → marker-stamp
    two-transaction window means a crash (or a ``MigrationGateNotMet`` deferral)
    re-invokes the handler on its own output, so every handler must be safe to
    re-run (probe-then-return / ``INSERT OR IGNORE`` / not-yet-migrated guards).
    The flag-only re-ingest cache migrations (003-007, 009, 014, 017) satisfy it
    by ``_set_cache_meta`` upsert; ``001_dedup_highest_wins`` is the lone
    atomic-stamp carve-out.
    """
    return _make_migration_decorator(_CACHE_MIGRATIONS, "cache.db", name)


def conversations_migration(name: str):
    """Register a conversations.db migration (DB journal redesign spec §7.2).
    Use as @conversations_migration("NNN_descriptive_name").

    HANDLER CONTRACT — idempotency is MANDATORY. See ``stats_migration`` above
    for the full statement: the dispatcher's handler-commit → marker-stamp
    two-transaction window means a crash (or a ``MigrationGateNotMet`` deferral)
    re-invokes the handler on its own output, so every handler must be safe to
    re-run (probe-then-return / ``INSERT OR IGNORE`` / not-yet-migrated guards).
    conversations.db is re-derivable from provider JSONL, so ``open_conversations_db``
    runs the dispatcher with ``recover_version_ahead=True`` (like cache.db).
    """
    return _make_migration_decorator(
        _CONVERSATIONS_MIGRATIONS, "conversations.db", name)


# Pre-framework migration markers were stored under unprefixed names;
# the dispatcher's bootstrap rename rewrites them to NNN_ form on the
# first open_db() that runs the framework. Raw-sqlite3 db commands
# (cmd_db_status, cmd_db_skip) bypass open_db() by design and so don't
# benefit from that rename — they consult this map to recognize legacy
# rows as already-applied without mutating the DB.
_LEGACY_MARKER_ALIASES_BY_DB: dict[str, dict[str, str]] = {
    "stats.db": {
        "five_hour_block_models_backfill_v1":   "001_five_hour_block_models_backfill_v1",
        "five_hour_block_projects_backfill_v1": "002_five_hour_block_projects_backfill_v1",
        "merge_5h_block_duplicates_v1":         "003_merge_5h_block_duplicates_v1",
    },
    # cache.db has no pre-framework markers.
    "cache.db": {},
}


def _bootstrap_rename_legacy_markers(conn: sqlite3.Connection, db_label: str) -> None:
    """One-shot, idempotent: rename pre-framework marker rows to NNN_ form.

    Caller (the dispatcher, added in a later task) owns the BEGIN/COMMIT
    envelope; this fn just executes the DML inside the active transaction.
    Hardcoded against the three known stats.db markers — no-op everywhere
    else, including cache.db (which has no pre-framework markers).

    Also clears any pre-framework failure-log entries referencing the
    legacy unprefixed name, so a residual banner stops rendering once the
    rename succeeds. Without this clear, the dispatcher's success-side
    _clear_migration_error_log_entries(qualified_name) would match
    nothing and the legacy banner would persist forever (Codex P2 #5).

    Idempotent on subsequent opens: the UPDATEs find nothing to rename
    and the log clears find nothing to drop.

    Idempotent against the duplicate-marker case too: if BOTH the
    legacy (``old``) and the prefixed (``new``) rows already exist
    (e.g., a user briefly ran a dev build that prefixed the markers,
    then reverted to a pre-framework binary that re-applied the legacy
    unprefixed markers), the UPDATE would collide on the schema_migrations
    PRIMARY KEY (``name``) — observed in the wild as a recurring
    ``UNIQUE constraint failed: schema_migrations.name`` failure that
    permanently blocked the dispatcher from running ANY downstream
    migration. Resolution: DELETE the legacy row first when its
    prefixed counterpart already exists, then UPDATE the rest. The
    prefixed row wins because it carries the dispatcher-managed
    applied_at_utc that newer code reads for sequencing decisions.
    """
    aliases = _LEGACY_MARKER_ALIASES_BY_DB.get(db_label, {})
    if not aliases:
        return
    for old, new in aliases.items():
        # If the prefixed marker is already present, drop the legacy
        # duplicate (UPDATE would collide on PRIMARY KEY); keep the
        # prefixed row's applied_at_utc as authoritative.
        conn.execute(
            "DELETE FROM schema_migrations "
            " WHERE name = ? "
            "   AND EXISTS (SELECT 1 FROM schema_migrations WHERE name = ?)",
            (old, new),
        )
        conn.execute(
            "UPDATE schema_migrations SET name = ? WHERE name = ?",
            (new, old),
        )
        _clear_migration_error_log_entries(old)


def _conn_db_dir(conn: sqlite3.Connection) -> "pathlib.Path | None":
    """Resolved directory of the connection's `main` database file, or None for
    an in-memory / no-file connection (PRAGMA database_list returns '' there).
    Tuple-indexed so it works on the cache.db connection (no row_factory)."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            db_file = row[2]
            if not db_file:
                return None
            return pathlib.Path(db_file).resolve().parent
    return None


def _would_block_prod_migration(conn: sqlite3.Connection) -> bool:
    """True iff a git-checkout binary is about to migrate a DB that physically
    lives in the REAL prod data dir (issue #142).

    Connection-scoped (NOT global APP_DIR) so :memory:/temp/scratch connections
    never trip it; HOME-faking-immune via _real_prod_data_dir (password DB, not
    $HOME); suppressor-INDEPENDENT raw .git check so it still fires under the
    test-suite's CCTALLY_DISABLE_DEV_AUTODETECT. Escape: CCTALLY_ALLOW_PROD_MIGRATION."""
    if _cctally_core._truthy_env("CCTALLY_ALLOW_PROD_MIGRATION"):
        return False
    if not (_cctally_core._repo_root() / ".git").exists():
        return False
    db_dir = _conn_db_dir(conn)
    if db_dir is None:
        return False
    try:
        return db_dir == _cctally_core._real_prod_data_dir().resolve()
    except OSError:
        return False


def _would_block_prod_stats(path: pathlib.Path) -> bool:
    """Path-based sibling of ``_would_block_prod_migration`` for the stats.db
    classifier-gated auto-heal + ``db rebuild --db stats`` (spec §6.3, issue
    #146): True iff a git-checkout binary would quarantine+rebuild a stats.db that
    physically lives in the REAL prod data dir. Password-DB-resolved (HOME-faking
    immune), suppressor-independent raw ``.git`` check. Escape:
    ``CCTALLY_ALLOW_PROD_MIGRATION``."""
    if _cctally_core._truthy_env("CCTALLY_ALLOW_PROD_MIGRATION"):
        return False
    if not (_cctally_core._repo_root() / ".git").exists():
        return False
    try:
        return (
            pathlib.Path(path).resolve().parent
            == _cctally_core._real_prod_data_dir().resolve()
        )
    except OSError:
        return False


# Known external cloud-sync / backup daemons that hold a DB file open and are the
# leading suspects for the region-shifting prod corruption (#336 defense 2). The
# forensics scan flags any running process whose name matches, so the auto-heal
# incident finally captures the culprit rather than only the damage.
_CORRUPTION_HOLDER_HINTS = (
    "backupd", "bird", "cloudd", "Dropbox", "GoogleDrive", "OneDrive",
    "FileProvider", "CloudDocs", "Box", "pCloud",
)


def _forensics_iso(epoch_or_now) -> str:
    if isinstance(epoch_or_now, (int, float)):
        d = dt.datetime.fromtimestamp(epoch_or_now, dt.timezone.utc)
    else:
        d = epoch_or_now
    return d.isoformat(timespec="seconds").replace("+00:00", "Z")


def write_corruption_forensics(
    db_path, *, db_label: str = "stats",
) -> "pathlib.Path | None":
    """Write the #336-defense-2 corruption forensics bundle FIRST — before any
    quarantine/rebuild disturbs the evidence (spec §6.3). Captures the file family
    sizes+mtimes, a best-effort ``PRAGMA integrity_check``, an ``lsof`` of the
    family, and a scan of running processes for known cloud-sync/backup holders.
    Every leg degrades to a captured error string rather than raising, so a heal
    never fails because forensics could not fully run. Returns the bundle path (or
    None if it could not be written). Shared by the auto-heal HEAL_HOOK and
    ``db rebuild`` (and, later, doctor's incident leg)."""
    db_path = pathlib.Path(db_path)
    ts = _db_backup_timestamp()
    try:
        _cctally_core.LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    out = _cctally_core.LOG_DIR / f"{db_path.name}-corruption-forensics-{ts}.json"
    bundle: dict = {
        "schemaVersion": 1,
        "capturedAtUtc": _forensics_iso(dt.datetime.now(dt.timezone.utc)),
        "db": db_label,
        "path": str(db_path),
        "family": {},
        "integrityCheck": None,
        "lsof": None,
        "holders": [],
    }
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(str(db_path) + suffix)
        try:
            st = p.stat()
            bundle["family"][p.name] = {
                "bytes": st.st_size, "mtimeUtc": _forensics_iso(st.st_mtime),
            }
        except OSError:
            bundle["family"][p.name] = None
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            c.execute("PRAGMA busy_timeout=2000")
            bundle["integrityCheck"] = [
                r[0] for r in c.execute("PRAGMA integrity_check").fetchall()
            ]
        finally:
            c.close()
    except Exception as exc:
        bundle["integrityCheck"] = f"error: {exc}"
    try:
        cp = subprocess.run(
            ["lsof", "--", str(db_path)],
            capture_output=True, text=True, timeout=5,
        )
        bundle["lsof"] = cp.stdout or cp.stderr or ""
    except Exception as exc:
        bundle["lsof"] = f"error: {exc}"
    try:
        cp = subprocess.run(
            ["ps", "-Axo", "pid=,comm="],
            capture_output=True, text=True, timeout=5,
        )
        for line in (cp.stdout or "").splitlines():
            low = line.lower()
            if any(h.lower() in low for h in _CORRUPTION_HOLDER_HINTS):
                bundle["holders"].append(line.strip())
    except Exception as exc:
        bundle["holders"] = [f"error: {exc}"]
    try:
        out.write_text(json.dumps(bundle, indent=2, sort_keys=True))
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
    except OSError:
        return None
    return out


def quarantine_db_family(db_path, *, ts: "str | None" = None) -> pathlib.Path:
    """Move a damaged DB + its ``-wal``/``-shm`` sidecars into a single
    timestamped incident directory under ``quarantine/`` with a manifest (spec
    §6.3). NEVER deletes evidence — three renames under the caller's exclusion
    locks, not pretending to be one atomic op. ``0o700`` dir / ``0o600`` files.
    Returns the incident directory (which may be empty if nothing was present)."""
    db_path = pathlib.Path(db_path)
    ts = ts or _db_backup_timestamp()
    root = _cctally_core.APP_DIR / "quarantine"
    incident = root / f"{db_path.name}-{ts}"
    incident.mkdir(parents=True, exist_ok=True)
    for d in (root, incident):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    moved: list = []
    for suffix in ("", "-wal", "-shm"):
        src = pathlib.Path(str(db_path) + suffix)
        if not src.exists():
            continue
        dst = incident / src.name
        try:
            os.replace(str(src), str(dst))
            try:
                os.chmod(dst, 0o600)
            except OSError:
                pass
            moved.append(src.name)
        except OSError as exc:
            eprint(f"[quarantine] could not move {src}: {exc}")
    manifest = {
        "schemaVersion": 1,
        "quarantinedAtUtc": _forensics_iso(dt.datetime.now(dt.timezone.utc)),
        "originalPath": str(db_path),
        "movedFiles": moved,
    }
    try:
        (incident / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True))
    except OSError:
        pass
    return incident


def cmd_db_rebuild(args: argparse.Namespace) -> int:
    """``db rebuild --db stats`` — explicit journal replay into a fresh index
    (spec §9). Forensics-quarantines the current stats.db the SAME forensics-first
    way auto-heal does (even when the DB is healthy — this is an operator's
    deliberate reset), then rebuilds from the journal and reports counts +
    duration. Held under the stats maintenance lock so it serializes with a
    concurrent auto-heal. #146 prod guard: a dev/worktree binary refuses to
    rebuild the real prod stats.db unless ``CCTALLY_ALLOW_PROD_MIGRATION=1``.
    Exit 0 on success, 2 for the prod guard, 3 for a rebuild failure."""
    from _lib_json_envelope import stamp_schema_version
    import _cctally_journal

    as_json = bool(getattr(args, "json", False))
    path = _cctally_core.DB_PATH
    if _would_block_prod_stats(path):
        eprint(
            "cctally: refusing to rebuild the prod stats.db "
            "(~/.local/share/cctally) from a dev checkout. Run the installed "
            "binary, or override with CCTALLY_ALLOW_PROD_MIGRATION=1."
        )
        return 2

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    maint_fd = os.open(
        str(_cctally_core.STATS_LOCK_MAINTENANCE_PATH),
        os.O_RDWR | os.O_CREAT, 0o600,
    )
    forensics = None
    incident = None
    try:
        fcntl.flock(maint_fd, fcntl.LOCK_EX)
        import _cctally_store
        # Symmetry with the auto-heal path (Task-8 P3-3): take the bounded ingest
        # lock so a concurrent ingest cycle can't write into the stats.db we are
        # about to quarantine + rebuild. Journal-is-truth makes this
        # correctness-neutral, but keeping the two rebuild paths identical avoids
        # a latent write-into-quarantined-inode surprise.
        ingest_fd = _cctally_store._heal_flock_bounded(
            _cctally_core.JOURNAL_INGEST_LOCK_PATH, 5.0)
        try:
            if path.exists():
                forensics = write_corruption_forensics(path, db_label="stats")
                incident = quarantine_db_family(path)
            result = _cctally_journal.rebuild_stats_index()
        except Exception as exc:
            eprint(f"cctally: stats.db rebuild failed: {exc}")
            return 3
        finally:
            _cctally_store._heal_release_flock(ingest_fd)
    finally:
        try:
            fcntl.flock(maint_fd, fcntl.LOCK_UN)
        finally:
            os.close(maint_fd)

    total_rows = sum(result.rows_by_table.values())
    if as_json:
        payload = {
            "db": "stats",
            "segmentsRead": result.segments_read,
            "linesFolded": result.lines_folded,
            "malformed": result.malformed,
            "durationSeconds": round(result.duration_s, 3),
            "rowsByTable": result.rows_by_table,
            "totalRows": total_rows,
            "quarantineDir": str(incident) if incident else None,
            "forensicsPath": str(forensics) if forensics else None,
        }
        print(json.dumps(stamp_schema_version(payload, version=1)))
    else:
        print(
            f"cctally: rebuilt stats.db from {result.segments_read} journal "
            f"segment(s) — {result.lines_folded} lines folded, "
            f"{result.malformed} malformed, {total_rows} rows, "
            f"{result.duration_s:.2f}s."
        )
        for tbl, n in result.rows_by_table.items():
            if n:
                print(f"  {tbl}: {n}")
        if incident is not None:
            print(f"  previous stats.db quarantined -> {incident}")
        if forensics is not None:
            print(f"  forensics -> {forensics}")
    return 0


def _first_pending_migration_name(
    conn: sqlite3.Connection, registry: "list[Migration]", cur_version: int
) -> str:
    """Best-effort name of the first not-yet-applied migration, for the refusal
    message. Marker-aware (handles skip-gaps + db-unskip's user_version=0) with
    a raw-index fallback. Legacy unprefixed markers are an accepted imperfection
    — the name is a human hint, not load-bearing."""
    try:
        applied = {r[0] for r in conn.execute(
            "SELECT name FROM schema_migrations").fetchall()}
    except sqlite3.OperationalError:
        applied = set()
    try:
        skipped = {r[0] for r in conn.execute(
            "SELECT name FROM schema_migrations_skipped").fetchall()}
    except sqlite3.OperationalError:
        skipped = set()
    for m in registry:
        if m.name not in applied and m.name not in skipped:
            return m.name
    return registry[cur_version].name


def _recover_version_ahead(
    conn: sqlite3.Connection,
    registry: list[Migration],
    db_label: str,
) -> dict:
    """Reconcile a version-ahead DB down to this binary's known head (issue #145).

    A DB whose ``PRAGMA user_version`` exceeds ``len(registry)`` was last
    touched by a newer/unreleased cctally. cache.db is fully re-derivable, so
    we heal in place instead of bricking: trim the unknown (ahead) markers from
    BOTH ledger tables, then reconcile ``user_version``.

    We DELIBERATELY do not blind-set ``user_version = len(registry)``: the
    dispatcher treats ``schema_migrations_skipped`` as authoritative and only
    advances ``user_version`` when every known migration is applied-or-skipped.
    So we trim unknown rows from both tables (Codex review P1 #1), then set
    ``user_version = len(registry)`` only if every known migration is
    applied-or-skipped; otherwise ``0`` so the dispatcher's normal walk re-runs
    the still-pending known migrations idempotently (Codex review P1 #2) — never
    cementing a fast-path past a genuinely-missing known migration.

    Extra tables/columns the unknown migration created are left inert (SQLite
    tolerates them; cache is re-derivable). Idempotent: no-op when not ahead.
    Tolerates absent ledger tables (Codex review P2).

    Returns ``{"reverted_from", "reverted_to", "trimmed"}`` for the caller's
    breadcrumb / ``db recover`` report.
    """
    cur_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if cur_version <= len(registry):
        return {"reverted_from": cur_version, "reverted_to": cur_version, "trimmed": 0}

    aliases = _LEGACY_MARKER_ALIASES_BY_DB.get(db_label, {})
    known = {m.name for m in registry} | set(aliases.keys()) | set(aliases.values())
    placeholders = ",".join("?" for _ in known) if known else "''"
    params = tuple(known)

    trimmed = 0
    for table in ("schema_migrations", "schema_migrations_skipped"):
        try:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE name NOT IN ({placeholders})", params
            )
            trimmed += max(cur.rowcount, 0)  # DELETE rowcount is always >= 0
        except sqlite3.OperationalError:
            pass  # table absent → nothing to trim there

    applied: set[str] = set()
    skipped: set[str] = set()
    for table, dest in (("schema_migrations", applied),
                        ("schema_migrations_skipped", skipped)):
        try:
            for row in conn.execute(f"SELECT name FROM {table}").fetchall():
                # Normalize legacy unprefixed markers to their canonical NNN_
                # name (issue #148). The alias union above keeps such a row from
                # being trimmed; without this normalization the membership test
                # below compares canonical m.name against the legacy alias and
                # falsely concludes the migration is missing, resetting
                # user_version to 0 and forcing a needless full re-walk. Mirrors
                # the alias-aware read in cmd_db_status.
                dest.add(aliases.get(row[0], row[0]))
        except sqlite3.OperationalError:
            pass

    all_known_done = all((m.name in applied or m.name in skipped) for m in registry)
    new_version = len(registry) if all_known_done else 0
    conn.execute(f"PRAGMA user_version = {new_version}")
    conn.commit()
    return {"reverted_from": cur_version, "reverted_to": new_version, "trimmed": trimmed}


def _stamp_applied(conn, name, applied_at_utc=None):
    """Persist the schema_migrations marker for ``name``, then commit.

    Central stamp owned by the dispatcher (issue #140). Handlers no longer
    self-stamp — EXCEPT cache 001, whose stamp must stay atomic with its
    destructive wipe; for that one this call is an idempotent no-op.
    ``INSERT OR IGNORE`` so a pre-existing row (cache 001, or a concurrent
    winner) never raises.
    """
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
        (name, applied_at_utc or now_utc_iso()),
    )
    conn.commit()


def _run_pending_migrations(
    conn: sqlite3.Connection,
    *,
    registry: list[Migration],
    db_label: str,
    recover_version_ahead: bool = False,
) -> None:
    """Apply pending migrations from ``registry`` against ``conn``.

    Spec: docs/superpowers/specs/2026-05-06-migration-framework-design.md
          §2.3 (full pseudocode), §3.1 (failure semantics).

    Behavior:
      - PRAGMA user_version > len(registry)  → raise DowngradeDetected.
      - PRAGMA user_version == len(registry) → fast-path return.
      - Bootstrap rename runs in its own BEGIN/COMMIT (Codex P1 #2 fix):
        closes the implicit transaction Python's sqlite3 module would
        auto-open on the UPDATE statements, so subsequent handler
        ``conn.execute("BEGIN")`` calls start cleanly.
      - Fresh install (schema_migrations just CREATE'd, zero rows
        post-bootstrap, AND the DB's primary data table is empty or
        absent) → stamp every migration applied without invoking
        handlers. The data-emptiness probe (D1) defends against the
        pre-framework upgrade case where cache.db was populated by
        a pre-v1.12.0 build that wrote ``session_entries`` without
        ever creating ``schema_migrations`` — pre-fix that landscape
        was falsely classified as fresh and stamped every migration
        applied without running its handler, indefinitely persisting
        the buggy summed-tokens dedup. Probe tables:
        ``stats.db → weekly_cost_snapshots``,
        ``cache.db → session_entries``.
      - Per migration: handler raises ``Exception`` → log + BREAK
        (Codex P1 #3 — the FIRST failure halts the registry walk so
        later migrations never see partial-prior state). ``BaseException``
        propagates uncaught (Codex P1 #4 — KeyboardInterrupt / SystemExit
        must not be swallowed).
      - Tuple-safe SELECTs (Codex P2 #7) — works against connections
        with or without ``row_factory = Row``; cache.db deliberately
        leaves the default tuple row factory.
      - PRAGMA user_version advances ONLY when every migration is
        applied OR skipped post-loop. A failure in the middle of the
        registry leaves user_version unchanged so the next open re-tries
        from the failed entry.
    """
    cur_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if cur_version > len(registry):
        if recover_version_ahead:
            # cache.db is re-derivable — heal in place instead of bricking (#145).
            info = _recover_version_ahead(conn, registry, db_label)
            eprint(
                f"cctally: {db_label} was ahead (v{info['reverted_from']} > "
                f"known v{len(registry)}); trimmed unknown migration state and "
                f"reconciled to the known head (cache is re-derivable). Run "
                f"'cctally cache-sync --rebuild' for a full rebuild."
            )
            cur_version = conn.execute("PRAGMA user_version").fetchone()[0]
            # common case: cur_version == len(registry) → fast-path below.
            # adversarial (a known marker was missing): cur_version == 0 →
            # falls through to the normal pending-loop, which reconciles.
            # NOTE: on that adversarial fall-through against a prod cache.db,
            # _recover_version_ahead has ALREADY committed user_version=0, so
            # the prod-migration guard below ("user_version provably unchanged")
            # is reached with user_version already lowered. That is acceptable
            # ONLY because heal opts in for cache.db, which is re-derivable — a
            # reset-to-0 then ProdMigrationRefused just makes the next legit
            # open re-walk. The guard's "unchanged" invariant holds for stats.db
            # (never heals) and for the non-heal path.
        else:
            raise DowngradeDetected(
                db_label, db_version=cur_version, max_known=len(registry),
            )
    if cur_version == len(registry):
        # When the registry is currently empty (today's cache.db case),
        # still leave the schema_migrations table behind so a later
        # transition to len(registry) >= 1 can distinguish populated
        # DBs from fresh installs. Without this, the fast-path returns
        # before any DDL, so the future first-cache-migration walk
        # finds no schema_migrations table, treats the populated DB as
        # fresh, and stamps the new migration applied without invoking
        # its handler.
        if len(registry) == 0:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
            )
        # Clear stale bootstrap-rename failure entries. If user_version
        # reached len(registry), every migration is applied OR skipped
        # — by definition no pending failure remains. Any persisted
        # bootstrap-rename entry in the error log is from a PRIOR
        # buggy bootstrap (now repaired) and is stale; clear it so the
        # banner stops rendering. Cheap no-op when the log file
        # doesn't exist or doesn't contain a matching entry.
        _clear_migration_error_log_entries(
            f"{db_label}:_bootstrap_rename_legacy_markers"
        )
        return  # fast path

    # Prod-migration guard (issue #142): a git-checkout binary must not
    # forward-migrate the real prod data dir — that bumps user_version past
    # what the installed release knows and bricks it with DowngradeDetected.
    # We are past the two early returns, so cur_version < len(registry): there
    # ARE pending migrations that would advance user_version. Refuse BEFORE
    # bootstrap-rename / fresh-install detection / any marker write, so
    # user_version is provably unchanged. Connection-scoped so it only fires
    # on the real prod DB files, never on :memory:/temp/scratch test conns.
    if _would_block_prod_migration(conn):
        raise ProdMigrationRefused(
            db_label, _first_pending_migration_name(conn, registry, cur_version)
        )

    # Track whether schema_migrations existed before this open so we can
    # detect the fresh-install path. After bootstrap, even a "first time
    # opened with framework code" DB might have rows from the legacy
    # rename — those count as already-applied, NOT as a fresh install.
    schema_migrations_existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone() is not None

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name           TEXT PRIMARY KEY,
            applied_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations_skipped (
            name           TEXT PRIMARY KEY,
            skipped_at_utc TEXT NOT NULL,
            reason         TEXT
        )
        """
    )

    # Bootstrap rename in its own commit envelope (Codex P1 #2 fix).
    # Closes the implicit transaction Python's sqlite3 module would
    # auto-open on the UPDATE statements, so subsequent handler
    # conn.execute("BEGIN") starts cleanly.
    try:
        conn.execute("BEGIN")
        _bootstrap_rename_legacy_markers(conn, db_label)
        conn.commit()
        # On success, clear any persisted error from a PRIOR bootstrap-rename
        # failure (e.g., the duplicate-marker UNIQUE collision that was
        # observed in the wild and is now repaired by the DELETE-before-UPDATE
        # in _bootstrap_rename_legacy_markers). Without this clear, the
        # banner from the prior failed run would persist forever after the
        # repair, even though the dispatcher now completes cleanly.
        _clear_migration_error_log_entries(
            f"{db_label}:_bootstrap_rename_legacy_markers"
        )
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        _log_migration_error(
            name=f"{db_label}:_bootstrap_rename_legacy_markers",
            exc=exc,
            tb=traceback.format_exc(),
        )
        eprint(
            f"[migration {db_label}:_bootstrap_rename_legacy_markers] "
            f"failed: {exc}"
        )
        return  # do not walk the registry this open

    # Tuple-safe SELECTs — cache.db connection does not set row_factory.
    applied = {
        row[0] for row in conn.execute("SELECT name FROM schema_migrations").fetchall()
    }
    skipped = {
        row[0] for row in conn.execute("SELECT name FROM schema_migrations_skipped").fetchall()
    }

    # D1 — fresh install requires BOTH "schema_migrations table did not
    # exist" AND "the DB's primary data table is empty (or absent)".
    # Pre-fix this check was schema_migrations-only: a pre-v1.12.0
    # cache.db (populated session_entries but no schema_migrations
    # table — the framework didn't exist for cache.db before this
    # release) was falsely classified as a fresh install. The
    # fresh-install branch then stamped EVERY pending migration's
    # marker WITHOUT invoking its handler, so the cache 001
    # dedup-highest-wins migration silently skipped on every upgrading
    # user. The handler is the entire fix — skipping it leaves the
    # buggy summed-tokens data in place indefinitely.
    #
    # Probe tables per DB — ANY non-empty probe table means "not fresh":
    #   * stats.db → every table the recompute migrations (008/009/010)
    #     touch: ``weekly_cost_snapshots`` (008), ``five_hour_blocks``
    #     (009), ``percent_milestones`` (010). Probing ONLY
    #     ``weekly_cost_snapshots`` was a gap: a legacy stats.db with live
    #     5h history but no weekly snapshots (e.g. a user who only ever ran
    #     5h-window commands) was falsely classified as a fresh install,
    #     so 009 got stamped-without-running and its historical 5h totals
    #     stayed inflated forever — the exact bug this patch set exists to
    #     fix. Probe all three so non-emptiness in ANY recompute target
    #     forces the handlers to run.
    #   * cache.db → ``session_entries`` (the table 001 wipes; non-empty
    #     means real session history under the buggy old dedup rule).
    # Probe table absent → treat as empty (a brand-new DB hasn't run
    # the schema CREATEs yet, so the data table doesn't exist; that's a
    # genuine fresh install).
    fresh_install = (not schema_migrations_existed) and len(applied) == 0
    if fresh_install:
        probe_tables = {
            "stats.db": (
                "weekly_cost_snapshots",
                "five_hour_blocks",
                "percent_milestones",
                # budget milestone tables tracked by 011 (#137); empty on fresh
                # installs, so this only guards a hand-dropped schema_migrations DB.
                "budget_milestones",
                "projected_milestones",
                "codex_budget_milestones",
            ),
            # conversation_messages joins session_entries as a fresh-install
            # signal (#188): a transcript-populated cache whose cost rows are
            # absent must still be treated as non-fresh so a flag-only
            # conversation migration's consumer actually runs (e.g. 011's
            # command-args promotion) instead of being stamped without it.
            # codex_session_entries joins them (#312): an accounting-bearing
            # cache written before conversation-key enrichment but missing
            # schema_migrations must not be stamped fresh, or 026 would never
            # clear it for the required byte-zero Codex replay.
            # codex_conversation_events joins them (#294 S6): a Codex-bearing
            # cache written by S1's fused ingest but missing schema_migrations
            # would otherwise be classified fresh and stamp 025 WITHOUT replaying
            # normalization, leaving the read kernels authoritative over an empty
            # normalized corpus. Probing it forces 025's handler to run and derive
            # the normalized rows from the retained events.
            "cache.db": (
                "session_entries", "conversation_messages", "codex_session_entries",
                "codex_conversation_events",
            ),
            # conversations.db (spec §7.2): a transcript-bearing store written
            # before the framework existed carries the marker but no
            # schema_migrations. Probing its data tables classifies it NON-fresh
            # so migration 001's handler runs (adopts the marker) instead of
            # being stamped without it — the difference between adopt and skip.
            "conversations.db": (
                "conversation_messages", "conversation_source_files",
                "codex_conversation_source_files",
            ),
        }.get(db_label, ())
        for probe_table in probe_tables:
            # _probe_table_nonempty centralizes the "is there data here?"
            # probe (cctally-dev#93): a present-and-non-empty table means
            # data exists from a pre-framework write path, so the DB is
            # NOT a fresh install — run every handler normally so the
            # upgrading user gets the fix. A missing table contributes no
            # signal (genuine pre-CREATE fresh install); keep checking the
            # rest. Transient BUSY/LOCKED and any other OperationalError
            # propagate (corrupt DB / IO error).
            if _probe_table_nonempty(conn, probe_table):
                fresh_install = False
                break

    now_iso = now_utc_iso()
    for m in registry:
        if m.name in applied or m.name in skipped:
            continue
        if fresh_install:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
                (m.name, now_iso),
            )
            applied.add(m.name)
            continue
        qualified_name = f"{db_label}:{m.name}"
        try:
            m.handler(conn)
            _stamp_applied(conn, m.name, now_iso)      # central stamp (#140)
            _clear_migration_error_log_entries(qualified_name)
            applied.add(m.name)
        except MigrationGateNotMet as gate_exc:
            # Transient cross-DB gating: do NOT log to migration-errors.log,
            # do NOT mark as skipped, do NOT render the error banner. The
            # migration stays pending; the next open re-tries it. Spec
            # docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §D4.
            #
            # P2 — defensive log-entry clear, symmetric with the success
            # branch above. A prior run may have logged a hard failure
            # for this migration; if the underlying state has since
            # shifted such that the migration now gate-defers (e.g. a
            # prereq vanished mid-cycle, or the handler was rewritten to
            # gate where it previously raised), the stale error log
            # entry would persist forever and the banner would mislead.
            # Clearing here keeps the contract crisp: any non-failure
            # outcome (apply OR gate-defer) clears any prior failure log
            # for this migration's qualified name.
            _clear_migration_error_log_entries(qualified_name)
            if _lib_log.debug_enabled():
                _lib_log.get_logger("db").debug(
                    f"[migration {qualified_name}] deferred: {gate_exc}"
                )
            # D2 — ``continue``, NOT ``break``. A gate-defer leaves the DB
            # in a fully-consistent prior state (the handler raised before
            # touching anything, or rolled back its own BEGIN); later
            # registry entries can legitimately attempt to run. The
            # all-applied predicate below uses ``applied | skipped``, so
            # this gated migration's absence from both sets keeps
            # ``user_version`` from advancing — a future open re-tries
            # the gated migration even if every later one succeeded.
            #
            # Contrast the Exception branch below, which DOES break: a
            # generic handler exception may have left a partial transaction
            # state, so later migrations should not see it.
            continue
        except Exception as exc:
            _log_migration_error(
                name=qualified_name,
                exc=exc,
                tb=traceback.format_exc(),
            )
            eprint(f"[migration {qualified_name}] failed: {exc}")
            break  # stop on first failure (Codex P1 #3)

    if fresh_install:
        conn.commit()  # commit fresh-install stamps so they're durable

    # Advance user_version only when every migration is applied OR skipped.
    if all((m.name in applied or m.name in skipped) for m in registry):
        conn.execute(f"PRAGMA user_version = {len(registry)}")
        conn.commit()


# === Region 3: 001 handler (was bin/cctally:11232-11344) ===

@stats_migration("001_five_hour_block_models_backfill_v1")
def _backfill_five_hour_block_models(conn: sqlite3.Connection) -> None:
    """Upgrade-user backfill of five_hour_block_models.

    Fires when schema_migrations has no row for
    '001_five_hour_block_models_backfill_v1' AND five_hour_blocks is
    non-empty.

    Iterates parent rows, re-walks session_entries per block via
    _compute_block_totals(..., skip_sync=False), and INSERT OR IGNORE's
    child rows. skip_sync=False is intentional: cache.db can be empty
    or stale at open_db() time (deleted, imported, restored from
    backup), and querying it as-is would close the gate forever with
    zero children even though JSONL exists on disk. sync_cache only
    touches cache.db; no open_db() recursion.

    Defensively cleans up orphan child rows (block_id referencing a
    parent that no longer exists) before re-backfilling, so manual
    `DELETE FROM five_hour_blocks` followed by re-backfill doesn't
    leave duplicates.

    The gate closes regardless of how many child rows were written —
    empty `session_entries` for a block (real users with API/web-only
    blocks) yields zero child rows but MUST still be marked applied
    (regression scenario Q2). The dispatcher central-stamps the
    schema_migrations marker on this handler's clean return (#140).
    """
    # Empty-table fast path: with no parent five_hour_blocks rows, this
    # backfill has nothing to do. Return cleanly so the dispatcher
    # central-stamps us as applied (#140) — replaces the prior
    # `has_blocks` outer gate from the pre-framework era.
    if not conn.execute("SELECT 1 FROM five_hour_blocks LIMIT 1").fetchone():
        return
    conn.execute("BEGIN")
    try:
        # Defensive: clean up any orphans from a prior parent rebuild.
        conn.execute(
            "DELETE FROM five_hour_block_models "
            "WHERE block_id NOT IN (SELECT id FROM five_hour_blocks)"
        )

        rows = conn.execute(
            "SELECT id, five_hour_window_key, block_start_at, "
            "       last_observed_at_utc "
            "  FROM five_hour_blocks"
        ).fetchall()
        for row in rows:
            block_start_dt = parse_iso_datetime(
                row["block_start_at"],
                "five_hour_blocks.block_start_at",
            )
            last_obs_dt = parse_iso_datetime(
                row["last_observed_at_utc"],
                "five_hour_blocks.last_observed_at_utc",
            )
            # skip_sync=False: ingest JSONL deltas before walking
            # entries. If the user's cache.db is empty/stale at the
            # moment open_db() fires this gate (e.g., cache.db deleted,
            # stats.db imported from another machine), querying the
            # cache as-is would return zero entries and we'd close the
            # gate forever with empty children. sync_cache(conn)
            # operates on cache.db only — it does NOT call open_db(),
            # so there is no recursion risk.
            totals = _compute_block_totals(
                block_start_dt, last_obs_dt, skip_sync=False,
            )
            if totals.get("by_model"):
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO five_hour_block_models (
                      block_id, five_hour_window_key, model,
                      input_tokens, output_tokens,
                      cache_create_tokens, cache_read_tokens,
                      cost_usd, entry_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            int(row["id"]),
                            int(row["five_hour_window_key"]),
                            model,
                            b["input_tokens"],
                            b["output_tokens"],
                            b["cache_create_tokens"],
                            b["cache_read_tokens"],
                            b["cost_usd"],
                            b["entry_count"],
                        )
                        for model, b in totals["by_model"].items()
                    ],
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise


# === Region 4: 002 handler (was bin/cctally:11347-11437) ===

@stats_migration("002_five_hour_block_projects_backfill_v1")
def _backfill_five_hour_block_projects(conn: sqlite3.Connection) -> None:
    """Upgrade-user backfill of five_hour_block_projects.

    Mirror of _backfill_five_hour_block_models but writes by_project
    buckets. Cleans up orphan child rows defensively before the main
    loop. The dispatcher central-stamps the projects-side
    schema_migrations marker on clean return (#140), so the gate closes
    for empty-row backfills too.
    """
    # Empty-table fast path: with no parent five_hour_blocks rows, this
    # backfill has nothing to do. Return cleanly so the dispatcher
    # central-stamps us as applied (#140) — replaces the prior
    # `has_blocks` outer gate from the pre-framework era.
    if not conn.execute("SELECT 1 FROM five_hour_blocks LIMIT 1").fetchone():
        return
    conn.execute("BEGIN")
    try:
        conn.execute(
            "DELETE FROM five_hour_block_projects "
            "WHERE block_id NOT IN (SELECT id FROM five_hour_blocks)"
        )

        rows = conn.execute(
            "SELECT id, five_hour_window_key, block_start_at, "
            "       last_observed_at_utc "
            "  FROM five_hour_blocks"
        ).fetchall()
        for row in rows:
            block_start_dt = parse_iso_datetime(
                row["block_start_at"],
                "five_hour_blocks.block_start_at",
            )
            last_obs_dt = parse_iso_datetime(
                row["last_observed_at_utc"],
                "five_hour_blocks.last_observed_at_utc",
            )
            # See _backfill_five_hour_block_models for the same
            # skip_sync=False rationale: ingest JSONL deltas first so
            # an empty/stale cache.db doesn't permanently close the
            # gate with zero rows. sync_cache only touches cache.db,
            # so there is no open_db() recursion risk.
            totals = _compute_block_totals(
                block_start_dt, last_obs_dt, skip_sync=False,
            )
            if totals.get("by_project"):
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO five_hour_block_projects (
                      block_id, five_hour_window_key, project_path,
                      input_tokens, output_tokens,
                      cache_create_tokens, cache_read_tokens,
                      cost_usd, entry_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            int(row["id"]),
                            int(row["five_hour_window_key"]),
                            project_path,
                            b["input_tokens"],
                            b["output_tokens"],
                            b["cache_create_tokens"],
                            b["cache_read_tokens"],
                            b["cost_usd"],
                            b["entry_count"],
                        )
                        for project_path, b in totals["by_project"].items()
                    ],
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise



# === Region 5: Error sentinel (was bin/cctally:11439-11717) ===

def _log_migration_error(*, name: str, exc: BaseException, tb: str) -> None:
    """Append a migration failure record to MIGRATION_ERROR_LOG_PATH.

    Failure-tolerant: any IO error here is logged via eprint and swallowed
    so a logging-side failure doesn't shadow the original migration error.
    """
    # POSIX append() is atomic per write() syscall up to PIPE_BUF (~4 KiB).
    # A multi-line traceback exceeding that can interleave with a concurrent
    # appender, producing a corrupt block. _render_migration_error_banner's
    # parser handles malformed entries via the generic-copy fallback (no
    # crash). Acceptable per "best effort" design — concurrent migration
    # failures are vanishingly rare since open_db() serializes via WAL.
    try:
        _cctally_core.LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = now_utc_iso()
        one_line_err = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
        indented_tb = "\n".join("  " + line for line in tb.rstrip().splitlines())
        block = f"[{ts}] {name}\n  {one_line_err}\n{indented_tb}\n\n"
        with open(_cctally_core.MIGRATION_ERROR_LOG_PATH, "a") as fh:
            fh.write(block)
    except Exception as log_exc:
        eprint(f"[migration-error-log] failed to write: {log_exc}")


def _clear_migration_error_log_entries(name: str) -> None:
    """Remove all entries tagged with ``name`` from the migration error log.

    If the resulting file is empty (or doesn't exist to begin with), unlink
    it. Failure-tolerant: any IO error is swallowed; the log file is
    best-effort.
    """
    # Race: read → filter → write is non-atomic. Concurrent writers (rare —
    # usually only happens if a manual cctally cmd races a status-line tick)
    # can lose one log entry or briefly resurrect a stale banner. Acceptable
    # per "best effort" design (see Q1=A in design discussion); the next
    # successful migration auto-clears, and worst case the user sees one
    # extra banner cycle. Not worth fcntl.flock complexity for failure-rare
    # code path.
    try:
        if not _cctally_core.MIGRATION_ERROR_LOG_PATH.exists():
            return
        content = _cctally_core.MIGRATION_ERROR_LOG_PATH.read_text()
        # Entries are separated by "\n\n". Each entry's first line is
        # "[ts] <name>".
        blocks = [b for b in content.split("\n\n") if b.strip()]
        kept = []
        for block in blocks:
            first_line = block.splitlines()[0] if block.splitlines() else ""
            # Match: line ends with " <name>" (after the timestamp prefix).
            if first_line.endswith(f" {name}"):
                continue
            kept.append(block)
        if not kept:
            _cctally_core.MIGRATION_ERROR_LOG_PATH.unlink()
            return
        _cctally_core.MIGRATION_ERROR_LOG_PATH.write_text("\n\n".join(kept) + "\n\n")
    except Exception as exc:
        eprint(
            f"[migration-error-log] failed to clear entries for {name}: {exc}"
        )


def _render_migration_error_banner() -> str | None:
    """Return a one-line banner string from the migration error log, or
    ``None`` if there is nothing to surface.

    Parses the most recent entry's first line for the migration name and
    timestamp. Falls back to a generic message on parse failure.
    """
    if not _cctally_core.MIGRATION_ERROR_LOG_PATH.exists():
        return None
    try:
        content = _cctally_core.MIGRATION_ERROR_LOG_PATH.read_text()
    except Exception:
        return None
    if not content.strip():
        return None
    blocks = [b for b in content.split("\n\n") if b.strip()]
    if not blocks:
        return None
    most_recent = blocks[-1].splitlines()[0]
    # most_recent format: "[2026-05-01T12:34:56Z] merge_5h_block_duplicates_v1"
    if most_recent.startswith("[") and "] " in most_recent:
        try:
            ts_part, _, name_part = most_recent[1:].partition("] ")
            ts = ts_part.strip()
            name = name_part.strip()
            if ts and name:
                return (
                    f"⚠ cctally: migration `{name}` failed at {ts}. "
                    f"See {_cctally_core.MIGRATION_ERROR_LOG_PATH}"
                )
        except Exception:
            pass
    return (
        f"⚠ cctally: migration error logged. "
        f"See {_cctally_core.MIGRATION_ERROR_LOG_PATH}"
    )


# Suppression list — silent / background / internal commands. The banner
# would either pollute machine-readable output (record-usage / hook-tick /
# refresh-usage when consumed by status-line shells, sync-week / cache-sync
# when scripted) or have nowhere to land (tui's full-screen rich render).
# `setup` is special-cased (banner shown only with --status); `dashboard`
# is also special-cased so cmd_dashboard can print at server startup
# instead of swallowing into early stdout.
_BANNER_SUPPRESSED_COMMANDS = frozenset({
    "record-usage",    # invoked every status-line tick + hook fire; banner would spam
    "hook-tick",       # background; CC hook fire, log-only output
    "sync-week",       # background; called from refresh-usage path
    "cache-sync",      # background; bulk operation, no banner needed
    "refresh-usage",   # background; OAuth fetch + record-usage chain
    "tui",             # rich Live mode takes over the screen; banner would be clobbered
    "db",              # `db status` shows failure state in its own output;
                       # `db skip` / `db unskip` are mid-fix — banner would be redundant.
    "doctor",          # consolidates migration + update banner state into its
                       # own report; double-printing the banner would duplicate
                       # findings doctor already surfaces structurally.
    "pricing-check",   # read-only diagnostic emitting structured (often JSON)
                       # output; banner noise pollutes the report + scripted
                       # `--json` pipelines. Same posture as doctor.
    "repair-symlinks", # invoked by npm postinstall; no banner during install
    "blocks",          # stdout-formatted table replacing `ccusage blocks`;
                       # stderr noise pollutes the visually-aligned report and
                       # confuses scripted pipelines piping via `2>&1`.
                       # Banner still lands on the next interactive non-report
                       # command (`report`, `weekly`, `percent-breakdown`, etc.).
    # Note: `setup` carve-out handled separately (only suppressed w/o --status).
    # Note: `dashboard` carve-out handled separately (banner printed in cmd_dashboard).
})


# === Region 6: _print_migration_error_banner_if_needed (was bin/cctally:11719-11760) ===

def _print_migration_error_banner_if_needed(args) -> None:
    """Print a one-line warning banner if the migration error log has
    entries.

    Suppression rules:
      - Sentinel file doesn't exist or is empty -> no banner.
      - Command in ``_BANNER_SUPPRESSED_COMMANDS`` -> no banner.
      - ``setup`` without --status -> no banner. ``setup --status`` -> banner.
      - ``dashboard`` -> handled inside cmd_dashboard, skipped here.
      - Machine-stdout modes (--status-line and similar single-line shell-
        substituted integrations) -> no banner anywhere; both stdout AND
        stderr are unsafe surfaces (status-line integration is
        `$(cmd 2>/dev/null)`). User sees banner on next interactive cmd.
      - --json mode (any command exposing it, including diff's
        dest="emit_json") -> banner goes to STDERR instead of stdout to
        keep stdout JSON parsable.
    """
    c = _cctally()
    cmd = getattr(args, "command", None)
    if cmd is None or cmd in _BANNER_SUPPRESSED_COMMANDS:
        return
    # args.status is meaningful only for cmd == "setup" (--status flag);
    # any future subcommand adding --status with default dest="status"
    # would inherit show-banner behavior unintentionally. Audit at that time.
    if cmd == "setup" and not getattr(args, "status", False):
        return
    if cmd == "dashboard":
        return  # cmd_dashboard handles its own banner at server startup

    # Machine-stdout suppression: status-line and similar single-line scripted
    # integrations swallow both stdout and stderr — banner has no safe surface,
    # so skip entirely. User will see the banner on their next interactive cmd.
    # _args_emit_machine_stdout / _args_emit_json stay in bin/cctally
    # (shared with the update-banner gate); reach via the call-time accessor.
    if c._args_emit_machine_stdout(args):
        return

    banner_msg = _render_migration_error_banner()
    if banner_msg is None:
        return

    # JSON mode: banner goes to STDERR to keep stdout JSON parseable.
    json_mode = c._args_emit_json(args)
    print(banner_msg, file=sys.stderr if json_mode else sys.stdout)



# === Region 7: 003 handler (was bin/cctally:11762-12084) ===

@stats_migration("003_merge_5h_block_duplicates_v1")
def _migration_merge_5h_block_duplicates_v1(conn: sqlite3.Connection) -> None:
    """One-shot migration: merge ``five_hour_blocks`` rows that represent
    the same physical 5h window but have different ``five_hour_window_key``
    values (boundary-jitter forks; F4-incident class).

    Algorithm
    ─────────
    1. Load every parent row ordered by ``five_hour_resets_at``.
    2. Greedy-group: a new row joins the current group iff
       ``epoch - group_anchor_epoch <= 1800`` (3 × the 600 s floor); else
       flush and start a new group at this row's epoch.
    3. For each group of size ≥ 2:
       a. Canonical = the row with the earliest ``first_observed_at_utc``
          (write-once anchor — same precedence the 5h-block live-write
          path treats as immutable).
       b. ``weekly_usage_snapshots.five_hour_window_key`` IN (dropped) →
          rewritten to canonical so the latest-snapshot lookup returns
          one canonical key.
       c. ``five_hour_milestones`` are write-once per
          (canonical_block, percent_threshold). For each threshold seen
          across the group, KEEP the row with the earliest
          ``captured_at_utc`` and re-FK it onto the canonical block;
          DELETE the rest. (Earliest-captured guards the spec invariant
          [Write-once milestones]: never overwrite a historical milestone
          with a later — and therefore higher-cost — observation.)
       d. ``five_hour_block_models`` / ``five_hour_block_projects`` for
          dropped windows → DELETE outright. They're recompute-every-tick
          rollup-children (CLAUDE.md spec); the next ``record-usage`` will
          repopulate the canonical block's rows from
          ``session_entries`` via ``_compute_block_totals``.
       e. MERGE group-wide aggregates into canonical: ``last_observed_at_utc``,
          ``final_five_hour_percent``, ``seven_day_pct_at_block_end``,
          ``crossed_seven_day_reset``, ``is_closed``, and the five
          ``total_*`` columns. Rationale: each duplicate row received
          ticks only while ITS specific (jittered) ``five_hour_window_key``
          was current, so the rows hold complementary slices of the same
          physical 5h window. Without this merge, canonical (= earliest
          ``first_observed_at_utc``) would freeze at the earliest slice,
          and CLOSED blocks (no future tick) would permanently
          under-report. Read-only access to the rows already in memory —
          no ``cache.db`` open, honoring the migration's external-state
          constraint.
       f. DELETE the dropped parent ``five_hour_blocks`` rows.

    Single ``BEGIN`` / ``COMMIT`` envelope. On any exception the whole
    migration ROLLBACKs and re-raises; the missing ``schema_migrations``
    row makes the next ``open_db`` call retry idempotently.

    FK on ``five_hour_milestones.block_id`` is documentation-only (no
    SQLite cascade — see CLAUDE.md), so all FK rewrites are explicit
    ``UPDATE``s here.
    """
    conn.execute("BEGIN")
    try:
        blocks = conn.execute(
            """
            SELECT id, five_hour_window_key, five_hour_resets_at,
                   first_observed_at_utc, last_observed_at_utc,
                   final_five_hour_percent,
                   seven_day_pct_at_block_end,
                   crossed_seven_day_reset, is_closed,
                   total_input_tokens, total_output_tokens,
                   total_cache_create_tokens, total_cache_read_tokens,
                   total_cost_usd
              FROM five_hour_blocks
             ORDER BY five_hour_resets_at ASC
            """
        ).fetchall()

        # Convert resets_at to epoch for distance math. A row whose
        # five_hour_resets_at fails to parse is left alone in its own
        # singleton group (defensive — should not happen on data
        # written by record-usage, but better to skip than raise).
        rows: list[tuple[int, dict]] = []
        for b in blocks:
            try:
                ep = int(parse_iso_datetime(
                    b["five_hour_resets_at"],
                    "five_hour_blocks.five_hour_resets_at",
                ).timestamp())
            except (ValueError, TypeError):
                continue
            rows.append((ep, dict(b)))

        # Defensive: SQL ORDER BY is lex-ordered. For the columns we
        # read today (consistently +00:00 form), lex == chronological.
        # Re-sort by parsed epoch in Python so a future code path
        # accidentally writing `Z` form into five_hour_resets_at can't
        # mis-group across format boundaries.
        rows.sort(key=lambda r: r[0])

        # Greedy-group by proximity to the group's anchor epoch.
        groups: list[list[tuple[int, dict]]] = []
        cur_group: list[tuple[int, dict]] = []
        cur_anchor: int | None = None
        for ep, row in rows:
            if (
                cur_anchor is None
                or (ep - cur_anchor) <= _cctally()._FIVE_HOUR_JITTER_FLOOR_SECONDS * 3
            ):
                cur_group.append((ep, row))
                if cur_anchor is None:
                    cur_anchor = ep
            else:
                groups.append(cur_group)
                cur_group = [(ep, row)]
                cur_anchor = ep
        if cur_group:
            groups.append(cur_group)

        for group in groups:
            if len(group) < 2:
                continue

            # Canonical wins by earliest first_observed_at_utc — same
            # write-once precedence as the live upsert path.
            # NULL first_observed_at_utc shouldn't happen post-schema-
            # NOT-NULL, but defensive against legacy rows; NULL rows
            # lose canonical-pick tiebreak (sort LAST via True>False).
            # Empty-string fallback in the second tuple element keeps
            # SQLite NULLs comparable; a NULL row only becomes
            # canonical if EVERY row in the group is NULL.
            group_sorted = sorted(
                group,
                key=lambda g: (
                    g[1]["first_observed_at_utc"] is None,
                    g[1]["first_observed_at_utc"] or "",
                ),
            )
            canonical = group_sorted[0][1]
            dropped = [g[1] for g in group_sorted[1:]]
            dropped_keys = [d["five_hour_window_key"] for d in dropped]
            dropped_ids = [d["id"] for d in dropped]

            # (b) Re-key snapshots so latest-snapshot lookup returns
            # the canonical key.
            placeholders_keys = ",".join("?" * len(dropped_keys))
            conn.execute(
                f"UPDATE weekly_usage_snapshots "
                f"   SET five_hour_window_key = ? "
                f" WHERE five_hour_window_key IN ({placeholders_keys})",
                [canonical["five_hour_window_key"], *dropped_keys],
            )

            # (c) Milestones: per-threshold dedup, keep earliest
            # captured_at_utc, re-FK keepers to canonical.
            #
            # Defensive widening (Codex r2 finding 1, spec §3.4): if
            # migration 006 has already landed and added ``reset_event_id``,
            # key the dedup on ``(percent_threshold, reset_event_id)`` so
            # we don't silently collapse legitimately distinct pre/post-
            # credit rows at the same physical threshold. On the legacy
            # upgrade path (column doesn't exist yet because 003 runs
            # before 006 in migration order), ``has_seg`` is False and the
            # dedup key collapses to ``(threshold, 0)`` — byte-identical
            # to the original threshold-only shape. PRAGMA probe rather
            # than version-detect so the path also covers operator
            # re-runs (e.g. ``cctally db unskip 003_*``) post-006.
            ms_cols = {
                str(r[1])
                for r in conn.execute(
                    "PRAGMA table_info(five_hour_milestones)"
                ).fetchall()
            }
            has_seg = "reset_event_id" in ms_cols
            ms_id_placeholders = ",".join(
                "?" * (len(dropped_ids) + 1)
            )
            if has_seg:
                all_milestones = conn.execute(
                    f"SELECT id, percent_threshold, captured_at_utc, "
                    f"       reset_event_id "
                    f"  FROM five_hour_milestones "
                    f" WHERE block_id IN ({ms_id_placeholders})",
                    [canonical["id"], *dropped_ids],
                ).fetchall()
            else:
                all_milestones = conn.execute(
                    f"SELECT id, percent_threshold, captured_at_utc "
                    f"  FROM five_hour_milestones "
                    f" WHERE block_id IN ({ms_id_placeholders})",
                    [canonical["id"], *dropped_ids],
                ).fetchall()
            by_key: dict[tuple[int, int], dict] = {}
            for m in all_milestones:
                seg = int(m["reset_event_id"]) if has_seg else 0
                key = (int(m["percent_threshold"]), seg)
                md = dict(m)
                if (
                    key not in by_key
                    or md["captured_at_utc"]
                    < by_key[key]["captured_at_utc"]
                ):
                    by_key[key] = md
            keep_ids = {m["id"] for m in by_key.values()}
            # DELETE non-keepers BEFORE rekeying keepers. Otherwise, when
            # both canonical and a dropped block hold a milestone for the
            # same physical key and the dropped row's milestone is the
            # earlier keeper, UPDATEing it to the canonical key collides
            # with canonical's still-present non-keeper on UNIQUE
            # (either the 2-col legacy shape or the 3-col post-006 shape),
            # rolling back the migration. After this DELETE the only
            # milestones referencing dropped_keys are the keepers
            # themselves (one per dedup key), so the UPDATE loop below is
            # collision-free.
            non_keep_ids = [
                m["id"] for m in all_milestones if m["id"] not in keep_ids
            ]
            if non_keep_ids:
                nk_placeholders = ",".join("?" * len(non_keep_ids))
                conn.execute(
                    f"DELETE FROM five_hour_milestones "
                    f" WHERE id IN ({nk_placeholders})",
                    non_keep_ids,
                )
            for m in by_key.values():
                conn.execute(
                    "UPDATE five_hour_milestones "
                    "   SET block_id = ?, "
                    "       five_hour_window_key = ? "
                    " WHERE id = ?",
                    (
                        canonical["id"],
                        canonical["five_hour_window_key"],
                        m["id"],
                    ),
                )

            # (d) Children rollup tables — delete dropped rows'
            # children. Recompute on next record-usage tick repopulates
            # canonical's rows.
            for tbl in (
                "five_hour_block_models",
                "five_hour_block_projects",
            ):
                conn.execute(
                    f"DELETE FROM {tbl} "
                    f" WHERE five_hour_window_key IN ({placeholders_keys})",
                    dropped_keys,
                )

            # (e) Merge group-wide aggregates into canonical BEFORE
            # deleting the dropped rows. Each duplicate row received
            # record-usage ticks for the slice of the 5h window during
            # which its specific (jittered) five_hour_window_key was
            # current — so their last_observed_at_utc / final_pct /
            # totals are complementary, not redundant. For closed /
            # historical blocks no future tick will fire, so without
            # this merge the canonical row would be permanently
            # frozen at the earliest-observation slice. Reads no
            # external state (still no cache.db open) — all values
            # come from rows we already SELECT'd above.
            #
            # Rules:
            #   - last_observed_at_utc → group MAX (lexicographic on
            #     canonical UTC-Z form == chronological).
            #   - final_five_hour_percent / seven_day_pct_at_block_end
            #     → values from the group row whose
            #     last_observed_at_utc is MAX (preserves the
            #     latest-observation snapshot rather than blindly
            #     taking MAX(percent), which could pick a glitched
            #     spike from a non-latest row).
            #   - crossed_seven_day_reset / is_closed → group MAX
            #     (any row flagged ⇒ canonical flagged).
            #   - total_*_tokens / total_cost_usd → group MAX.
            #     _compute_block_totals always recomputes over
            #     [block_start_at, captured_at_utc], so the row with
            #     the latest captured_at has the strict-superset
            #     totals; MAX picks that row's values without needing
            #     to track which row "wins".
            group_rows = [g[1] for g in group_sorted]
            latest = max(
                group_rows,
                key=lambda r: r["last_observed_at_utc"] or "",
            )
            merged_crossed = max(
                int(r["crossed_seven_day_reset"] or 0)
                for r in group_rows
            )
            merged_is_closed = max(
                int(r["is_closed"] or 0) for r in group_rows
            )
            merged_in = max(
                int(r["total_input_tokens"] or 0) for r in group_rows
            )
            merged_out = max(
                int(r["total_output_tokens"] or 0) for r in group_rows
            )
            merged_cc = max(
                int(r["total_cache_create_tokens"] or 0)
                for r in group_rows
            )
            merged_cr = max(
                int(r["total_cache_read_tokens"] or 0)
                for r in group_rows
            )
            merged_cost = max(
                float(r["total_cost_usd"] or 0.0) for r in group_rows
            )
            conn.execute(
                """
                UPDATE five_hour_blocks
                   SET last_observed_at_utc       = ?,
                       final_five_hour_percent    = ?,
                       seven_day_pct_at_block_end = ?,
                       crossed_seven_day_reset    = ?,
                       is_closed                  = ?,
                       total_input_tokens         = ?,
                       total_output_tokens        = ?,
                       total_cache_create_tokens  = ?,
                       total_cache_read_tokens    = ?,
                       total_cost_usd             = ?,
                       last_updated_at_utc        = ?
                 WHERE id = ?
                """,
                (
                    latest["last_observed_at_utc"],
                    latest["final_five_hour_percent"],
                    latest["seven_day_pct_at_block_end"],
                    merged_crossed,
                    merged_is_closed,
                    merged_in,
                    merged_out,
                    merged_cc,
                    merged_cr,
                    merged_cost,
                    now_utc_iso(),
                    canonical["id"],
                ),
            )

            # (f) Delete dropped parent block rows.
            id_placeholders = ",".join("?" * len(dropped_ids))
            conn.execute(
                f"DELETE FROM five_hour_blocks "
                f" WHERE id IN ({id_placeholders})",
                dropped_ids,
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise


# === Region 7b: 004 handler — self-heal forked week_start_date buckets ===

@stats_migration("004_heal_forked_week_start_date_buckets")
def _migration_heal_forked_week_start_date_buckets(conn: sqlite3.Connection) -> None:
    """One-shot self-heal: merge rows whose ``week_start_date`` was forked
    by a host-TZ contamination at insert time (pre-fix
    ``_derive_week_from_payload`` / ``pick_week_selection`` took ``.date()``
    of a host-local-TZ datetime instead of the canonical UTC ISO).

    Defense-in-depth pairing with commit ``6def75f8`` (UTC-anchor the
    bucket-key date at the writer). The writer fix prevents NEW ghost
    rows on the FIXED binary, but a still-deployed older binary (e.g.,
    npm v1.7.0 on the user's machine) can keep writing ghosts every
    time the host process inherits a non-UTC ``TZ``. This migration
    auto-merges any such ghost rows on the next ``open_db()``, so the
    in-place corruption gets cleaned up regardless of which binary
    happened to write it.

    Invariant: for every row with ``week_start_at IS NOT NULL``,
    ``week_start_date`` MUST equal ``substr(week_start_at, 1, 10)`` (the
    canonical UTC calendar day of the subscription-week boundary).

    Per-table action when the invariant is violated:

      * ``weekly_usage_snapshots`` / ``weekly_cost_snapshots`` — no
        UNIQUE constraint on ``(week_start_date, ...)``, so simply
        UPDATE both date columns to ``substr(week_start_at, 1, 10)`` /
        ``substr(week_end_at, 1, 10)``. The ghost rows merge into the
        canonical bucket as additional samples on the same physical
        week.

      * ``percent_milestones`` — UNIQUE(week_start_date,
        percent_threshold). For each ghost row: if a canonical-keyed
        row at the same threshold already exists, DELETE the ghost
        (canonical preserves the original alerted_at and the genuine
        crossing's cumulative cost). Otherwise UPDATE.

    Idempotent: a second invocation finds zero forked rows and is a
    no-op. Forward-only — never regresses canonical rows. Reads no
    external state (no ``cache.db`` open, no JSONL walk).

    Empty-table fast path: when none of the three tables has a forked
    row, return without opening a transaction (the dispatcher
    central-stamps the marker on clean return, #140).

    Spec hook: paired regression test in
    ``tests/test_heal_forked_week_start_date_buckets.py``.
    """
    # Empty-fork fast path. UNION ALL across the three tables; one
    # SELECT 1 / LIMIT 1 short-circuits on the first violator. When
    # zero rows are forked, skip the BEGIN/UPDATE block entirely and
    # return (the dispatcher central-stamps the marker, #140).
    has_fork_row = conn.execute(
        """
        SELECT 1 FROM (
          SELECT 1 FROM weekly_usage_snapshots
           WHERE week_start_at IS NOT NULL
             AND week_start_date != substr(week_start_at, 1, 10)
          UNION ALL
          SELECT 1 FROM weekly_cost_snapshots
           WHERE week_start_at IS NOT NULL
             AND week_start_date != substr(week_start_at, 1, 10)
          UNION ALL
          SELECT 1 FROM percent_milestones
           WHERE week_start_at IS NOT NULL
             AND week_start_date != substr(week_start_at, 1, 10)
        ) LIMIT 1
        """
    ).fetchone()
    if not has_fork_row:
        return

    conn.execute("BEGIN")
    try:
        # (a) weekly_usage_snapshots — no UNIQUE; straight UPDATE.
        conn.execute(
            """
            UPDATE weekly_usage_snapshots
               SET week_start_date = substr(week_start_at, 1, 10),
                   week_end_date   = substr(week_end_at,   1, 10)
             WHERE week_start_at IS NOT NULL
               AND week_start_date != substr(week_start_at, 1, 10)
            """
        )

        # (b) weekly_cost_snapshots — same.
        conn.execute(
            """
            UPDATE weekly_cost_snapshots
               SET week_start_date = substr(week_start_at, 1, 10),
                   week_end_date   = substr(week_end_at,   1, 10)
             WHERE week_start_at IS NOT NULL
               AND week_start_date != substr(week_start_at, 1, 10)
            """
        )

        # (c) percent_milestones — UNIQUE(week_start_date,
        # percent_threshold). DELETE ghosts whose canonical-keyed
        # counterpart already exists at the same threshold BEFORE
        # UPDATEing the rest, otherwise the UPDATE collides on UNIQUE
        # and rolls back the migration.
        conn.execute(
            """
            DELETE FROM percent_milestones
             WHERE week_start_at IS NOT NULL
               AND week_start_date != substr(week_start_at, 1, 10)
               AND EXISTS (
                     SELECT 1 FROM percent_milestones canon
                      WHERE canon.week_start_date
                            = substr(percent_milestones.week_start_at, 1, 10)
                        AND canon.percent_threshold
                            = percent_milestones.percent_threshold
                   )
            """
        )
        conn.execute(
            """
            UPDATE percent_milestones
               SET week_start_date = substr(week_start_at, 1, 10),
                   week_end_date   = substr(week_end_at,   1, 10)
             WHERE week_start_at IS NOT NULL
               AND week_start_date != substr(week_start_at, 1, 10)
            """
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise


@stats_migration("005_percent_milestones_reset_event_id")
def _migration_percent_milestones_reset_event_id(conn: sqlite3.Connection) -> None:
    """Add ``reset_event_id`` to ``percent_milestones`` so post-credit
    threshold crossings can coexist with pre-credit ones for the same
    ``(week_start_date, percent_threshold)``.

    Sentinel: ``0`` = pre-credit / no event. Existing rows backfill to
    ``0`` via the ``DEFAULT 0`` clause on the new column.

    The new UNIQUE constraint is
    ``UNIQUE(week_start_date, percent_threshold, reset_event_id)`` so the
    same (week, threshold) pair can land twice if a goodwill credit
    re-opens the segment under a fresh ``week_reset_events.id``. SQLite
    can't ALTER a UNIQUE constraint in place — we use the
    rename-recreate-copy idiom.

    Companion live-path edits: ``cmd_record_usage`` now stamps the
    active segment (the latest ``week_reset_events.id`` for the
    current ``new_week_end_at``, else 0) into ``reset_event_id``; the
    in-place credit detection branch can re-fire the same threshold
    after a credit.

    Idempotent: a second invocation finds the column already present
    and returns. Empty-table fast path: when the column is already
    present this handler is a no-op — no schema edit needed (the
    dispatcher central-stamps the marker on clean return, #140).
    """
    # Fast-path probe: column already present means a prior run of this
    # migration (or a fresh-install fast-stamp from the dispatcher that
    # already picked up the new live-schema CREATE TABLE) has done the
    # work. Return; the dispatcher central-stamps the marker (#140).
    cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(percent_milestones)").fetchall()
    }
    if "reset_event_id" in cols:
        return

    conn.execute("BEGIN")
    try:
        # Add the column with sentinel 0 default (covers existing rows).
        conn.execute(
            "ALTER TABLE percent_milestones "
            "ADD COLUMN reset_event_id INTEGER NOT NULL DEFAULT 0"
        )
        # SQLite can't ALTER a UNIQUE constraint in place; rename, recreate
        # with the new 3-column UNIQUE, copy, drop. Preserves ids and every
        # existing column (including those added by add_column_if_missing:
        # five_hour_percent_at_crossing, alerted_at).
        conn.execute(
            "ALTER TABLE percent_milestones RENAME TO percent_milestones_old_005"
        )
        conn.execute(
            """
            CREATE TABLE percent_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                week_start_date TEXT NOT NULL,
                week_end_date TEXT NOT NULL,
                week_start_at TEXT,
                week_end_at TEXT,
                percent_threshold INTEGER NOT NULL,
                cumulative_cost_usd REAL NOT NULL,
                marginal_cost_usd REAL,
                usage_snapshot_id INTEGER NOT NULL,
                cost_snapshot_id INTEGER NOT NULL,
                five_hour_percent_at_crossing REAL,
                alerted_at TEXT,
                reset_event_id INTEGER NOT NULL DEFAULT 0,
                UNIQUE(week_start_date, percent_threshold, reset_event_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO percent_milestones (
                id, captured_at_utc, week_start_date, week_end_date,
                week_start_at, week_end_at, percent_threshold,
                cumulative_cost_usd, marginal_cost_usd,
                usage_snapshot_id, cost_snapshot_id,
                five_hour_percent_at_crossing, alerted_at, reset_event_id
            )
            SELECT id, captured_at_utc, week_start_date, week_end_date,
                   week_start_at, week_end_at, percent_threshold,
                   cumulative_cost_usd, marginal_cost_usd,
                   usage_snapshot_id, cost_snapshot_id,
                   five_hour_percent_at_crossing, alerted_at,
                   reset_event_id
              FROM percent_milestones_old_005
            """
        )
        conn.execute("DROP TABLE percent_milestones_old_005")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@stats_migration("006_five_hour_milestones_reset_event_id")
def _migration_five_hour_milestones_reset_event_id(conn: sqlite3.Connection) -> None:
    """Add ``reset_event_id`` to ``five_hour_milestones`` so post-credit
    threshold crossings can coexist with pre-credit ones for the same
    ``(five_hour_window_key, percent_threshold)``.

    Sentinel: ``0`` = pre-credit / no event. Existing rows backfill to
    ``0`` via the ``DEFAULT 0`` clause on the new column.

    The new UNIQUE constraint is
    ``UNIQUE(five_hour_window_key, percent_threshold, reset_event_id)`` so
    the same (window_key, threshold) pair can land twice if a goodwill
    credit re-opens the segment under a fresh ``five_hour_reset_events.id``.
    SQLite can't ALTER a UNIQUE constraint in place — we use the
    rename-recreate-copy idiom (same as migration 005 did for
    ``percent_milestones``).

    Companion live-path edits land at (Task 2 of issue #43):
      - bin/_cctally_record.py — 5h milestone INSERT + alert paths
        (Sites A-E in spec §3.3); grep ``active_reset_event_id`` to
        locate (line numbers drift per ``gotcha_cited_line_numbers_stale``)
      - bin/_cctally_dashboard.py — alerts list row-identity widening
        (Site F in spec §3.3 — bucket C per spec §3.2's three-bucket model);
        grep ``reset_event_id`` near the 5h alerts SELECT

    Idempotent: a second invocation finds the column already present and
    returns. Empty-table fast path: when the column is already present
    (fresh-install fast-stamp from the dispatcher because the live
    ``CREATE TABLE IF NOT EXISTS five_hour_milestones`` already carries
    the new shape — REQUIRED for fresh-install correctness per spec §3.2),
    this handler is a no-op — no schema edit needed (the dispatcher
    central-stamps the marker on clean return, #140).
    """
    # Fast-path probe: column already present means a prior run of this
    # migration (or a fresh-install fast-stamp from the dispatcher that
    # already picked up the new live-schema CREATE TABLE) has done the
    # work. Return; the dispatcher central-stamps the marker (#140).
    cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(five_hour_milestones)").fetchall()
    }
    if "reset_event_id" in cols:
        return

    conn.execute("BEGIN")
    try:
        # Add the column with sentinel 0 default (covers existing rows).
        conn.execute(
            "ALTER TABLE five_hour_milestones "
            "ADD COLUMN reset_event_id INTEGER NOT NULL DEFAULT 0"
        )
        # SQLite can't ALTER a UNIQUE constraint in place; rename, recreate
        # with the new 3-column UNIQUE, copy, drop. Preserves ids and every
        # existing column (including those added by add_column_if_missing:
        # alerted_at).
        conn.execute(
            "ALTER TABLE five_hour_milestones "
            "RENAME TO five_hour_milestones_old_006"
        )
        conn.execute(
            """
            CREATE TABLE five_hour_milestones (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id                    INTEGER NOT NULL,
                five_hour_window_key        INTEGER NOT NULL,
                percent_threshold           INTEGER NOT NULL,
                captured_at_utc             TEXT    NOT NULL,
                usage_snapshot_id           INTEGER NOT NULL,
                block_input_tokens          INTEGER NOT NULL DEFAULT 0,
                block_output_tokens         INTEGER NOT NULL DEFAULT 0,
                block_cache_create_tokens   INTEGER NOT NULL DEFAULT 0,
                block_cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
                block_cost_usd              REAL    NOT NULL DEFAULT 0,
                marginal_cost_usd           REAL,
                seven_day_pct_at_crossing   REAL,
                alerted_at                  TEXT,
                reset_event_id              INTEGER NOT NULL DEFAULT 0,
                UNIQUE(five_hour_window_key, percent_threshold, reset_event_id),
                FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO five_hour_milestones (
                id, block_id, five_hour_window_key, percent_threshold,
                captured_at_utc, usage_snapshot_id,
                block_input_tokens, block_output_tokens,
                block_cache_create_tokens, block_cache_read_tokens,
                block_cost_usd, marginal_cost_usd,
                seven_day_pct_at_crossing, alerted_at, reset_event_id
            )
            SELECT id, block_id, five_hour_window_key, percent_threshold,
                   captured_at_utc, usage_snapshot_id,
                   block_input_tokens, block_output_tokens,
                   block_cache_create_tokens, block_cache_read_tokens,
                   block_cost_usd, marginal_cost_usd,
                   seven_day_pct_at_crossing, alerted_at, reset_event_id
              FROM five_hour_milestones_old_006
            """
        )
        # Recreate the block_id index that was attached to the original
        # table; the rename carried index metadata with the table, but
        # the new table needs its own index entry. Safe under
        # IF NOT EXISTS if the rename preserved it (it does in practice,
        # but the explicit recreate is defensive).
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_five_hour_milestones_block
            ON five_hour_milestones(block_id)
            """
        )
        conn.execute("DROP TABLE five_hour_milestones_old_006")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@stats_migration("007_observed_pre_credit_pct")
def _migration_observed_pre_credit_pct(conn: sqlite3.Connection) -> None:
    """Add ``observed_pre_credit_pct`` to ``week_reset_events`` so the
    race-defensive cleanup DELETE in the in-place weekly credit branch
    has a durable record of the pre-credit baseline we observed at
    write time — independent of how the upstream claude-statusline
    tool rounds replays.

    Today statusline replays cctally's ``hwm-7d`` value byte-identically,
    so the existing strict ``round(.,1)`` equality predicate is sound.
    Future-proofs against rounding drift: if Anthropic ever rounds the
    ``--percent`` payload differently from the OAuth API used by
    record-usage, or if statusline grows its own coarser rounding, a
    replay at e.g. 67.5 against a stored prior_pct = 67.4 would slip
    past strict equality and then dominate the reset-aware clamp's
    MAX over the post-credit segment. With the value stamped on the
    event row, the cleanup predicate widens to a 1.0pp tolerance band
    (issue #45) — wide enough to absorb single-digit drift, narrow
    enough that legitimate post-credit observations (≥25pp away by
    the in-place credit detection threshold's hypothesis) stay.

    Backfill: NULL on existing rows. NULL is legacy / never-stamped;
    the live cleanup's bind still uses the current tick's in-scope
    ``prior_pct`` (the value we just observed and would have stamped),
    so the cleanup remains correct on the very tick that writes the
    row. The stored value matters for future tooling that may re-run
    cleanup against an already-written event row.

    Companion live-path edits land in:
      - bin/cctally — CREATE TABLE adds the column for fresh installs.
      - bin/_cctally_record.py — in-place credit INSERT stamps
        ``observed_pre_credit_pct = prior_pct``; race-defensive DELETE
        switches from ``round(weekly_percent,1) = round(?,1)`` to
        ``ABS(weekly_percent - ?) < 1.0``.

    Idempotent: a second invocation finds the column already present
    and returns. Empty-column fast path: when the live CREATE TABLE
    already carries the column (fresh install), return without an ALTER
    (the dispatcher central-stamps the marker on clean return, #140).
    Simple ADD COLUMN — no UNIQUE constraint change, so no
    rename-recreate-copy needed (contrast migrations 005 / 006).
    """
    cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(week_reset_events)").fetchall()
    }
    if "observed_pre_credit_pct" in cols:
        return

    conn.execute("BEGIN")
    try:
        conn.execute(
            "ALTER TABLE week_reset_events "
            "ADD COLUMN observed_pre_credit_pct REAL"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# === Region 7b: Cross-DB migration gate helper (ccusage-parity prep) ===

def _gate_001_post_ingest_completed(
    cache_ro: sqlite3.Connection,
    claude_projects_dirs: pathlib.Path | list[pathlib.Path],
    *,
    data_present: bool = False,
) -> None:
    """Thin I/O shell over the pure ``resolve_upgrade_gate`` resolver.

    Derives the six ``UpgradeGateInputs`` from cache.db reads + the
    on-disk JSONL state, calls the resolver (the D3 truth table), and
    raises ``MigrationGateNotMet(reason)`` when the resolution is
    ``DEFER``. All decision logic lives in the resolver; this function
    does only I/O. (cctally-dev#93, spec D1/D3.)

    Input derivation
    ----------------
      * ``cache_001_state`` — ``"applied"`` if ``schema_migrations``
        carries ``001_dedup_highest_wins``; else ``"skipped"`` if
        ``schema_migrations_skipped`` carries it; else ``"pending"``.
      * ``walk_complete_since_001`` — the ``cache_meta``
        ``claude_ingest_walk_complete`` marker is present. ``sync_cache``
        writes it only after a clean full walk that began with 001 already
        applied, and cache 001 / rebuild / truncation clear it atomically
        (spec D5). This REPLACES the old ``session_files.last_ingested_at
        >= 001.applied_at_utc`` proof — the marker is the single
        ingest-completeness signal now. A missing ``cache_meta`` table
        composes as ``False`` (not a hard defer).
      * ``cache_has_entries`` — ``session_entries`` is non-empty, read
        via an inline ``SELECT 1 FROM session_entries LIMIT 1``
        (deliberately NOT ``_probe_table_nonempty``, which propagates
        transient errors by design — the shell must CATCH a transient
        BUSY/LOCKED here and flip ``marker_state_readable=False`` so the
        resolver DEFERs at row 1; the helper cannot do that). Together
        with ``walk_complete`` this closes the round-3 partial-walk
        false-pass and the P1 empty-cache rebuild-over-pruned-disk case
        (spec D3): row 6 requires BOTH.
      * ``caller_has_historical_rows`` — caller-supplied ``data_present``;
        each migration passes its OWN scoped row set (008
        ``bool(snapshot_rows)``, etc.) so a no-op upgrade isn't wedged.
      * ``disk_state`` — ``"absent"`` (no projects dirs resolve),
        ``"jsonl_present"`` (≥1 ``*.jsonl`` under any root), or
        ``"pruned"`` (dirs resolve but hold no JSONL). REASON-only — it
        never changes the decision, only the row-7 operator guidance text.
      * ``marker_state_readable`` — ``False`` only when the
        ``schema_migrations`` read is missing-table (cache.db never ran
        the dispatcher) OR any of the reads is transiently
        ``BUSY``/``LOCKED``/``CANTOPEN`` (per-read split, spec P2#1). The
        resolver maps this to row 1 DEFER (retry next open).

    Parameters
    ----------
    cache_ro
        Read-only sqlite3 connection to ``cache.db``. Cross-DB migrations
        open the sibling DB read-only inside their handler body via
        ``sqlite3.connect(f"file:{path}?mode=ro", uri=True)``. Exposed as
        an explicit parameter so tests can inject a tmp-path connection
        without touching ``HOME``.
    claude_projects_dirs
        Either a single ``pathlib.Path`` (legacy single-rooted form) or a
        ``list[pathlib.Path]`` of projects/ directories. The disk-state
        classification ORs across every root. Production callers resolve
        this via ``_resolve_projects_dirs_for_gate`` (env-aware); an empty
        list is the legitimate ``disk_state="absent"`` topology and is
        handled by the resolver (no per-migration default-dir fallback).
    data_present
        Keyword-only (defaults ``False`` for the 2-arg test callers).
        Whether the caller still holds historical rows it is about to
        recompute from ``session_entries``.

    Spec: docs/superpowers/specs/2026-05-23-migration-gate-state-machine-design.md D1/D3.
    """
    # Normalize to list so the disk-state classification can OR across
    # roots. Accepting a bare Path keeps the legacy test signature working.
    if isinstance(claude_projects_dirs, pathlib.Path):
        projects_dirs = [claude_projects_dirs]
    else:
        projects_dirs = list(claude_projects_dirs)

    marker_state_readable = True

    # --- cache 001 state (schema_migrations / schema_migrations_skipped) ---
    # "applied" wins; else "skipped"; else "pending". A missing
    # ``schema_migrations`` table (cache.db never ran the dispatcher) or a
    # transient BUSY/LOCKED on the read flips ``marker_state_readable`` so
    # the resolver defers at row 1 instead of guessing.
    cache_001_state = "pending"
    try:
        if cache_ro.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?",
            ("001_dedup_highest_wins",),
        ).fetchone() is not None:
            cache_001_state = "applied"
        else:
            try:
                if cache_ro.execute(
                    "SELECT 1 FROM schema_migrations_skipped WHERE name=?",
                    ("001_dedup_highest_wins",),
                ).fetchone() is not None:
                    cache_001_state = "skipped"
            except sqlite3.OperationalError as exc:
                if _is_transient_sqlite_error(exc):
                    marker_state_readable = False
                elif not _is_no_such_table_error(exc):
                    raise
                # no_such_table on _skipped -> treat as "not skipped" (pending)
    except sqlite3.OperationalError as exc:
        if _is_transient_sqlite_error(exc) or _is_no_such_table_error(exc):
            marker_state_readable = False
        else:
            raise

    # --- walk_complete (cache_meta marker presence) ---
    # The single ingest-completeness signal (spec D5). ``sync_cache`` writes
    # it only after a clean full walk begun with 001 applied; cache 001 /
    # rebuild / truncation clear it atomically. Missing table -> walk✗.
    walk_complete = False
    if marker_state_readable:
        try:
            walk_complete = cache_ro.execute(
                "SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'"
            ).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if _is_transient_sqlite_error(exc):
                marker_state_readable = False
            elif not _is_no_such_table_error(exc):
                raise

    # --- cache_has_entries (session_entries non-empty) ---
    cache_has_entries = False
    if marker_state_readable:
        try:
            cache_has_entries = cache_ro.execute(
                "SELECT 1 FROM session_entries LIMIT 1"
            ).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if _is_transient_sqlite_error(exc):
                marker_state_readable = False
            elif not _is_no_such_table_error(exc):
                raise

    # --- disk_state (REASON-only; never changes the decision) ---
    if not projects_dirs:
        disk_state = "absent"
    elif any(any(p.glob("**/*.jsonl")) for p in projects_dirs):
        disk_state = "jsonl_present"
    else:
        disk_state = "pruned"

    resolution = resolve_upgrade_gate(UpgradeGateInputs(
        cache_001_state=cache_001_state,
        walk_complete_since_001=walk_complete,
        cache_has_entries=cache_has_entries,
        caller_has_historical_rows=bool(data_present),
        disk_state=disk_state,
        marker_state_readable=marker_state_readable,
    ))
    if resolution.action is GateAction.DEFER:
        raise MigrationGateNotMet(resolution.reason)


def _is_no_such_table_error(exc: sqlite3.OperationalError) -> bool:
    """Return True iff ``exc`` is SQLite's "no such table" error.

    Two-signal predicate to defend against future SQLite version drift
    in the error-message format:

      * Substring match on the lowercased message (stable for ~20 years).
      * ``exc.sqlite_errorcode == SQLITE_ERROR (1)`` (Python 3.11+;
        cctally's floor is 3.11 per ``__min_python_version__``). The
        ``getattr(..., None) in (None, 1)`` form degrades gracefully if
        the attribute is ever missing — substring-only on legacy Python.

    Centralized so the gate shell's cache-state reads and the migration
    table-existence checks share the same "table missing" predicate.
    """
    return (
        "no such table" in str(exc).lower()
        and getattr(exc, "sqlite_errorcode", None) in (None, 1)
    )


def _probe_table_nonempty(conn: sqlite3.Connection, table: str) -> bool:
    """True iff ``table`` exists and has at least one row. Missing table -> False.

    Single source for the 'is there data here?' probe shared by the dispatcher
    fresh-install fast-path and the gate shell's cache_has_entries input
    (cctally-dev#93). Transient BUSY/LOCKED propagates to the caller.
    """
    try:
        return conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError as exc:
        if _is_no_such_table_error(exc):
            return False
        raise


def _is_transient_sqlite_error(exc: sqlite3.OperationalError) -> bool:
    """Return True iff ``exc`` is a transient SQLite condition the gate
    can legitimately defer on.

    Covers:

      * ``SQLITE_BUSY``    (errorcode 5)  — another writer holds the DB.
      * ``SQLITE_LOCKED``  (errorcode 6)  — a table within the DB is locked.
      * ``SQLITE_CANTOPEN``(errorcode 14) — the DB file doesn't exist /
        can't be opened (e.g. unlinked mid-flight between an ``exists()``
        probe and ``sqlite3.connect``, or never created yet).

    Gate-defer semantics (G4 + G5): a transient error means the gate
    state is genuinely unknown at this instant, NOT that the migration
    has failed. The dispatcher should translate to ``MigrationGateNotMet``
    rather than logging to ``migration-errors.log`` (which would render
    a misleading error banner for a self-healing condition).

    Belt-and-suspenders predicate: matches on ``sqlite_errorcode`` first
    (stable Python 3.11+ API), with a substring fallback for the rare
    case where the attribute is missing (legacy Python builds; the
    ``getattr(..., None) in (...)`` form degrades to substring-only).
    """
    code = getattr(exc, "sqlite_errorcode", None)
    if code in (5, 6, 14):
        return True
    if code is None:
        msg = str(exc).lower()
        # Stable SQLite error-message fragments for the three transient
        # codes; substring-only fallback when sqlite_errorcode is absent.
        if (
            "database is locked" in msg
            or "database table is locked" in msg
            or "unable to open database" in msg
        ):
            return True
    return False


# === Region 7b2: Eager cache-migration trigger (V4 — same-invocation 008 apply) ===


def _apply_cache_schema(conn: sqlite3.Connection) -> None:
    """Single source of cache.db's schema (cctally-dev#93, spec D4).

    ``_cctally()``-free so both ``open_cache_db`` (in _cctally_cache.py, which
    already imports _cctally_db) and ``_eagerly_apply_cache_migrations`` (here)
    can call it without an import cycle. Idempotent (CREATE ... IF NOT EXISTS +
    ``add_column_if_missing``). Does NOT run the dispatcher and does NOT include
    the Codex ``last_total_tokens`` ALTER, which carries a one-time purge
    side-effect that stays in ``open_cache_db``. The eager dispatcher can run
    024, but that handler acquires its own Codex flock and only deletes
    Codex-derived rows; it neither reads nor requires ``last_total_tokens``.
    Normal ``open_cache_db`` remains responsible for the ALTER and its historic
    purge, so leaving it outside this shared schema is safe.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_files (
            path             TEXT PRIMARY KEY,
            size_bytes       INTEGER NOT NULL,
            mtime_ns         INTEGER NOT NULL,
            last_byte_offset INTEGER NOT NULL,
            last_ingested_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS session_entries (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path         TEXT    NOT NULL,
            line_offset         INTEGER NOT NULL,
            timestamp_utc       TEXT    NOT NULL,
            model               TEXT    NOT NULL,
            msg_id              TEXT,
            req_id              TEXT,
            input_tokens        INTEGER NOT NULL DEFAULT 0,
            output_tokens       INTEGER NOT NULL DEFAULT 0,
            cache_create_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
            usage_extra_json    TEXT,
            cost_usd_raw        REAL,
            speed               TEXT,
            mutation_seq        INTEGER NOT NULL DEFAULT 0,
            mutation_min_ts     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_entries_timestamp
            ON session_entries(timestamp_utc);
        CREATE INDEX IF NOT EXISTS idx_entries_source
            ON session_entries(source_path);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_dedup
            ON session_entries(msg_id, req_id)
            WHERE msg_id IS NOT NULL AND req_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS conversation_messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT,
            uuid          TEXT,
            parent_uuid   TEXT,
            source_path   TEXT    NOT NULL,
            byte_offset   INTEGER NOT NULL,
            timestamp_utc TEXT,
            entry_type    TEXT    NOT NULL,
            text          TEXT    NOT NULL DEFAULT '',
            blocks_json   TEXT    NOT NULL DEFAULT '[]',
            model         TEXT,
            msg_id        TEXT,
            req_id        TEXT,
            cwd           TEXT,
            git_branch    TEXT,
            is_sidechain  INTEGER NOT NULL DEFAULT 0,
            source_tool_use_id TEXT,
            UNIQUE(source_path, byte_offset)
        );
        CREATE INDEX IF NOT EXISTS idx_conv_session_ts
            ON conversation_messages(session_id, timestamp_utc, id);
        CREATE INDEX IF NOT EXISTS idx_conv_session_uuid
            ON conversation_messages(session_id, uuid);
        CREATE INDEX IF NOT EXISTS idx_conv_source
            ON conversation_messages(source_path);
        CREATE INDEX IF NOT EXISTS idx_conv_turnkey
            ON conversation_messages(msg_id, req_id);
        -- #289: partial covering index on cwd. build_anon_plan_for_db sources
        -- anonymization tokens with `SELECT DISTINCT cwd FROM conversation_messages
        -- WHERE cwd IS NOT NULL AND cwd != ''`; without this index that DISTINCT is
        -- a full scan of the whole (prose-heavy) table (~20s cold on a 5.5 GB
        -- cache). The partial `(cwd)` index matches the WHERE exactly so the
        -- DISTINCT is answered by an ordered index-only walk. MIRRORS migration 021
        -- (base schema here for fresh/rebuilt caches, migration for existing ones).
        CREATE INDEX IF NOT EXISTS idx_conversation_messages_cwd
            ON conversation_messages(cwd)
            WHERE cwd IS NOT NULL AND cwd != '';

        -- #301: partial covering index on (model, session_id). list_conversation_facets
        -- and _model_clause source model facets/filters with `SELECT DISTINCT ... model
        -- FROM conversation_messages WHERE model IS NOT NULL AND model != ''`; without
        -- this index those DISTINCTs do an index->heap walk of the whole table (~22s
        -- cold on a 5.5 GB cache). The partial (model, session_id) index answers all
        -- three model queries index-only: the facets DISTINCT and distinct-model
        -- enumeration as ordered index walks, and the ?models= filter as a model seek.
        -- MIRRORS migration 022 (base schema here for fresh/rebuilt caches, migration
        -- for existing ones) — same discipline as idx_conversation_messages_cwd (#289).
        CREATE INDEX IF NOT EXISTS idx_conversation_messages_model_session
            ON conversation_messages(model, session_id)
            WHERE model IS NOT NULL AND model != '';

        -- #193: per-session AI-generated title, isolated from the six places
        -- that iterate conversation_messages. The explicit NOT NULL on the
        -- non-INTEGER PRIMARY KEY matters (SQLite's legacy NULL-in-PK bug);
        -- _session_titles_map can only key on a concrete session_id.
        CREATE TABLE IF NOT EXISTS conversation_ai_titles (
            session_id  TEXT NOT NULL PRIMARY KEY,
            ai_title    TEXT NOT NULL,
            source_path TEXT,
            byte_offset INTEGER NOT NULL
        );

        -- Browse-rail rollup (conversation_sessions). Materializes the four
        -- structural aggregates the rail's old live GROUP BY produced
        -- (COUNT/MIN/MAX over conversation_messages per session_id) so
        -- GET /api/conversations no longer scans the whole message table to
        -- render a 50-row page, PLUS three filter columns
        -- (project_label/cost_usd/cache_rebuild_count, migration 015) so the
        -- Browse list's date/project/cost/cache-rebuild filters are pure-SQL
        -- predicates, PLUS three DISPLAYED-enrichment columns
        -- (git_branch/models_json/title, migration 023 / #302) so the rail's
        -- per-session enrichment (git branch, ordered model list, stable
        -- first-prompt title) is read straight off the rollup instead of
        -- re-scanning conversation_messages per session on every cold page.
        -- The structural columns are recomputed by a COUNT/MIN/MAX GROUP BY; the
        -- filter + enrichment columns are filled per-session by
        -- _fill_conversation_sessions_filter_columns in the same flock-held
        -- recompute (cost/models/title/branch via the query kernel's batch maps,
        -- cache_rebuild_count via a per-session assemble). The AI title is NOT
        -- stored here — it is volatile, so list_conversations overlays it live
        -- from conversation_ai_titles (#302 Q2-B). The explicit NOT NULL on the
        -- non-INTEGER PK matters (SQLite's legacy NULL-in-PK bug); the rail keys
        -- on a concrete session_id and the recompute's GROUP BY already filters
        -- nulls. The index lets the only paginated ordering (recent)
        -- early-terminate at LIMIT with no temp B-tree. Re-derivable like the
        -- rest of cache.db; sync_cache keeps it honest (scoped DELETE+INSERT
        -- re-derive + flag-gated full recompute) — migration 013 arms the
        -- one-time history backfill, 023 re-arms it for the enrichment columns.
        CREATE TABLE IF NOT EXISTS conversation_sessions (
            session_id          TEXT NOT NULL PRIMARY KEY,
            msg_count           INTEGER NOT NULL DEFAULT 0,
            started_utc         TEXT,
            last_activity_utc   TEXT,
            project_label       TEXT,
            cost_usd            REAL NOT NULL DEFAULT 0,
            cache_rebuild_count INTEGER NOT NULL DEFAULT 0,
            git_branch          TEXT,
            models_json         TEXT,
            title               TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_conv_sessions_recent
            ON conversation_sessions(last_activity_utc DESC, session_id DESC);

        -- #217 S2 / I-3: file-path search axis. One row per WRITE-class file
        -- touch (Edit/MultiEdit/Write/NotebookEdit) inside a conversation message,
        -- derived from blocks_json by _derive_file_touches. message_id =
        -- conversation_messages.id (the turn anchor). Derived/re-derivable state:
        -- _fill_file_touches accumulates via INSERT OR IGNORE, the destructive
        -- message paths delete it (clear_conversation_messages / per-source
        -- reingest), and migration 019 arms a one-time history backfill.
        --
        -- CRITICAL: this is a PLAIN table with NO dependency on the FTS shape, and
        -- it is created HERE — inside the unconditional executescript, BEFORE the
        -- FTS5 ``legacy_present`` early-return below — so it ALWAYS exists
        -- regardless of FTS topology. (The I-2 title-FTS bug created its vtable
        -- AFTER that early-return, so its consumer crashed on a legacy-shape +
        -- both-pending upgrade; the file-touches table must not repeat that class.)
        CREATE TABLE IF NOT EXISTS conversation_file_touches (
            message_id  INTEGER NOT NULL,
            session_id  TEXT NOT NULL,
            uuid        TEXT,
            file_path   TEXT NOT NULL,
            tool        TEXT NOT NULL,
            UNIQUE(message_id, file_path, tool)
        );
        -- COLLATE NOCASE keeps the index's case-folding identical to the default
        -- (case-insensitive) ``LIKE`` (#217 S2 / I-3 review Important #1): NOCASE folds
        -- A-Z/a-z the same way the LIKE built-in does, so matching semantics are
        -- byte-identical to a BINARY index. (#223: ``_search_files`` now substring-
        -- matches EVERY query (``file_path LIKE '%q%'``) — a deliberate scan over the
        -- modest touch table — so this index no longer drives a prefix probe; it is
        -- retained as-is with no schema change. NOCASE was originally load-bearing so a
        -- ``LIKE 'prefix%'`` could ride the btree, which SQLite only does when the index
        -- column folds the same way.)
        CREATE INDEX IF NOT EXISTS idx_file_touches_path
            ON conversation_file_touches(file_path COLLATE NOCASE);

        CREATE TABLE IF NOT EXISTS codex_session_files (
            path             TEXT PRIMARY KEY,
            size_bytes       INTEGER NOT NULL,
            mtime_ns         INTEGER NOT NULL,
            last_byte_offset INTEGER NOT NULL,
            last_ingested_at TEXT NOT NULL,
            last_session_id  TEXT,
            last_model       TEXT
        );
        CREATE TABLE IF NOT EXISTS codex_session_entries (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path              TEXT    NOT NULL,
            line_offset              INTEGER NOT NULL,
            timestamp_utc            TEXT    NOT NULL,
            session_id               TEXT    NOT NULL,
            model                    TEXT    NOT NULL,
            input_tokens             INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens      INTEGER NOT NULL DEFAULT 0,
            output_tokens            INTEGER NOT NULL DEFAULT 0,
            reasoning_output_tokens  INTEGER NOT NULL DEFAULT 0,
            total_tokens             INTEGER NOT NULL DEFAULT 0,
            UNIQUE(source_path, line_offset)
        );
        CREATE INDEX IF NOT EXISTS idx_codex_entries_timestamp
            ON codex_session_entries(timestamp_utc);
        CREATE INDEX IF NOT EXISTS idx_codex_entries_session
            ON codex_session_entries(session_id);
        CREATE INDEX IF NOT EXISTS idx_codex_entries_source
            ON codex_session_entries(source_path);

        -- #294 S1: physical Codex rollout retention. These tables deliberately
        -- live in the unconditional base-schema script, before the legacy-FTS
        -- topology checks below: an existing cache with an old FTS shape must
        -- still gain the S1 tables on every open, just as conversation_file_touches
        -- does above.
        CREATE TABLE IF NOT EXISTS codex_source_roots (
            source_root_key      TEXT NOT NULL PRIMARY KEY,
            canonical_root_path  TEXT NOT NULL UNIQUE,
            first_seen_utc       TEXT NOT NULL,
            last_seen_utc        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS codex_conversation_threads (
            conversation_key     TEXT NOT NULL PRIMARY KEY,
            source_root_key      TEXT NOT NULL,
            native_thread_id     TEXT NOT NULL,
            root_thread_id       TEXT NOT NULL,
            parent_thread_id     TEXT,
            source_path          TEXT NOT NULL,
            cwd                  TEXT,
            git_json             TEXT,
            source_kind          TEXT,
            thread_source_json   TEXT,
            model_provider       TEXT,
            context_window       INTEGER,
            first_seen_utc       TEXT,
            last_seen_utc        TEXT,
            UNIQUE(source_root_key, root_thread_id, native_thread_id)
        );
        CREATE INDEX IF NOT EXISTS idx_codex_threads_source_root
            ON codex_conversation_threads(source_root_key);
        CREATE INDEX IF NOT EXISTS idx_codex_threads_source_path
            ON codex_conversation_threads(source_path);

        CREATE TABLE IF NOT EXISTS quota_window_snapshots (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            source                TEXT NOT NULL CHECK(source IN ('claude','codex')),
            source_root_key       TEXT,
            source_path           TEXT NOT NULL,
            line_offset           INTEGER NOT NULL,
            captured_at_utc       TEXT NOT NULL,
            observed_slot         TEXT,
            logical_limit_key     TEXT NOT NULL,
            limit_id              TEXT,
            limit_name            TEXT,
            window_minutes        INTEGER NOT NULL CHECK(window_minutes > 0),
            used_percent          REAL NOT NULL CHECK(used_percent >= 0 AND used_percent <= 100),
            resets_at_utc         TEXT NOT NULL,
            plan_type             TEXT,
            individual_limit_json TEXT,
            reached_type          TEXT,
            observed_model        TEXT,
            UNIQUE(source, source_path, line_offset, logical_limit_key),
            CHECK(source != 'codex' OR source_root_key IS NOT NULL)
        );
        CREATE INDEX IF NOT EXISTS idx_quota_window_source_root
            ON quota_window_snapshots(source_root_key);
        CREATE INDEX IF NOT EXISTS idx_quota_window_captured_at
            ON quota_window_snapshots(captured_at_utc);

        CREATE TABLE IF NOT EXISTS codex_conversation_events (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path          TEXT NOT NULL,
            line_offset          INTEGER NOT NULL,
            source_root_key      TEXT NOT NULL,
            conversation_key     TEXT,
            native_thread_id     TEXT,
            root_thread_id       TEXT,
            parent_thread_id     TEXT,
            timestamp_utc        TEXT,
            record_type          TEXT,
            event_type           TEXT,
            turn_id              TEXT,
            call_id              TEXT,
            payload_json         TEXT NOT NULL,
            UNIQUE(source_path, line_offset)
        );
        CREATE INDEX IF NOT EXISTS idx_codex_events_conversation
            ON codex_conversation_events(conversation_key);
        CREATE INDEX IF NOT EXISTS idx_codex_events_timestamp
            ON codex_conversation_events(timestamp_utc);

        -- #294 S6: normalized Codex conversation storage. Like the S1 tables
        -- above, these live in the UNCONDITIONAL base-schema script — before the
        -- legacy-FTS topology checks below — so an existing cache gains them on
        -- every open. The independent Codex FTS layer
        -- (_apply_codex_conversation_fts) stands up codex_conversation_fts + its
        -- own triggers separately, BEFORE the Claude legacy-FTS early-return, so
        -- a legacy-shape Claude cache still gets the Codex search index.
        --
        -- codex_conversation_messages: one normalized row per NORMALIZED physical
        -- event (§3.1). ``id`` is a PLAIN rowid alias (deliberately NOT
        -- AUTOINCREMENT): migration 025 replays inserts in deterministic
        -- (source_path ASC, line_offset ASC) order, so a repeated run is
        -- byte-idempotent (no sqlite_sequence drift), while fresh ingest inserts
        -- in discovery order — fresh-vs-migrated equality is asserted
        -- SEMANTICALLY (row content modulo id).
        CREATE TABLE IF NOT EXISTS codex_conversation_messages (
            id               INTEGER PRIMARY KEY,
            conversation_key TEXT NOT NULL,
            source_root_key  TEXT NOT NULL,
            source_path      TEXT NOT NULL,
            line_offset      INTEGER NOT NULL,
            timestamp_utc    TEXT,
            turn_id          TEXT,
            call_id          TEXT,
            kind             TEXT NOT NULL,
            event_type       TEXT,
            record_family    TEXT NOT NULL,
            model            TEXT,
            text             TEXT,
            content_digest   TEXT NOT NULL,
            content_len      INTEGER NOT NULL CHECK(content_len >= 0),
            detail_json      TEXT,
            search_tool      TEXT,
            search_thinking  TEXT,
            UNIQUE(source_path, line_offset)
        );
        CREATE INDEX IF NOT EXISTS idx_codex_conv_msgs_conversation
            ON codex_conversation_messages(conversation_key, timestamp_utc, id);
        CREATE INDEX IF NOT EXISTS idx_codex_conv_msgs_source
            ON codex_conversation_messages(source_path);

        -- codex_conversation_rollups: browse-rail materialization (§3.2). A pure
        -- function of surviving codex_conversation_messages (+ thread metadata),
        -- recomputed-affected-or-deleted after every write/delete path. item_count
        -- counts rendered LOGICAL items (mirror-paired), not physical rows.
        CREATE TABLE IF NOT EXISTS codex_conversation_rollups (
            conversation_key  TEXT NOT NULL PRIMARY KEY,
            source_root_key   TEXT NOT NULL,
            parent_thread_id  TEXT,
            item_count        INTEGER NOT NULL DEFAULT 0,
            started_utc       TEXT,
            last_activity_utc TEXT,
            project_key       TEXT,
            project_label     TEXT,
            models_json       TEXT,
            title             TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_codex_conv_rollups_recent
            ON codex_conversation_rollups(last_activity_utc DESC, conversation_key DESC);

        -- codex_conversation_file_touches: write-class axis for the `files`
        -- search kind + outline file stats (§3.3). source_path gives explicit
        -- lineage so the per-file delete/truncate/prune paths scope deletions
        -- exactly as they do for the other Codex families.
        CREATE TABLE IF NOT EXISTS codex_conversation_file_touches (
            message_id       INTEGER NOT NULL,
            conversation_key TEXT NOT NULL,
            source_path      TEXT NOT NULL,
            file_path        TEXT NOT NULL,
            tool             TEXT NOT NULL,
            UNIQUE(message_id, file_path, tool)
        );
        CREATE INDEX IF NOT EXISTS idx_codex_conv_touches_source
            ON codex_conversation_file_touches(source_path);

        CREATE TABLE IF NOT EXISTS cache_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # Inline migration: add session_id / project_path columns to session_files
    # if they're missing. These were added for A2 `session` subcommand metadata;
    # populated lazily in sync_cache() / _ensure_session_files_row().
    add_column_if_missing(conn, "session_files", "session_id", "TEXT")
    add_column_if_missing(conn, "session_files", "project_path", "TEXT")
    # #294 S1: old Codex accounting rows have no truthful provider-root or
    # conversation identity, so the linkage remains nullable until migration
    # 024 clears them for a source-derived reingest. Existing report selectors
    # intentionally keep selecting their shipped columns only.
    add_column_if_missing(conn, "codex_session_entries", "source_root_key", "TEXT")
    add_column_if_missing(conn, "codex_session_entries", "conversation_key", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_codex_entries_source_root "
        "ON codex_session_entries(source_root_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_codex_entries_conversation "
        "ON codex_session_entries(conversation_key)"
    )
    # #294 S3: the qualified accounting adapter is bounded by timestamp and
    # joins only through the S1 root-qualified conversation identity.  Keep
    # this re-derivable index in the unconditional schema path so an existing
    # cache gains the same scale-safe plan without a data migration.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_codex_entries_ts_root_conversation "
        "ON codex_session_entries(timestamp_utc, source_root_key, conversation_key)"
    )
    # The per-file terminal thread facts seed a later append without rereading
    # the prefix. They are nullable for old cache rows; migration 024 never
    # fabricates these source facts and instead clears/rederives them.
    add_column_if_missing(conn, "codex_session_files", "source_root_key", "TEXT")
    add_column_if_missing(conn, "codex_session_files", "last_native_thread_id", "TEXT")
    add_column_if_missing(conn, "codex_session_files", "last_root_thread_id", "TEXT")
    add_column_if_missing(conn, "codex_session_files", "last_parent_thread_id", "TEXT")
    add_column_if_missing(conn, "codex_session_files", "last_conversation_key", "TEXT")
    # #294 S6: the terminal sticky-turn seed for delta resumes, alongside the
    # existing last_model/thread facts. A batch that ends after a turn_context
    # and resumes with a response_item stamps the correct effective turn.
    add_column_if_missing(conn, "codex_session_files", "last_turn_id", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_codex_files_source_root "
        "ON codex_session_files(source_root_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_codex_files_conversation "
        "ON codex_session_files(last_conversation_key)"
    )
    # #181: materialize the only-ever-consumed extra `usage` key (`speed`) into
    # a real session_entries column so the hot cache read paths (iter_entries /
    # get_claude_session_entries) stop json.loads-ing the deeply-nested
    # usage_extra_json blob per row — that per-tick parse was pegging a core in
    # the dashboard. Idempotent column-add (no marker, no version), appended
    # after cost_usd_raw to match the CREATE TABLE order; cache migration 008
    # then backfills it from the legacy blob on existing rows.
    add_column_if_missing(conn, "session_entries", "speed", "TEXT")
    # #270: the durable per-row mutation signal. ``mutation_seq`` is a change
    # stamp bumped on every insert + every WHERE-passing in-place UPSERT (from a
    # cache_meta counter, ingest side); ``mutation_min_ts`` records the EARLIEST
    # event time the row has ever held, so a finalization that overwrites
    # ``timestamp_utc`` and moves the row across a bucket boundary still lets the
    # closed-bucket watermark reach the OLD bucket. Idempotent column-adds (no
    # marker, no version — a pure column+index add, not a framework migration);
    # ``INTEGER NOT NULL DEFAULT 0`` is a metadata-only add in SQLite (no table
    # rewrite on a large session_entries). The covering index
    # ``idx_entries_mutation_seq`` backs the one new query shape
    # ``MIN(mutation_min_ts) WHERE mutation_seq > ?`` (index-only range-min over
    # the delta) plus the ``WHERE mutation_seq > ?`` filters. Both the column
    # adds AND the index MUST run BEFORE the legacy FTS early-return below (the
    # ``_apply_cache_schema_legacy_early_return_before_new_table`` gotcha), so an
    # old cache.db that early-returns still receives them. The index create is
    # kept OUT of the top executescript on purpose: on an existing DB the CREATE
    # TABLE there is a no-op, so the columns do not exist yet and an index over
    # them would raise — it must follow the add_column_if_missing calls.
    add_column_if_missing(
        conn, "session_entries", "mutation_seq", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(conn, "session_entries", "mutation_min_ts", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entries_mutation_seq "
        "ON session_entries(mutation_seq, mutation_min_ts)")
    # #279 S3 F3: DB-level idempotency backstop mirroring
    # codex_session_entries' UNIQUE(source_path, line_offset). Guarded and
    # OUTSIDE the top executescript: this function runs on EVERY open BEFORE the
    # migration dispatcher, so a legacy cache.db holding historical
    # physical-key duplicates must tolerate the index being ABSENT until cache
    # migration 020 dedups it — an unguarded CREATE UNIQUE INDEX here would
    # brick every open of such a DB before 020 could ever run. Fresh and clean
    # DBs get the index immediately; a dirty legacy one gets it from 020 (or the
    # first open after 020 dedups the duplicates). Kept after the
    # add_column_if_missing calls (like idx_entries_mutation_seq) so the columns
    # exist, and BEFORE the legacy-FTS early-return so an old-shape DB still
    # receives it.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_physical "
            "ON session_entries(source_path, line_offset)")
    except sqlite3.IntegrityError:
        pass
    # Existing-DB guard for the skill-content fold link (cctally-dev
    # skill-content-nesting): the message-level sourceToolUseID. Idempotent
    # column-add (no marker, no version); cache migration 006 then re-ingests
    # so the value actually lands on historical rows.
    add_column_if_missing(conn, "conversation_messages", "source_tool_use_id", "TEXT")
    # #177 Session 1: enriched data-contract columns. Idempotent column-adds (no
    # marker, no version — exactly like source_tool_use_id); cache migration 007
    # then re-ingests so the values actually land on historical rows.
    add_column_if_missing(conn, "conversation_messages", "stop_reason", "TEXT")
    add_column_if_missing(conn, "conversation_messages", "attribution_skill", "TEXT")
    add_column_if_missing(conn, "conversation_messages", "attribution_plugin", "TEXT")
    # #217 S1 / U7a: ``search_aux`` (the pre-#177-S6 non-prose FTS blob, always
    # '' since the split) is NO LONGER emitted here — a fresh install never
    # carries it, and cache migration ``016_drop_search_aux`` drops it from an
    # existing install once the migration-010 search split is consumed. The
    # legacy ``conversation_fts_aux`` test/fixture standup
    # (``_create_conversation_fts_aux_table``) adds the column locally itself.
    # #177 S6: the split non-prose search index — two columns so kind facets
    # (Tools / Thinking) are exact in SQL. The consolidated multi-column
    # conversation_fts(text, search_tool, search_thinking) indexes these.
    # Idempotent column-adds (no marker, no version); migration 010 backfills the
    # values onto existing history from blocks_json under the cache.db.lock flock.
    add_column_if_missing(
        conn, "conversation_messages", "search_tool", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(
        conn, "conversation_messages", "search_thinking", "TEXT NOT NULL DEFAULT ''")
    # #302: the browse-rail DISPLAYED-enrichment columns on the conversation_sessions
    # rollup (git_branch / models_json / title). Idempotent column-adds (no marker,
    # no version — the repo's column-addition rule, NOT a raw ALTER in a migration
    # handler; Codex P1-2). They MUST land here — after the CREATE TABLE above (so
    # the table exists on a fresh DB) and BEFORE the legacy-FTS early-return below
    # (so an old-shape existing cache.db still receives them before any rail read
    # SELECTs them). Migration 023 arms the one-time full backfill that fills the
    # values on existing history; a fresh DB fills them at ingest.
    add_column_if_missing(conn, "conversation_sessions", "git_branch", "TEXT")
    add_column_if_missing(conn, "conversation_sessions", "models_json", "TEXT")
    add_column_if_missing(conn, "conversation_sessions", "title", "TEXT")
    # #320: quota pool identity cannot depend on transcript events after the
    # store split. Stamp the active model directly on each compact physical
    # quota observation. Existing caches receive the nullable column here; 028
    # backfills it from the still-present legacy event corpus before dropping
    # that corpus.
    add_column_if_missing(conn, "quota_window_snapshots", "observed_model", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_files_session_id "
        "ON session_files(session_id)"
    )
    # #294 S6: the independent Codex conversation FTS lifecycle. Run BEFORE (and
    # independent of) the Claude FTS branch below — the Claude branch early-returns
    # on a legacy conversation_fts(text) shape, so the Codex index must be stood up
    # here or a legacy-shape cache would never gain Codex search. Never touches any
    # Claude FTS object (conv_fts_* / conv_title_fts_*).
    _apply_codex_conversation_fts(conn)
    # FTS5 is optional in the sqlite build. Create the external-content index +
    # sync triggers as separate executes wrapped in one try; on failure create
    # NEITHER the table NOR the triggers (a trigger referencing a missing table
    # would itself error), set a persisted flag, and let search fall back to
    # LIKE. Spec §1. Idempotent (IF NOT EXISTS).
    if _fts5_available(conn):
        # #177 S6: if a LEGACY conversation_fts(text) shape is already present
        # (a pre-S6 install whose migration-010 backfill/swap has not yet run),
        # leave EVERYTHING untouched — the legacy table, the legacy aux table,
        # and the legacy triggers. The sync-side swap (_swap_conversation_fts_to_
        # split, under the cache.db.lock flock) owns the transition; re-applying
        # the schema must NOT race it or partially mutate the shape. A fresh
        # install (no conversation_fts) and an already-split shape both fall
        # through to the create/recover path below.
        legacy_present = (
            conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='conversation_fts'"
            ).fetchone() is not None
            and not _conversation_fts_is_split(conn))
        if legacy_present:
            conn.commit()
            return
        try:
            # Recovery (spec §1/P2): if a PRIOR run marked FTS unavailable,
            # conversation_messages rows were ingested (by sync_cache / the
            # backfill) WITHOUT the AFTER INSERT trigger ever indexing them —
            # or a prior downgrade dropped the index while leaving the base
            # rows. Detect that BEFORE clearing the flag so we can rebuild the
            # external-content index from conversation_messages below. A fresh
            # install never sets the flag, so this stays False and no rebuild
            # runs (the triggers index rows incrementally as they arrive). This
            # is ALSO the FTS5-unavailable migration-010 recovery seam: the
            # backfill ran the base-column UPDATEs but skipped the vtable DDL, so
            # the first FTS-capable open lands the split shape + rebuilds here.
            recovering = conn.execute(
                "SELECT 1 FROM cache_meta WHERE key='fts5_unavailable'"
            ).fetchone() is not None
            # #177 S6: the single consolidated multi-column external-content
            # table replaces the old prose + aux pair. Column names match the
            # content table BY NAME (external-content rule).
            conn.execute(_CONV_FTS_SPLIT_DDL)
            # Trigger DDL lives in ONE place (_CONV_FTS_TRIGGER_DDL) so this
            # initial create and the #138 storm-free full-clear
            # (clear_conversation_messages, which drops + recreates the trigger
            # set) can never drift.
            _create_conversation_fts_triggers(conn)
            # #217 S2 / E7: the external-content title FTS rides the SAME
            # FTS5-available envelope (P1-6). It is independent of the message
            # FTS (external-content over conversation_ai_titles, not
            # conversation_messages), so a failed message-FTS create above never
            # reaches here; but a failed title-FTS create must drop both message
            # and title triggers and mark unavailable, so the shared except below
            # owns the cleanup. Idempotent (IF NOT EXISTS).
            conn.execute(_CONV_TITLE_FTS_DDL)
            _create_conversation_title_fts_triggers(conn)
            if recovering:
                # Repopulate the freshly-(re)created index from the base table
                # so pre-recovery history is searchable. Cheap no-op when
                # conversation_messages is empty.
                conn.execute(
                    "INSERT INTO conversation_fts(conversation_fts) VALUES('rebuild')")
                # The title FTS is external-content over conversation_ai_titles,
                # so a prior FTS5-unavailable run that ingested titles without the
                # AI trigger left the title index stale too — rebuild it the same
                # way (cheap no-op when conversation_ai_titles is empty).
                conn.execute(
                    "INSERT INTO conversation_title_fts(conversation_title_fts) "
                    "VALUES('rebuild')")
            conn.execute("DELETE FROM cache_meta WHERE key='fts5_unavailable'")
        except sqlite3.OperationalError:
            # partial create cleanup, then mark unavailable. _drop drops the
            # split trigger set (and any legacy aux trigger names, harmlessly)
            # plus the title trigger set, and we drop all three possible vtables,
            # so a failed create can't leave a live trigger over a missing table.
            _drop_conversation_fts_triggers(conn)
            _drop_conversation_title_fts_triggers(conn)
            try:
                conn.execute("DROP TABLE IF EXISTS conversation_fts")
                conn.execute("DROP TABLE IF EXISTS conversation_fts_aux")
                conn.execute("DROP TABLE IF EXISTS conversation_title_fts")
            except sqlite3.OperationalError:
                pass
            _set_cache_meta(conn, "fts5_unavailable", "1")
    else:
        # FTS5 is unavailable on THIS sqlite build. If a prior (FTS-capable)
        # run created the sync triggers, they now reference an unusable
        # conversation_fts and EVERY INSERT into conversation_messages would
        # raise "no such module: fts5". Because the conversation INSERT shares
        # sync_cache's per-file write transaction with session_entries, that
        # rollback would discard COST ingest too. Drop the orphan triggers so
        # writes succeed under the LIKE fallback. (The conversation_fts vtable
        # itself can't be DROPped without the fts5 module, but with no triggers
        # nothing writes to it.)
        _drop_conversation_fts_triggers(conn)
        # #217 S2 / E7 (P1-6): same hazard for the title FTS — a prior
        # FTS5-capable run may have left conv_title_fts_* triggers that now
        # reference an unusable conversation_title_fts. They fire inside the
        # SAME per-file ingest transaction (the conversation_ai_titles upsert),
        # so an orphan title trigger would roll back the cost ingest too. Drop
        # them under the LIKE fallback; kind=title degrades to a LIKE scan over
        # conversation_ai_titles.
        _drop_conversation_title_fts_triggers(conn)
        _set_cache_meta(conn, "fts5_unavailable", "1")
    # The FTS branch above issues DML (DELETE/INSERT on cache_meta) which opens
    # an implicit transaction under sqlite3's legacy autocommit mode. Close it
    # so the migration dispatcher's subsequent ``conn.execute("BEGIN")`` starts
    # cleanly (mirrors the bootstrap-rename commit envelope rationale).
    conn.commit()


def _apply_conversations_schema(conn: sqlite3.Connection) -> None:
    """Create the #320 transcript/search database schema.

    The historical cache schema remains the migration-fixture source of truth.
    Reuse it here, then remove the compact accounting families so conversation
    queries can resolve those names through the read-only ``cache_db``
    attachment. Empty compatibility tables may still exist in cache.db for old
    migration handlers, but all live transcript rows belong here.
    """
    # The first open projects the historical monolithic schema into the new
    # store. Avoid repeating that create-then-drop work on every conversation
    # endpoint: it writes sqlite_schema and WAL pages even when no transcript
    # data changed. Future conversation schema revisions must bump this marker.
    try:
        current = conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='conversation_schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        current = None
    if current is not None and current[0] == "1":
        return

    _apply_cache_schema(conn)
    conn.executescript(
        """
        DROP TABLE IF EXISTS session_entries;
        DROP TABLE IF EXISTS session_files;
        DROP TABLE IF EXISTS codex_session_entries;
        DROP TABLE IF EXISTS codex_session_files;
        DROP TABLE IF EXISTS quota_window_snapshots;
        DROP TABLE IF EXISTS codex_conversation_threads;
        DROP TABLE IF EXISTS codex_source_roots;

        CREATE TABLE IF NOT EXISTS conversation_source_files (
            path             TEXT PRIMARY KEY,
            size_bytes       INTEGER NOT NULL,
            mtime_ns         INTEGER NOT NULL,
            last_byte_offset INTEGER NOT NULL,
            last_ingested_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS codex_conversation_source_files (
            path                         TEXT PRIMARY KEY,
            size_bytes                   INTEGER NOT NULL,
            mtime_ns                     INTEGER NOT NULL,
            last_byte_offset             INTEGER NOT NULL,
            last_ingested_at             TEXT NOT NULL,
            source_root_key              TEXT,
            last_session_id              TEXT,
            last_model                   TEXT,
            last_total_tokens            INTEGER,
            last_native_thread_id        TEXT,
            last_root_thread_id          TEXT,
            last_parent_thread_id        TEXT,
            last_conversation_key        TEXT,
            last_turn_id                 TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO cache_meta(key,value) VALUES "
        "('conversation_schema_version','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """True if this sqlite build can create an FTS5 table. Cheap probe on a
    temp table that is created then dropped. Hidden test seam: tests monkeypatch
    this to False to exercise the LIKE fallback."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _set_cache_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO cache_meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


@conversations_migration("001_adopt_schema_version_marker")
def _conv_001_adopt_schema_version_marker(conn: sqlite3.Connection) -> None:
    """Bring conversations.db under the migration framework (spec §7.2).

    Adoption, not migration: ``_apply_conversations_schema`` (run before the
    dispatcher in ``open_conversations_db``) already owns the physical schema and
    the ``cache_meta['conversation_schema_version']`` marker. On an EXISTING
    populated DB the store's version gate keeps ``_apply_conversations_schema``
    from re-running the schema (its own marker short-circuit), and the
    dispatcher's data-emptiness probe (``conversation_messages`` /
    ``conversation_source_files``) classifies the DB as NON-fresh so this handler
    runs and the dispatcher stamps the marker + advances ``user_version`` to 1
    WITHOUT touching a single transcript row. On a FRESH DB the dispatcher's
    fresh-install path stamps the marker without invoking this handler at all.

    The handler is a self-contained, idempotent no-op-equivalent: it re-asserts
    the ``conversation_schema_version`` marker (an ``INSERT … ON CONFLICT`` upsert)
    so it is safe to re-run on its own output (handler-idempotency contract)."""
    _set_cache_meta(conn, "conversation_schema_version", "1")
    conn.commit()


# #177 S6: the consolidated multi-column external-content FTS5 table that
# replaces the old conversation_fts(text) + conversation_fts_aux(search_aux)
# pair. The three column names MUST match the conversation_messages columns BY
# NAME (external-content FTS5 resolves columns through the content table by
# name): a mismatch creates fine but breaks 'rebuild' + iterdump
# ("no such column"). Fresh installs create this directly; existing installs
# swap to it under the cache.db.lock flock via _swap_conversation_fts_to_split.
_CONV_FTS_SPLIT_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5("
    "text, search_tool, search_thinking, "
    "content='conversation_messages', content_rowid='id')"
)


def _conversation_fts_is_split(conn: sqlite3.Connection) -> bool:
    """True if conversation_fts is the #177 S6 multi-column shape (carries the
    ``search_tool`` column), False for the legacy single-column ``text`` shape or
    an absent table."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(conversation_fts)")]
    return "search_tool" in cols


def _create_conversation_fts_aux_table(conn: sqlite3.Connection) -> None:
    """Create the #177 aux external-content FTS5 index over the ``search_aux``
    blob. TEST/FIXTURE STANDUP ONLY — production code (``_apply_cache_schema``)
    builds the post-#177-S6 SPLIT index directly; this legacy single-aux-column
    shape is referenced solely by the migration-fixture builder + the swap tests
    (the migration's by-name DROP list still legitimately uses the trigger-name
    tuples, but never calls this helper). A standalone module-level seam (NOT
    inlined in ``_apply_cache_schema``)
    so the all-or-nothing regression test can monkeypatch it to raise
    ``OperationalError`` AFTER the prose ``conversation_fts`` create succeeded —
    proving the shared try-envelope drops BOTH indexes + BOTH trigger sets and a
    later ``conversation_messages`` INSERT still commits (the cost write txn is
    not rolled back). Must run inside that envelope; idempotent
    (``IF NOT EXISTS``)."""
    # The FTS column name MUST match the content table column (``search_aux``),
    # exactly as the prose ``conversation_fts`` uses ``text``. An external-content
    # FTS5 table resolves its columns through the content table by NAME, so a
    # mismatched column (e.g. ``aux`` against a ``search_aux`` content column)
    # leaves the index functional for MATCH but breaks any content-backed read of
    # the column — notably ``sqlite3.Connection.iterdump`` (the dump emits
    # ``SELECT quote(aux) FROM conversation_fts_aux`` → "no such column"). Aligning
    # the names keeps the index dumpable and is the documented FTS5 posture.
    #
    # #217 S1 / U7a: the live ``_apply_cache_schema`` no longer emits
    # ``search_aux`` (migration 016 drops it once the search split is consumed),
    # so this LEGACY standup adds the column LOCALLY here — it stands up the
    # pre-S6 aux shape and needs the content-table column to exist by NAME.
    # Idempotent (duplicate-column tolerated) so a DB that still carries the
    # column (pre-016 existing install) is a no-op.
    try:
        conn.execute(
            "ALTER TABLE conversation_messages "
            "ADD COLUMN search_aux TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already present (pre-016 existing install)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts_aux "
        "USING fts5(search_aux, content='conversation_messages', content_rowid='id')")


# Conversation FTS sync triggers (external-content FTS5). Defined ONCE here so
# the initial create in _apply_cache_schema and the #138 storm-free full-clear
# in clear_conversation_messages (which drops + recreates them) can never drift.
# conv_fts_ad / conv_fts_au use the external-content `'delete'` idiom.
# #177 S6: three-column trigger set for the consolidated conversation_fts. Same
# trigger names (conv_fts_ai/ad/au) as the legacy single-column set so the swap
# reuses them; the AU trigger now fires AFTER UPDATE OF text, search_tool,
# search_thinking (the backfill UPDATEs search_tool/search_thinking, which the
# OLD text-only AU did NOT fire — that invisibility is load-bearing, see
# _consume_search_split, but it applies only while the legacy triggers are still
# installed; once swapped, these fire on all three columns).
_CONV_FTS_TRIGGER_DDL = (
    "CREATE TRIGGER IF NOT EXISTS conv_fts_ai AFTER INSERT ON conversation_messages "
    "BEGIN INSERT INTO conversation_fts(rowid, text, search_tool, search_thinking) "
    "VALUES (new.id, new.text, new.search_tool, new.search_thinking); END",
    "CREATE TRIGGER IF NOT EXISTS conv_fts_ad AFTER DELETE ON conversation_messages "
    "BEGIN INSERT INTO conversation_fts(conversation_fts, rowid, text, search_tool, search_thinking) "
    "VALUES('delete', old.id, old.text, old.search_tool, old.search_thinking); END",
    "CREATE TRIGGER IF NOT EXISTS conv_fts_au "
    "AFTER UPDATE OF text, search_tool, search_thinking ON conversation_messages "
    "BEGIN INSERT INTO conversation_fts(conversation_fts, rowid, text, search_tool, search_thinking) "
    "VALUES('delete', old.id, old.text, old.search_tool, old.search_thinking); "
    "INSERT INTO conversation_fts(rowid, text, search_tool, search_thinking) "
    "VALUES (new.id, new.text, new.search_tool, new.search_thinking); END",
)
# Drop by name (the body is irrelevant to DROP TRIGGER); reverse order is
# cosmetic — order doesn't matter for independent triggers.
_CONV_FTS_TRIGGER_NAMES = ("conv_fts_au", "conv_fts_ad", "conv_fts_ai")

# #177: the parallel aux index (conversation_fts_aux) over the search_aux blob.
# All-or-nothing with the prose FTS under the single fts5_unavailable flag — its
# DDL lives beside the prose set and the SAME create/drop chokepoints handle
# both, so the two trigger sets can never drift. The AU trigger is keyed on
# ``AFTER UPDATE OF search_aux`` (a text-only update doesn't fire it, and the
# prose AU's ``AFTER UPDATE OF text`` doesn't fire this one).
_CONV_FTS_AUX_TRIGGER_DDL = (
    "CREATE TRIGGER IF NOT EXISTS conv_fts_aux_ai AFTER INSERT ON conversation_messages "
    "BEGIN INSERT INTO conversation_fts_aux(rowid, search_aux) VALUES (new.id, new.search_aux); END",
    "CREATE TRIGGER IF NOT EXISTS conv_fts_aux_ad AFTER DELETE ON conversation_messages "
    "BEGIN INSERT INTO conversation_fts_aux(conversation_fts_aux, rowid, search_aux) "
    "VALUES('delete', old.id, old.search_aux); END",
    "CREATE TRIGGER IF NOT EXISTS conv_fts_aux_au AFTER UPDATE OF search_aux ON conversation_messages "
    "BEGIN INSERT INTO conversation_fts_aux(conversation_fts_aux, rowid, search_aux) "
    "VALUES('delete', old.id, old.search_aux); "
    "INSERT INTO conversation_fts_aux(rowid, search_aux) VALUES (new.id, new.search_aux); END",
)
_CONV_FTS_AUX_TRIGGER_NAMES = ("conv_fts_aux_au", "conv_fts_aux_ad", "conv_fts_aux_ai")

# #177 S6: the LEGACY single-column prose trigger set, retained ONLY so a
# pre-swap install (and the legacy-shape test fixture) can stand up the old
# conversation_fts(text) shape. Fresh installs + the post-swap shape use the
# three-column _CONV_FTS_TRIGGER_DDL above. Same trigger names, so the swap's
# DROP-by-name (_CONV_FTS_TRIGGER_NAMES) covers either generation.
_CONV_FTS_LEGACY_TRIGGER_DDL = (
    "CREATE TRIGGER IF NOT EXISTS conv_fts_ai AFTER INSERT ON conversation_messages "
    "BEGIN INSERT INTO conversation_fts(rowid, text) VALUES (new.id, new.text); END",
    "CREATE TRIGGER IF NOT EXISTS conv_fts_ad AFTER DELETE ON conversation_messages "
    "BEGIN INSERT INTO conversation_fts(conversation_fts, rowid, text) "
    "VALUES('delete', old.id, old.text); END",
    "CREATE TRIGGER IF NOT EXISTS conv_fts_au AFTER UPDATE OF text ON conversation_messages "
    "BEGIN INSERT INTO conversation_fts(conversation_fts, rowid, text) "
    "VALUES('delete', old.id, old.text); "
    "INSERT INTO conversation_fts(rowid, text) VALUES (new.id, new.text); END",
)


def _create_conversation_fts_triggers(conn: sqlite3.Connection) -> None:
    """Create the #177 S6 split conversation_fts(text, search_tool,
    search_thinking) sync trigger set — idempotent (each is ``IF NOT EXISTS``).
    Single source of truth for the trigger DDL, shared by ``_apply_cache_schema``
    and ``clear_conversation_messages`` (#138). The caller must have already
    created ``conversation_fts`` in the split shape (the consolidated table
    replaced the old prose+aux pair, so there is no separate aux trigger set to
    create)."""
    for stmt in _CONV_FTS_TRIGGER_DDL:
        conn.execute(stmt)


def _create_conversation_fts_legacy_triggers(conn: sqlite3.Connection) -> None:
    """Create the LEGACY single-column prose + aux trigger sets over the
    pre-#177-S6 conversation_fts(text) + conversation_fts_aux(search_aux) pair.
    TEST/FIXTURE STANDUP ONLY — referenced solely by the migration-fixture
    builder + the swap tests to materialize the legacy shape the migration then
    upgrades. Production never calls this (the swap drops these trigger sets BY
    NAME via the ``_CONV_FTS_*_TRIGGER_NAMES`` tuples and recreates the split
    set). The caller must have created both legacy tables."""
    for stmt in _CONV_FTS_LEGACY_TRIGGER_DDL:
        conn.execute(stmt)
    for stmt in _CONV_FTS_AUX_TRIGGER_DDL:
        conn.execute(stmt)


def _drop_conversation_fts_triggers(conn: sqlite3.Connection) -> None:
    """Drop BOTH FTS5 sync trigger sets — prose + aux (#177) — idempotent
    (``IF EXISTS``). Swallows ``OperationalError`` per statement so a
    partial/absent trigger set (e.g. an FTS-unavailable build) is tolerated. One
    call site drops both sets so a failed aux create can't strand a live aux
    trigger over a missing conversation_fts_aux."""
    for name in _CONV_FTS_TRIGGER_NAMES + _CONV_FTS_AUX_TRIGGER_NAMES:
        try:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        except sqlite3.OperationalError:
            pass


# #217 S2 / E7: external-content FTS5 over the per-session AI title
# (conversation_ai_titles). The single column name MUST match the content table
# column (``ai_title``) BY NAME (external-content FTS5 resolves columns through
# the content table by name — same rule the message FTS follows). Fresh installs
# create this directly inside ``_apply_cache_schema``'s FTS5-available branch;
# migration 018 creates it for existing installs. The content-table ``rowid`` is
# STABLE across title updates (``_AI_TITLE_UPSERT_SQL`` is ``ON CONFLICT(session_id)
# DO UPDATE``, not delete+reinsert), so the AU trigger covers the update path and
# the external-content choice is sound.
_CONV_TITLE_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS conversation_title_fts USING fts5("
    "ai_title, content='conversation_ai_titles', content_rowid='rowid')"
)

# Title-FTS sync triggers (external-content FTS5). Mirror the message-FTS
# conv_fts_ai/ad/au idiom: AD/AU carry the OLD rowid via the ``'delete'``
# command. Defined ONCE here so every create/drop site stays in lockstep.
_CONV_TITLE_FTS_TRIGGER_DDL = (
    "CREATE TRIGGER IF NOT EXISTS conv_title_fts_ai "
    "AFTER INSERT ON conversation_ai_titles "
    "BEGIN INSERT INTO conversation_title_fts(rowid, ai_title) "
    "VALUES (new.rowid, new.ai_title); END",
    "CREATE TRIGGER IF NOT EXISTS conv_title_fts_ad "
    "AFTER DELETE ON conversation_ai_titles "
    "BEGIN INSERT INTO conversation_title_fts(conversation_title_fts, rowid, ai_title) "
    "VALUES('delete', old.rowid, old.ai_title); END",
    "CREATE TRIGGER IF NOT EXISTS conv_title_fts_au "
    "AFTER UPDATE OF ai_title ON conversation_ai_titles "
    "BEGIN INSERT INTO conversation_title_fts(conversation_title_fts, rowid, ai_title) "
    "VALUES('delete', old.rowid, old.ai_title); "
    "INSERT INTO conversation_title_fts(rowid, ai_title) "
    "VALUES (new.rowid, new.ai_title); END",
)
_CONV_TITLE_FTS_TRIGGER_NAMES = (
    "conv_title_fts_au", "conv_title_fts_ad", "conv_title_fts_ai")


def _create_conversation_title_fts_triggers(conn: sqlite3.Connection) -> None:
    """Create the #217 S2 title-FTS sync trigger set — idempotent (each is
    ``IF NOT EXISTS``). The caller must have already created
    ``conversation_title_fts`` (the triggers reference it)."""
    for stmt in _CONV_TITLE_FTS_TRIGGER_DDL:
        conn.execute(stmt)


def _drop_conversation_title_fts_triggers(conn: sqlite3.Connection) -> None:
    """Drop the title-FTS sync trigger set — idempotent (``IF EXISTS``). Swallows
    ``OperationalError`` per statement so an absent set (FTS5-unavailable build)
    is tolerated. P1-6: dropping these on a no-FTS5 build is what keeps a
    ``conversation_ai_titles`` upsert from firing a trigger against the missing
    vtable and rolling back the shared per-file ingest transaction."""
    for name in _CONV_TITLE_FTS_TRIGGER_NAMES:
        try:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        except sqlite3.OperationalError:
            pass


def _clear_conversation_file_touches(conn: sqlite3.Connection) -> None:
    """#217 S2 / I-3 (P1-4): drop ALL file-touch rows on a full
    clear/rebuild/truncation. ``conversation_file_touches`` is derived state keyed
    by ``conversation_messages.id``, and a full clear recycles those rowids — so a
    surviving touch row would point at a future, unrelated message. Tolerates a
    missing table defensively (belt-and-suspenders; ``_apply_cache_schema`` always
    creates it before the FTS branch, so it should exist on any schema'd conn)."""
    try:
        conn.execute("DELETE FROM conversation_file_touches")
    except sqlite3.OperationalError:
        pass   # table not yet created (pre-schema conn); nothing to clear


def clear_conversation_messages(conn: sqlite3.Connection) -> None:
    """Full-clear ``conversation_messages`` + its FTS index WITHOUT firing the
    per-row delete trigger O(rows) (#138).

    A bulk ``DELETE FROM conversation_messages`` fires ``conv_fts_ad`` once per
    row — each an FTS5 ``'delete'`` shadow-write — AND forfeits SQLite's
    no-trigger truncate fast-path, stalling the held ``cache.db.lock`` far
    longer than the ``session_entries`` clear alone. We suppress the triggers:

        drop all 3 conv_fts triggers
          → DELETE FROM conversation_messages   (true truncate fast-path now)
          → INSERT INTO conversation_fts(conversation_fts) VALUES('delete-all')
                                                (resets the external-content index)
          → recreate all 3 triggers

    Ordering is load-bearing: clearing the FTS index while the per-row delete
    trigger is still live makes the base ``DELETE`` write ``'delete'`` postings
    against already-gone rows and CORRUPTS the index (``database disk image is
    malformed``; verified on SQLite 3.53.1). Dropping the triggers first makes
    the base ``DELETE`` not touch the index at all; the explicit ``'delete-all'``
    then resets it cleanly and ``integrity-check`` still passes.

    Runs inside the caller's open transaction (the held ``cache.db.lock``); the
    caller owns the commit. When FTS5 is unavailable
    (``cache_meta.fts5_unavailable`` set → no triggers, no usable vtable),
    falls back to a plain base ``DELETE`` — there are no triggers to storm and a
    ``'delete-all'`` would error on the absent vtable."""
    try:
        fts_unavailable = conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='fts5_unavailable'"
        ).fetchone() is not None
    except sqlite3.OperationalError:
        # No cache_meta yet — only possible before the schema is applied, in
        # which case there is no FTS vtable/triggers either. Bias to the plain
        # DELETE: it can't storm what doesn't exist and won't touch a vtable.
        fts_unavailable = True

    if fts_unavailable:
        conn.execute("DELETE FROM conversation_messages")
        _clear_conversation_file_touches(conn)
        return

    _drop_conversation_fts_triggers(conn)
    conn.execute("DELETE FROM conversation_messages")
    _clear_conversation_file_touches(conn)
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('delete-all')")
    # #177 S6: the consolidated split table is the only FTS vtable on a swapped /
    # fresh DB. A pre-swap legacy install still carries conversation_fts_aux, so
    # reset it the same storm-free way ONLY when it physically exists (the swap
    # drops it; a fresh/split DB never had it). Triggers were dropped above, so
    # the base DELETE touched neither index.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_fts_aux'"
    ).fetchone() is not None:
        conn.execute(
            "INSERT INTO conversation_fts_aux(conversation_fts_aux) VALUES('delete-all')")
    _create_conversation_fts_triggers(conn)


# ── #294 S6: independent Codex conversation FTS lifecycle (§3.4) ──────────────
# The Codex normalized search index owns its OWN external-content FTS5 table +
# AI/AD/AU trigger trio, entirely separate from the Claude conversation_fts /
# conversation_title_fts objects. _apply_codex_conversation_fts runs from
# _apply_cache_schema BEFORE the Claude legacy-FTS early-return, so a legacy-shape
# Claude cache still gains it. The Codex-scoped `codex_fts_unavailable` cache_meta
# marker is INDEPENDENT of the Claude `fts5_unavailable` marker. Column names
# match codex_conversation_messages BY NAME (external-content FTS5 rule).
_CODEX_CONV_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS codex_conversation_fts USING fts5("
    "text, search_tool, search_thinking, "
    "content='codex_conversation_messages', content_rowid='id')"
)
# Trigger DDL lives in ONE place so the initial create and the storm-free
# full-clear (which drops + recreates the trigger set) can never drift. AD/AU use
# the external-content `'delete'` idiom; AU fires AFTER UPDATE OF the three
# indexed columns.
_CODEX_CONV_FTS_TRIGGER_DDL = (
    "CREATE TRIGGER IF NOT EXISTS codex_conv_fts_ai AFTER INSERT ON codex_conversation_messages "
    "BEGIN INSERT INTO codex_conversation_fts(rowid, text, search_tool, search_thinking) "
    "VALUES (new.id, new.text, new.search_tool, new.search_thinking); END",
    "CREATE TRIGGER IF NOT EXISTS codex_conv_fts_ad AFTER DELETE ON codex_conversation_messages "
    "BEGIN INSERT INTO codex_conversation_fts(codex_conversation_fts, rowid, text, search_tool, search_thinking) "
    "VALUES('delete', old.id, old.text, old.search_tool, old.search_thinking); END",
    "CREATE TRIGGER IF NOT EXISTS codex_conv_fts_au "
    "AFTER UPDATE OF text, search_tool, search_thinking ON codex_conversation_messages "
    "BEGIN INSERT INTO codex_conversation_fts(codex_conversation_fts, rowid, text, search_tool, search_thinking) "
    "VALUES('delete', old.id, old.text, old.search_tool, old.search_thinking); "
    "INSERT INTO codex_conversation_fts(rowid, text, search_tool, search_thinking) "
    "VALUES (new.id, new.text, new.search_tool, new.search_thinking); END",
)
_CODEX_CONV_FTS_TRIGGER_NAMES = ("codex_conv_fts_au", "codex_conv_fts_ad", "codex_conv_fts_ai")


def _create_codex_conversation_fts_triggers(conn: sqlite3.Connection) -> None:
    """Create the Codex FTS sync trigger set — idempotent (each ``IF NOT EXISTS``).
    Single source of truth, shared by ``_apply_codex_conversation_fts`` and the
    storm-free full-clear. The caller must have already created the vtable."""
    for stmt in _CODEX_CONV_FTS_TRIGGER_DDL:
        conn.execute(stmt)


def _drop_codex_conversation_fts_triggers(conn: sqlite3.Connection) -> None:
    """Drop the Codex FTS sync trigger set — idempotent (``IF EXISTS``). Swallows
    ``OperationalError`` per statement so an absent set (FTS5-unavailable build) is
    tolerated. Dropping these on a no-FTS5 build is what keeps a normalized INSERT
    from firing a trigger against the missing vtable and rolling back the shared
    per-file ingest transaction. NEVER touches any Claude FTS trigger."""
    for name in _CODEX_CONV_FTS_TRIGGER_NAMES:
        try:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        except sqlite3.OperationalError:
            pass


def _apply_codex_conversation_fts(conn: sqlite3.Connection) -> None:
    """Stand up / recover / degrade the independent Codex conversation FTS (§3.4).

    Four states: create (FTS5 available, no marker) → codex_conversation_fts +
    triggers; unavailable-at-creation → set ``codex_fts_unavailable`` + skip DDL;
    capable→unavailable reopen → drop ONLY the Codex triggers + set the marker;
    recovery (FTS5 available again, marker set) → drop/recreate the vtable + its
    triggers, ``rebuild`` from the base rows, clear the marker. Never touches any
    Claude FTS object."""
    if _fts5_available(conn):
        recovering = conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='codex_fts_unavailable'"
        ).fetchone() is not None
        try:
            if recovering:
                # A prior FTS5-unavailable run ingested normalized rows without a
                # live trigger (or a capable→unavailable→capable cycle left a stale
                # vtable). Drop + recreate the index and its triggers, then rebuild
                # from the base rows.
                _drop_codex_conversation_fts_triggers(conn)
                conn.execute("DROP TABLE IF EXISTS codex_conversation_fts")
            conn.execute(_CODEX_CONV_FTS_DDL)
            _create_codex_conversation_fts_triggers(conn)
            if recovering:
                conn.execute(
                    "INSERT INTO codex_conversation_fts(codex_conversation_fts) VALUES('rebuild')")
            conn.execute("DELETE FROM cache_meta WHERE key='codex_fts_unavailable'")
        except sqlite3.OperationalError:
            # Partial create cleanup, then mark unavailable so search uses LIKE.
            _drop_codex_conversation_fts_triggers(conn)
            try:
                conn.execute("DROP TABLE IF EXISTS codex_conversation_fts")
            except sqlite3.OperationalError:
                pass
            _set_cache_meta(conn, "codex_fts_unavailable", "1")
    else:
        # FTS5 unavailable on THIS build. If a prior capable run left the Codex
        # triggers, they now reference an unusable vtable and every normalized
        # INSERT would fail and roll back the shared per-file transaction — so
        # drop ONLY the Codex triggers (the vtable can't be dropped without fts5,
        # but with no triggers nothing writes to it) and mark unavailable.
        _drop_codex_conversation_fts_triggers(conn)
        _set_cache_meta(conn, "codex_fts_unavailable", "1")


def _codex_conversation_fts_full_clear(conn: sqlite3.Connection) -> None:
    """Storm-free FULL clear of every Codex normalized derived table (§3.4).

    Drops the Codex FTS triggers, truncates codex_conversation_messages (the
    no-trigger fast path), resets the external-content index via ``'delete-all'``,
    recreates the triggers, then clears file-touches + rollups. ``'delete-all'`` is
    valid here precisely because the WHOLE normalized corpus empties; the sequence
    makes repeated runs byte-idempotent at the FTS shadow-table level (migration
    025 re-run, cache-rebuild). Partial deletes (per-file truncation, root-set
    orphan prune) MUST NOT use this — they ride the per-row delete triggers so
    surviving conversations keep their postings. Falls back to a plain base DELETE
    when FTS5 is unavailable (no triggers, no usable vtable)."""
    try:
        fts_unavailable = conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='codex_fts_unavailable'"
        ).fetchone() is not None
    except sqlite3.OperationalError:
        fts_unavailable = True
    if fts_unavailable:
        conn.execute("DELETE FROM codex_conversation_messages")
    else:
        _drop_codex_conversation_fts_triggers(conn)
        conn.execute("DELETE FROM codex_conversation_messages")
        conn.execute(
            "INSERT INTO codex_conversation_fts(codex_conversation_fts) VALUES('delete-all')")
        _create_codex_conversation_fts_triggers(conn)
    for stmt in (
        "DELETE FROM codex_conversation_file_touches",
        "DELETE FROM codex_conversation_rollups",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # table not yet created (pre-schema conn); nothing to clear


def _eagerly_apply_cache_migrations() -> None:
    """Open cache.db so its pending migrations (notably
    ``001_dedup_highest_wins``) apply BEFORE stats migration 008's gate
    check.

    Why
    ---
    On the very first ``cctally`` invocation post-upgrade against a
    populated stats.db, the natural call order is:

      1. ``cmd_<reporting>`` opens stats.db via ``open_db()`` (runs the
         stats dispatcher → stats 008 fires).
      2. (Maybe) ``cmd_<jsonl-reader>`` opens cache.db via
         ``open_cache_db()`` (runs the cache dispatcher → cache 001
         fires).

    Step 1 happens BEFORE step 2 — and for commands that read stats.db
    only (e.g. ``cctally report`` without ``--sync-current``), step 2
    NEVER happens. So stats 008's gate finds no 001 marker in
    cache.db, raises ``MigrationGateNotMet``, the dispatcher defers,
    and ``report`` proceeds with stale ``weekly_cost_snapshots``
    forever (until the user happens to run a JSONL-reading command).

    This helper inverts the dependency: stats 008 itself triggers
    cache.db's dispatcher BEFORE checking the gate. After this returns,
    cache 001's marker is present (``cache_001_state="applied"``). But
    an eager-applied 001 WIPES the cache and clears the ``cache_meta``
    ``claude_ingest_walk_complete`` marker (spec D5) — and the gate
    (``_gate_001_post_ingest_completed`` → ``resolve_upgrade_gate``) now
    keys ingest-completeness on that walk-complete marker, not on a
    post-001 ``session_files`` row. So the gate DEFERs on this first
    invocation until a subsequent clean ``sync_cache`` re-walks the
    on-disk JSONL and re-establishes the marker. For users with no
    JSONL on disk (or no projects/ dir at all), ``disk_state="absent"``
    lets the resolver PROCEED (no data to lose) — 008 completes in the
    SAME invocation. For users with JSONL, the operator's next
    JSONL-reading command runs ``sync_cache``, which sets the
    walk-complete marker → the invocation after that runs 008
    successfully. That's worst-case one extra invocation instead of
    unbounded deferral.

    Lock ordering
    -------------
    Stats and cache are SEPARATE SQLite files with SEPARATE WAL locks.
    ``open_cache_db()`` does not touch stats.db. Stats.db is currently
    inside the migration dispatcher (the 008 handler hasn't started a
    ``BEGIN`` on the stats connection yet — that happens later in the
    body, AFTER this helper returns). No deadlock potential.

    Failure modes
    -------------
    If cache.db can't be opened (rare — disk full, permission denied,
    truly missing parent dir), let the exception propagate to the
    stats 008 body's ``try``, where the existing
    ``_is_transient_sqlite_error`` predicate translates it to
    ``MigrationGateNotMet``. The dispatcher then defers — symmetric
    with G4/G5 behavior on the read-only gate connection.

    Implementation note
    -------------------
    We open cache.db here directly (corruption-recovery connect + PRAGMAs +
    schema + dispatcher) rather than delegate to
    ``_cctally_cache.open_cache_db`` because the latter calls ``_cctally()``
    (a back-reference into ``sys.modules['cctally']``) which is set up by the
    bin/cctally entrypoint but absent in test harnesses that exercise the
    stats handler directly. The schema is applied via the shared
    ``_apply_cache_schema`` helper (cctally-dev#93, D4) — the SAME source
    ``open_cache_db`` uses — so the two paths can no longer drift (the prior
    hand-curated inline subset was the origin of the ``no such column:
    sf.project_path`` landmine). The only divergence from ``open_cache_db``
    is the Codex ``last_total_tokens`` ALTER + purge, which is deliberately
    Claude-irrelevant and provably never reached by this path (see
    ``_apply_cache_schema``'s docstring + spec D4/P1#3).
    """
    cache_db_path = _cctally_core.CACHE_DB_PATH
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(cache_db_path)
        conn.execute("SELECT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        # Corruption recovery mirrors the contract in
        # ``_cctally_cache.open_cache_db``: cache.db is fully
        # re-derivable from JSONL, so we unlink + recreate. Stay quiet
        # under tests — the dispatcher's gate-defer machinery handles
        # the case where this fails outright.
        eprint(f"[cache] corrupt cache DB ({exc}); recreating")
        try:
            cache_db_path.unlink()
        except FileNotFoundError:
            pass
        conn = sqlite3.connect(cache_db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        # Apply the shared cache.db schema (cctally-dev#93, D4). This is the
        # SAME source ``open_cache_db`` uses, including ``session_id`` /
        # ``project_path`` on session_files (009 joins ``sf.project_path`` on
        # the RO gate connection bootstrapped here; column resolution happens
        # at prepare time even with zero rows, so an absent column would raise
        # ``no such column: sf.project_path``) and the new ``cache_meta``
        # table. The Codex ``last_total_tokens`` ALTER stays out of the shared
        # helper and is intentionally not applied here (Claude-only path; see
        # ``_apply_cache_schema``'s docstring + spec D4/P1#3).
        _apply_cache_schema(conn)
        # Dispatcher (cache.db side). Runs every pending cache
        # migration, including ``001_dedup_highest_wins``. Idempotent —
        # if 001 has already applied, this is a fast-path return.
        _run_pending_migrations(
            conn, registry=_CACHE_MIGRATIONS, db_label="cache.db",
            recover_version_ahead=True,
        )
    finally:
        # Close immediately so the WAL writer lock (if any) is
        # released before the stats 008 body opens its read-only
        # gate connection.
        conn.close()


# === Region 7c: Cache migration 001_dedup_highest_wins (ccusage-parity fix) ===


def _recompute_banner_should_emit(
    *,
    data_present: bool,
) -> bool:
    """Shared banner-suppression gate for recompute-style migrations
    (cache 001, stats 008, 009, 010). Combines two conditions:

      (a) ``data_present`` — caller checked that the migration has
          actual rows to recompute. Empty-data topologies (most
          fresh-install upgrades, every golden fixture without seed
          rows) make the migration body a marker-only no-op; the
          banner would announce work that isn't happening. Caller
          owns this check because each migration scopes "data" to a
          different table (``session_entries`` for 001,
          ``weekly_cost_snapshots`` for 008, ``five_hour_blocks`` for
          009, ``percent_milestones`` for 010).

      (b) ``sys.argv[1]`` NOT in ``_BANNER_SUPPRESSED_COMMANDS``. Hot
          paths (``record-usage``, ``hook-tick``, ``sync-week``,
          ``cache-sync``, ``refresh-usage``) machine-consume stderr;
          ``tui`` / ``dashboard`` take over the screen; ``db`` and
          ``doctor`` surface migration state in their own reports;
          ``blocks`` is a stdout-formatted table whose stderr noise
          confuses scripted pipelines. Banner still lands on the
          next interactive non-report command (``report``,
          ``weekly``, ``percent-breakdown``, etc.) once on upgrade.
          Subgroup forms (``cctally claude/codex <cmd>``, issue #86
          Session B) carry the source group in ``argv[1]`` and the
          leaf in ``argv[2]``; we resolve the leaf so suppression is
          byte-identical to the flat alias.

    Returns True iff the banner should be printed. Defensive: any
    error reading ``sys.argv`` falls back to "don't print" — silence
    is the safer side under uncertainty (worst case, a heavy user
    misses the one-line announcement; not a correctness regression).

    SW5-extended — replaces the per-migration ad-hoc banner gates
    that drifted between 001 (which checked argv1 in suppression
    list) and 008/009/010 (which only checked data-table emptiness).
    The asymmetry caused ``cctally blocks`` to emit 009's banner
    even when ``record-usage`` would not — surfaced by
    ``floor-band-trap`` golden-terminal.txt drift.
    """
    if not data_present:
        return False
    try:
        argv1 = sys.argv[1] if len(sys.argv) > 1 else None
        # Subgroup forms (`cctally claude <cmd>` / `cctally codex <cmd>`) carry
        # the source group in argv[1]; the suppression key is the leaf command
        # in argv[2]. Resolve it so the recompute banner suppresses identically
        # to the flat alias (issue #86 Session B; matches the args.command leaf
        # resolution used by the error-sentinel banner). Purely additive — flat
        # invocations (argv1 not in {claude,codex}) are byte-identical to before.
        if argv1 in ("claude", "codex") and len(sys.argv) > 2:
            argv1 = sys.argv[2]
    except Exception:
        argv1 = None
    if argv1 in _BANNER_SUPPRESSED_COMMANDS:
        return False
    return True


def _001_banner_should_emit(conn: sqlite3.Connection) -> bool:
    """SW5 — gate cache migration 001's banner. Thin shim around the
    shared ``_recompute_banner_should_emit`` helper: probes
    ``session_entries`` for non-emptiness, then defers to the shared
    suppression-argv1 check.

    Kept as a named function (rather than inlined at the call site)
    because cache migration 001's data check requires a defensive
    ``sqlite3.Error`` swallow — the migration runs early and the
    table may not yet exist on certain ALTER-mid-upgrade topologies.
    Stats 008/009/010 don't need this swallow because their gate
    runs after the schema is fully bootstrapped.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM session_entries LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return False
    return _recompute_banner_should_emit(data_present=row is not None)


def _cache_db_lock_path_for_conn(conn: sqlite3.Connection) -> "pathlib.Path | None":
    """Return the fcntl lock-file path for the cache.db a connection is
    attached to — ``<main-db-file>.lock`` — or ``None`` for a path-less
    (``:memory:`` / temp) connection.

    Derived from the LIVE connection (``PRAGMA database_list``) rather than
    the ``CACHE_LOCK_PATH`` constant so it tracks whatever cache.db the
    handler was handed: production uses ``APP_DIR/cache.db`` whose sibling
    is exactly ``CACHE_LOCK_PATH`` (the lock ``sync_cache`` opens — the
    ``CACHE_LOCK_PATH == <CACHE_DB_PATH>.lock`` identity is asserted by
    ``tests/test_migration_gate_concurrency.py``), while tests follow their
    tmp cache.db so no real-home lock is ever touched. A path-less
    connection has no sibling lock file and no cross-process concurrency to
    guard, so the caller skips locking.
    """
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.DatabaseError:
        return None
    for row in rows:
        # cache.db connection has no row_factory -> tuple (seq, name, file).
        if row[1] == "main":
            db_file = row[2]
            if not db_file:
                return None  # :memory: / temp -> no sibling lock file
            return pathlib.Path(str(db_file) + ".lock")
    return None


def _cache_db_codex_lock_path_for_conn(
    conn: sqlite3.Connection,
) -> "pathlib.Path | None":
    """Return this connection's ``<cache.db>.codex.lock`` sibling.

    Codex ingest deliberately uses a separate fcntl lock from Claude ingest.
    Derive it from the connection rather than the global path constant so a
    migration test or recovery connection never contends on the caller's real
    cache lock. ``sync_codex_cache`` opens this exact sibling in production.
    """
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.DatabaseError:
        return None
    for row in rows:
        if row[1] == "main":
            db_file = row[2]
            if not db_file:
                return None
            return pathlib.Path(str(db_file) + ".codex.lock")
    return None


@cache_migration("001_dedup_highest_wins")
def _001_dedup_highest_wins(conn: sqlite3.Connection) -> None:
    """One-time re-ingest of session_entries with corrected msg_id+req_id dedup.

    The previous INSERT OR IGNORE kept the streaming-intermediate row of each
    (msg_id, req_id) pair (output_tokens=1, no ``speed`` field) and rejected
    the post-stream finalization row (output_tokens=N, ``speed='standard'``).
    The winner's data is not recoverable from session_entries alone — it was
    never inserted under the old rule. We wipe ``session_entries`` +
    ``session_files`` so the next ``sync_cache`` re-reads JSONL under the new
    ON CONFLICT DO UPDATE clause (highest-token-total wins, ``speed`` set
    breaks ties).

    Codex tables (``codex_session_entries``, ``codex_session_files``) are NOT
    touched — the bug is Claude-side only.

    Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I2.

    Invariants:
      * Marker row INSERTed inside the same BEGIN/COMMIT as the DELETEs.
      * Empty session_entries (no JSONL ingested yet) still writes the
        marker — table-emptiness is not the sentinel (CLAUDE.md "Pricing
        & schema"). A truly fresh install short-circuits earlier via the
        dispatcher's ``fresh_install`` fast-path; this handler only sees
        the post-shipped-empty case where the cache.db schema and
        migration tables already exist but ``session_entries`` is empty.
      * Migration handler does NOT call ``_log_migration_error`` /
        ``_clear_migration_error_log_entries``; the dispatcher owns that
        surface (CLAUDE.md "Migration error sentinel is uniform").

    SW5 — Banner suppression. Two gates compose:

      (a) ``session_entries`` non-emptiness — if the table is empty (most
          fresh-install upgrade topologies + every golden fixture), the
          handler's body is a marker-only no-op and the banner has
          nothing to announce. Mirrors the snapshot-rows gate on
          migration 008's banner.

      (b) ``sys.argv[1]`` in ``_BANNER_SUPPRESSED_COMMANDS`` — the same
          set the dispatcher consults for its post-failure banner. Hot
          paths (record-usage, hook-tick, sync-week, cache-sync,
          refresh-usage, tui, dashboard, db, doctor) machine-consume
          stderr or take over the screen, so the banner has nowhere
          safe to land. Migration handlers don't receive ``args``, so
          we read ``sys.argv`` directly — `argparse` hasn't run yet at
          handler time anyway. Interactive surfaces (``report``,
          ``weekly``, ``percent-breakdown``, etc.) still see it once.
    """
    # #105 — mutual exclusion with ``sync_cache``. Acquire the SAME
    # ``cache.db.lock`` fcntl flock ``sync_cache`` holds for its entire
    # walk, BEFORE the ``BEGIN IMMEDIATE`` below. Both paths therefore
    # acquire fcntl -> SQLite write lock in ONE consistent order, so there
    # is no opposite-order deadlock (the hazard that deferred this fix:
    # SQLite-then-fcntl in 001 vs fcntl-then-SQLite in sync_cache). With
    # the lock held across the wipe, 001's destructive DELETEs can never
    # interleave a ``sync_cache`` walk: a sync runs entirely before 001
    # (then 001 wipes ``session_files`` so the next sync re-ingests from
    # offset 0) or entirely after (reading an empty post-wipe baseline).
    # That makes the compound straddle — a sync reading its ``existing``
    # baseline pre-wipe, then committing a full-size ``session_files`` row
    # whose pre-wipe prefix 001 just deleted — structurally impossible.
    #
    # On contention (a sync is mid-walk) we DEFER via ``MigrationGateNotMet``
    # BEFORE touching any data: the cache stays fully consistent, the
    # dispatcher records 001 as still-pending (no error log, no banner) and
    # retries it on the next open — matching ``sync_cache``'s own
    # non-blocking LOCK_NB-and-bail and the framework's "defer is the safe
    # side" contract. 008/009/010 already defer while 001 is pending, so the
    # system stays safe until a non-contended instant applies it.
    lock_path = _cache_db_lock_path_for_conn(conn)
    lock_fh = None
    if lock_path is not None:
        lock_fh = open(lock_path, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fh.close()
            raise MigrationGateNotMet(
                "cache.db.lock held by a concurrent sync_cache; deferring "
                "cache 001 dedup wipe (#105)"
            )
    try:
        _001_dedup_highest_wins_locked(conn)
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()


def _001_dedup_highest_wins_locked(conn: sqlite3.Connection) -> None:
    """Body of cache 001, run with the ``cache.db.lock`` flock already held
    (or skipped for a path-less connection). Split from the public handler
    so the lock acquire/release wraps the whole wipe (#105); see
    ``_001_dedup_highest_wins`` for the lock-ordering rationale."""
    if _001_banner_should_emit(conn):
        eprint(
            "[cctally] Re-ingesting Claude session history with "
            "corrected dedup (one-time; may take 10-30s depending on "
            "JSONL volume)..."
        )
    # D3 — BEGIN IMMEDIATE so the destructive DELETEs are race-guarded,
    # not just the marker insert. The dispatcher snapshots the applied
    # set ONCE before its registry walk (``_run_pending_migrations``),
    # so two concurrent openers (e.g. dashboard + CLI on the same
    # cache.db) can BOTH classify 001 as pending and BOTH enter this
    # handler. With a plain ``BEGIN`` (deferred), each acquires the
    # write lock only on its first DELETE: the loser would wait for the
    # winner's COMMIT, then DELETE — wiping the rows the winner's
    # subsequent ``sync_cache`` already reingested, leaving the cache
    # partially rebuilt until another full sync. ``BEGIN IMMEDIATE``
    # grabs the write lock up front, so the loser blocks here BEFORE
    # touching any data; once it acquires the lock the winner's marker
    # is already committed, and the in-transaction re-check below turns
    # the loser's body into a no-op. The marker INSERT stays
    # ``INSERT OR IGNORE`` as a belt-and-suspenders against an
    # IntegrityError banner.
    conn.execute("BEGIN IMMEDIATE")
    try:
        already_applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ? LIMIT 1",
            ("001_dedup_highest_wins",),
        ).fetchone() is not None
        if already_applied:
            # A concurrent opener won the race and already wiped +
            # stamped 001 (and may already be repopulating via
            # sync_cache). Re-running the DELETEs here would destroy that
            # reingested data. Commit the empty IMMEDIATE transaction
            # (releases the write lock) and return — the marker is
            # present, so the dispatcher records us as applied.
            conn.commit()
            return
        conn.execute("DELETE FROM session_entries")
        conn.execute("DELETE FROM session_files")
        # Clear the walk-complete sentinel atomically with the wipe
        # (cctally-dev#93, D5/D2): a wiped session_entries must never coexist
        # with a "complete walk" marker. The end-of-loop write in sync_cache
        # re-establishes it only after a subsequent clean walk. In production
        # ``_apply_cache_schema`` always creates ``cache_meta`` before the
        # dispatcher fires 001 (open_cache_db / _eagerly_apply_cache_migrations
        # both apply the schema first), so the table is present. Tolerate its
        # absence defensively (a pre-cache_meta cache.db invoked through the
        # handler directly, e.g. older per-migration goldens): a missing table
        # means there is no stale marker to clear, so the no-op is correct.
        # The "no such table" prepare-time error never opened a write, so the
        # enclosing BEGIN IMMEDIATE transaction stays intact for the stamp +
        # commit below.
        try:
            conn.execute("DELETE FROM cache_meta WHERE key='claude_ingest_walk_complete'")
        except sqlite3.OperationalError as exc:
            if not _is_no_such_table_error(exc):
                raise
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) "
            "VALUES (?, ?)",
            ("001_dedup_highest_wins", now_utc_iso()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# === Region 7c: Cache migration 002_conversation_messages_backfill ===

@cache_migration("002_conversation_messages_backfill")
def _002_conversation_messages_backfill(conn: sqlite3.Connection) -> None:
    """Mark the ``conversation_messages`` backfill pending (Plan 1 Task 5; the
    deferral is issue #139).

    The table + indexes + FTS already live in ``_apply_cache_schema`` (so fresh
    installs have them and the dispatcher stamps THIS migration without running
    it — there is no history to populate). This handler runs only on an
    EXISTING install (``session_entries`` non-empty), which needs the message
    index populated from the full JSONL history.

    Rather than walk that history INLINE — which blocked the triggering command
    until the whole (potentially ~1M-line) backfill completed, including a
    stats-only ``cctally report`` that fires the cache dispatcher via
    ``_eagerly_apply_cache_migrations`` but never opens cache.db for reads
    (issue #139) — this handler just sets the ``conversation_backfill_pending``
    cache_meta flag and returns in microseconds. The actual offset-0 backfill
    runs on the next ``sync_cache``, which already holds the ``cache.db.lock``
    flock and owns the walker (see ``_cctally_cache.sync_cache``); a
    cache-consuming command — or, most often, the background ``hook-tick`` —
    absorbs the one-time walk where the latency is expected/invisible. Because
    the handler no longer touches JSONL it needs no flock and cannot contend
    with a concurrent sync, so the old non-blocking-flock +
    ``MigrationGateNotMet`` defer dance is gone.

    Does NOT self-stamp its ``schema_migrations`` marker: the dispatcher owns
    the central stamp on the existing-install success path (issue #140), calling
    ``_stamp_applied(conn, m.name)`` right after this handler returns cleanly —
    so the migration persists and is never re-walked (re-setting the flag) on a
    subsequent ``open_cache_db()``. This handler only commits the cache_meta
    flag. The flag itself is consumed + cleared by the first ``sync_cache`` that
    sees it (idempotent + crash-resumable there); a ``cache-sync --rebuild``
    clears it directly since its normal offset-0 walk repopulates the index."""
    _set_cache_meta(conn, "conversation_backfill_pending", "1")
    conn.commit()


@cache_migration("003_conversation_reingest_tool_ids")
def _003_conversation_reingest_tool_ids(conn: sqlite3.Connection) -> None:
    """Flag-only re-ingest of conversation_messages so tool_use.id /
    tool_result.tool_use_id / preview land on existing history (#164).

    The destructive clear + offset-0 backfill run in sync_cache UNDER the
    cache.db.lock flock — NOT here. Clearing in the handler would violate the
    lock discipline cache-001 follows and would empty the reader on
    stats-only / eager-migration opens or ``dashboard --no-sync``. A distinct
    flag from 002's conversation_backfill_pending: 002 = backfill-without-clear;
    003 = clear-then-backfill. The dispatcher stamps this migration centrally
    on the existing-install path (issue #140); a fresh install stamps it
    without running (empty table), and the flag — if ever set — is a harmless
    no-op there."""
    _set_cache_meta(conn, "conversation_reingest_pending", "1")
    conn.commit()


@cache_migration("004_conversation_reingest_subagent_kind")
def _004_conversation_reingest_subagent_kind(conn: sqlite3.Connection) -> None:
    """Flag-only re-ingest of conversation_messages so the spawn ``subagent_type``
    and the record-level ``toolUseResult`` agentId/meta land on existing history
    (#166). REUSES 003's ``conversation_reingest_pending`` flag — sync_cache
    already consumes it (clear + offset-0 backfill under the cache.db.lock flock),
    and the offset-0 walk re-parses every JSONL through the current parser, so the
    new fields land with zero new consumption code. A distinct ``schema_migrations``
    marker is what triggers this reingest on an existing install that already has
    003 applied; the flag is the generic 'conversation index needs a full
    clear+reingest' signal. Central stamp via the dispatcher (issue #140); a fresh
    install stamps it without running (empty table -> the flag, if ever set, is a
    harmless no-op)."""
    _set_cache_meta(conn, "conversation_reingest_pending", "1")
    conn.commit()


@cache_migration("005_conversation_reingest_meta")
def _005_conversation_reingest_meta(conn: sqlite3.Connection) -> None:
    """Flag-only re-ingest of conversation_messages so injected ``isMeta`` user
    lines (skill bodies, git-context, "Continue…", image placeholders,
    slash-command caveats) are reclassified from ``entry_type='human'`` to the
    new ``'meta'`` value and stop rendering as "YOU" prompts in the reader.
    REUSES 003's ``conversation_reingest_pending`` flag exactly like 004 — the
    offset-0 walk in sync_cache (clear + backfill under the cache.db.lock flock)
    re-parses every JSONL through the now-meta-aware parser, so the new
    classification lands with zero new consumption code. A distinct
    ``schema_migrations`` marker is what triggers this reingest on installs that
    already have 003/004 applied. Central stamp via the dispatcher (issue #140);
    a fresh install stamps it without running (empty table -> the flag, if ever
    set, is a harmless no-op)."""
    _set_cache_meta(conn, "conversation_reingest_pending", "1")
    conn.commit()


@cache_migration("006_conversation_reingest_source_tool_use_id")
def _006_conversation_reingest_source_tool_use_id(conn: sqlite3.Connection) -> None:
    """Flag-only re-ingest of conversation_messages so the message-level
    ``sourceToolUseID`` lands on existing history as the new
    ``source_tool_use_id`` column — the link the reader uses to fold a
    Skill-invoked skill body into its Skill tool chip.

    Sets a DISTINCT flag, ``conversation_source_tool_use_reingest_pending``,
    NOT the shared ``conversation_reingest_pending``. The shared flag also gates
    migration 005's read-time *human*-fallback (``_reingest_pending`` in the
    query kernel); re-arming it here would re-enable that fallback after it was
    consumed and could misclassify a genuine human prompt that happens to start
    with the skill preamble as a collapsed skill pill during 006's pre-reingest
    window. sync_cache consumes EITHER flag (clear + offset-0 backfill under the
    cache.db.lock flock) and the offset-0 walk re-parses every JSONL through the
    sourceToolUseID-aware parser, so the column lands with zero new consumption
    code. A distinct ``schema_migrations`` marker is what triggers this reingest
    on installs already at 005. Central stamp via the dispatcher (issue #140); a
    fresh install stamps it without running (empty table -> the flag, if ever
    set, is a harmless no-op)."""
    _set_cache_meta(conn, "conversation_source_tool_use_reingest_pending", "1")
    conn.commit()


@cache_migration("007_conversation_reingest_enrichment")
def _007_conversation_reingest_enrichment(conn: sqlite3.Connection) -> None:
    """Flag-only re-ingest so the enriched data contract — structured tool
    ``input`` + ``input_truncated``, the raised result cap + ``full_length``,
    ``stop_reason``/``attribution_skill``/``attribution_plugin``, and the
    ``search_aux`` FTS-aux blob — lands on existing history. Sets the DISTINCT
    ``conversation_reingest_enrichment_pending`` flag (NOT the shared
    ``conversation_reingest_pending``, which also gates migration 005's read-time
    human-fallback in the query kernel — re-arming it could misclassify a genuine
    human prompt during the pre-reingest window). sync_cache consumes the flag
    under the cache.db.lock flock (clear + offset-0 backfill); the offset-0 walk
    re-parses every JSONL through the enriched parser, so the new fields/columns
    land with zero new consumption code. Central stamp via the dispatcher (#140);
    a fresh install stamps it without running (empty table -> the flag, if ever
    set, is a harmless no-op)."""
    _set_cache_meta(conn, "conversation_reingest_enrichment_pending", "1")
    conn.commit()


@cache_migration("008_session_entries_speed_backfill")
def _008_session_entries_speed_backfill(conn: sqlite3.Connection) -> None:
    """Backfill the materialized ``session_entries.speed`` column from the
    legacy ``usage_extra_json`` blob (#181). ``speed`` is the ONLY non-token
    ``usage`` key any consumer reads (``<model>-fast`` rendering in
    _lib_aggregators + the _should_replace dedup tiebreak); materializing it
    lets the hot read paths (iter_entries / get_claude_session_entries) stop
    ``json.loads``-ing the deeply-nested blob per row — the per-tick dashboard
    rebuild was pegging a core on a ~261K-row cache.

    The column is added by _apply_cache_schema's add_column_if_missing (which
    runs before this dispatcher), so it always exists here. This handler only
    backfills existing rows; new ingests write the column directly and write
    NULL to usage_extra_json. ``WHERE speed IS NULL`` self-guards re-runs.
    We do NOT rewrite/NULL usage_extra_json on existing rows or VACUUM — the
    stale blob has no reader and is reclaimed on the next cache-sync --rebuild.
    json_extract returns NULL when '$.speed' is absent, so speed-less rows stay
    NULL. Central stamp via the dispatcher (#140); a fresh install stamps it
    without effect (empty table)."""
    conn.execute(
        "UPDATE session_entries "
        "SET speed = json_extract(usage_extra_json, '$.speed') "
        "WHERE speed IS NULL AND usage_extra_json IS NOT NULL"
    )
    conn.commit()


@cache_migration("009_conversation_media_reingest")
def _009_conversation_media_reingest(conn: sqlite3.Connection) -> None:
    """Flag-only re-ingest so the #177 S4 media/web enrichment — tool_result
    ``media[]`` placeholders (iter_media_items ordinals), user-content media
    ``index``, and the bounded ``web_search``/``web_fetch`` toolUseResult
    captures — lands on existing history. Sets the DISTINCT
    ``conversation_media_reingest_pending`` flag (NOT the shared
    ``conversation_reingest_pending``, which also gates migration 005's
    read-time human-fallback in the query kernel). Consumption rides the #179
    RESUMABLE per-file reingest (_resumable_reingest_conversation_messages) —
    the flag is wired into _REINGEST_FLAG_KEYS + both flag SELECTs + the two
    cleanup DELETE lists in _cctally_cache.py (all five sites; missing one
    either never triggers or re-arms forever). Central stamp via the
    dispatcher (#140); a fresh install stamps it without running."""
    _set_cache_meta(conn, "conversation_media_reingest_pending", "1")
    conn.commit()


@cache_migration("010_conversation_search_split")
def _010_conversation_search_split(conn: sqlite3.Connection) -> None:
    """Flag-only arm for the #177 S6 search-column split. Sets
    ``conversation_search_split_pending``; sync_cache consumes it under the
    cache.db.lock flock (cursor-resumable blocks_json backfill of
    search_tool/search_thinking, then an atomic legacy->split FTS swap+rebuild —
    see _cctally_cache._consume_search_split). The handler does NO data work so
    the dispatcher's central stamp (#140) marks a genuinely-complete handler; a
    fresh install stamps it WITHOUT running (its schema is already split via
    _apply_cache_schema, so the consumer finds no flag and no-ops). Mirrors the
    flag-only reingest pattern of 002/003/007/009."""
    _set_cache_meta(conn, "conversation_search_split_pending", "1")
    conn.commit()


def _swap_conversation_fts_to_split(conn: sqlite3.Connection) -> None:
    """Legacy two-table shape -> split multi-column shape, in one transaction.
    Explicit DROPs of BOTH old tables + triggers (an ``IF NOT EXISTS`` create
    would silently keep the old same-name single-column conversation_fts — spec
    F9), then create the split DDL + new triggers and rebuild the index from the
    base table. Runs under the caller's held cache.db.lock flock (the
    _consume_search_split end-transaction)."""
    _drop_conversation_fts_triggers(conn)
    conn.execute("DROP TABLE IF EXISTS conversation_fts")
    conn.execute("DROP TABLE IF EXISTS conversation_fts_aux")
    conn.execute(_CONV_FTS_SPLIT_DDL)
    _create_conversation_fts_triggers(conn)
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('rebuild')")


def conversation_search_depth(conn: sqlite3.Connection) -> str:
    """'prose-only' while migration 010's backfill is pending, else 'full'.
    Surfaced additively on the search/find responses so the client can degrade
    the Tools/Thinking facets during the one-time index split. An
    OperationalError (no cache_meta table yet) degrades to 'full'."""
    try:
        pending = conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='conversation_search_split_pending'"
        ).fetchone()
    except sqlite3.OperationalError:
        return "full"
    return "prose-only" if pending else "full"


@cache_migration("011_conversation_promote_command_args")
def _011_conversation_promote_command_args(conn: sqlite3.Connection) -> None:
    """Flag-only arm for #188 bug 4. Sets
    ``conversation_promote_command_args_pending``; sync_cache consumes it under
    the cache.db.lock flock (cursor-resumable: flip legacy ``entry_type='META'``
    command-marker rows whose ``<command-args>`` carry a real user prompt to
    ``entry_type='human'`` with ``text=args``, recomputing the split search
    columns so the args enter the entry_type='human' list-title/prompts facet and
    the FTS index — see _cctally_cache._consume_promote_command_args). The
    handler does NO data work so the dispatcher's central stamp (#140) marks a
    genuinely-complete handler; a fresh install stamps it WITHOUT running (its
    rows are already promoted at ingest, so the consumer finds nothing to flip).
    DISPLAY is already fixed read-time (no migration needed); the migration's
    sole job is FTS-searchability + the list-title facet on legacy data. Mirrors
    the flag-only pattern of 002/003/007/009/010."""
    _set_cache_meta(conn, "conversation_promote_command_args_pending", "1")
    conn.commit()


@cache_migration("012_create_conversation_ai_titles")
def _012_create_conversation_ai_titles(conn: sqlite3.Connection) -> None:
    """Flag-only arm for #193. The conversation_ai_titles table itself is created
    by _apply_cache_schema (runs on every open, fresh + existing installs); this
    migration sets ``ai_titles_backfill_pending`` so sync_cache walks all history
    once via backfill_ai_titles under the cache.db.lock flock. No data work here
    -> the dispatcher's central stamp (#140) marks a complete handler; a fresh
    install stamps WITHOUT a populated history (its incremental walk fills the
    table as it ingests, and the consumed backfill no-ops). Mirrors 002/010/011."""
    _set_cache_meta(conn, "ai_titles_backfill_pending", "1")
    conn.commit()


@cache_migration("013_create_conversation_sessions")
def _013_create_conversation_sessions(conn: sqlite3.Connection) -> None:
    """Flag-only arm for the conversation_sessions browse-rail rollup. The table
    is created by _apply_cache_schema (every open); this sets
    conversation_sessions_backfill_pending so sync_cache does the one-time full
    GROUP BY recompute under the cache.db.lock flock. No data work here — the
    dispatcher's central stamp (#140) marks a complete handler; a fresh install
    stamps WITHOUT running the handler, so the flag is NOT set there, which is
    correct (empty messages -> empty rollup; the incremental DELETE+INSERT
    re-derive fills both in lockstep). Mirrors 012."""
    _set_cache_meta(conn, "conversation_sessions_backfill_pending", "1")
    conn.commit()


@cache_migration("014_conversation_queued_prompt_reingest")
def _014_conversation_queued_prompt_reingest(conn: sqlite3.Connection) -> None:
    """Flag-only re-ingest so a message typed while the agent was busy — QUEUED
    and persisted as an ``attachment`` row (``attachment.type=="queued_command"``,
    ``commandMode=="prompt"``) rather than a ``type:"user"`` turn — lands on
    existing history. The parser (``_queued_prompt_row``) now promotes those to a
    synthetic HUMAN turn at ingest, so the offset-0 re-parse re-derives them with
    zero new consumption code. Sets the DISTINCT
    ``conversation_queued_prompt_reingest_pending`` flag (NOT the shared
    ``conversation_reingest_pending``, which also gates migration 005's read-time
    human-fallback in the query kernel). Consumption rides the #179 RESUMABLE
    per-file reingest (_resumable_reingest_conversation_messages) — the flag is
    wired into _TARGETED_DECLINE_FLAGS + _REINGEST_FLAG_KEYS + both flag SELECTs +
    the two cleanup DELETE lists in _cctally_cache.py (all five sites; missing one
    either never triggers or re-arms forever). Central stamp via the dispatcher
    (#140); a fresh install stamps it WITHOUT running (empty table -> the flag, if
    ever set, is a harmless no-op). Mirrors 007/009."""
    _set_cache_meta(conn, "conversation_queued_prompt_reingest_pending", "1")
    conn.commit()


@cache_migration("015_conversation_sessions_filter_columns")
def _015_conversation_sessions_filter_columns(conn: sqlite3.Connection) -> None:
    """Add the browse-filter columns to the conversation_sessions rollup
    (project_label, cost_usd, cache_rebuild_count) so the rail's date/project/
    cost/cache-rebuild filters are pure-SQL predicates (spec §1). ALTERs are
    idempotent (duplicate-column tolerated). Arms the SHARED
    conversation_sessions_backfill_pending flag so the next sync_cache full
    recompute fills the new columns via the augmented
    _recompute_conversation_sessions — keeping the heavy per-session assemble
    (cache_rebuild_count) off the migration's critical path, mirroring 013.
    Central stamp via the dispatcher (#140); handler does NOT self-stamp.

    A fresh install gets the three columns from _apply_cache_schema's CREATE
    TABLE and stamps 015 WITHOUT running this handler — the ALTERs no-op there
    (already present), and the empty rollup needs no backfill (the incremental
    DELETE+INSERT re-derive fills all columns in lockstep). Mirrors 013."""
    for ddl in (
        "ALTER TABLE conversation_sessions ADD COLUMN project_label TEXT",
        "ALTER TABLE conversation_sessions ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0",
        "ALTER TABLE conversation_sessions ADD COLUMN cache_rebuild_count INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # idempotent: column already present
    _set_cache_meta(conn, "conversation_sessions_backfill_pending", "1")
    conn.commit()


def _conversation_messages_has_column(conn: sqlite3.Connection, column: str) -> bool:
    """True iff ``conversation_messages`` carries *column*. Tolerates a missing
    table (a path-less / schema-not-applied connection) -> False."""
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(conversation_messages)")]
    except sqlite3.OperationalError:
        return False
    return column in cols


def _legacy_aux_fts_present(conn: sqlite3.Connection) -> bool:
    """True iff the pre-#177-S6 ``conversation_fts_aux`` external-content table
    OR any of its sync triggers (which reference ``search_aux``) still exist.

    While EITHER is live, an ``ALTER TABLE conversation_messages DROP COLUMN
    search_aux`` FAILS (the trigger/index body references the column). Migration
    010's state machine (``_consume_search_split`` in sync_cache) owns tearing
    these down; 016 only WAITS until they are gone. Tolerates a missing
    ``sqlite_master`` read -> False (no aux shape to block on)."""
    try:
        _trig_ph = ",".join("?" for _ in _CONV_FTS_AUX_TRIGGER_NAMES)
        row = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE (type='table' AND name='conversation_fts_aux') "
            f"   OR (type='trigger' AND name IN ({_trig_ph})) "
            "LIMIT 1",
            _CONV_FTS_AUX_TRIGGER_NAMES,
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


@cache_migration("016_drop_search_aux")
def _016_drop_search_aux(conn: sqlite3.Connection) -> None:
    """Drop the documented-dead ``conversation_messages.search_aux`` column
    (#217 S1 / U7a) — it has been ``''`` on every row since #177 S6 split the
    non-prose search index into ``search_tool``/``search_thinking`` and the live
    ``conversation_fts`` stopped referencing it.

    THREE guards, in order (the drop is the LAST thing that happens):

      1. Column-presence — idempotent skip-as-applied when ``search_aux`` is
         already gone. A fresh install never carries it (``_apply_cache_schema``
         no longer emits it post-#217), so the dispatcher stamps 016 without the
         column ever existing; an existing install drops it once.
      2. ``sqlite_version() >= 3.35`` — ``ALTER TABLE … DROP COLUMN`` did not
         exist before SQLite 3.35. On an older build we SKIP-as-applied, leaving
         the harmless dead column (never a hard fail; cache.db is re-derivable,
         and a later open on a newer SQLite still can't re-run a stamped
         migration — the column simply persists, which is benign).
      3. Search-split-consumed gate (Codex P1) — DEFER via ``MigrationGateNotMet``
         (retried next open) when migration 010's
         ``conversation_search_split_pending`` flag is set OR the legacy
         ``conversation_fts_aux`` table / its triggers still exist. ``DROP COLUMN``
         FAILS while a trigger references ``search_aux`` (confirmed in-memory), so
         016 must wait for ``_consume_search_split`` (sync_cache) to backfill the
         split columns + swap the FTS shape. 016 does NOT tear the aux table down
         itself — 010's state machine owns that. On a ``--no-sync``-forever DB the
         split never consumes and the column persists harmlessly (re-derivable).

    The handler does its own DDL only; the dispatcher central-stamps the marker
    (#140) on a clean return (the apply path) — it does NOT stamp on the
    ``MigrationGateNotMet`` defer path, so the migration stays pending and retries.
    """
    if not _conversation_messages_has_column(conn, "search_aux"):
        # Guard 1: already absent (fresh install, or a prior 016 run). The
        # dispatcher central-stamps on this clean return.
        return
    sqlite_version = tuple(
        int(p) for p in sqlite3.sqlite_version.split(".")[:3]
    )
    if sqlite_version < (3, 35, 0):
        # Guard 2: no DROP COLUMN on this build. Skip-as-applied (stamp on the
        # clean return) — the dead column stays, harmless.
        return
    try:
        split_pending = conn.execute(
            "SELECT 1 FROM cache_meta "
            "WHERE key='conversation_search_split_pending'"
        ).fetchone() is not None
    except sqlite3.OperationalError:
        # No cache_meta table (a bare / path-less connection) -> no pending
        # flag to honor; the legacy-aux probe below still guards the topology.
        split_pending = False
    if split_pending or _legacy_aux_fts_present(conn):
        # Guard 3: the search split is not yet consumed — the legacy aux FTS
        # may still reference search_aux, so DROP COLUMN would fail. DEFER; the
        # next sync's _consume_search_split clears the flag + drops the aux
        # shape, and a later open then drops the column.
        raise MigrationGateNotMet(
            "search_aux drop deferred: migration-010 search split not yet "
            "consumed (pending flag or legacy conversation_fts_aux still present)"
        )
    conn.execute(
        "ALTER TABLE conversation_messages DROP COLUMN search_aux")
    conn.commit()


@cache_migration("017_arm_nested_agent_reingest")
def _017_arm_nested_agent_reingest(conn: sqlite3.Connection) -> None:
    """Flag-only re-ingest so existing nested-subagent (grandchild) results whose
    ``agentId:`` trailer landed PAST the 16 KB ``_TOOL_RESULT_CAP`` clip re-link
    on existing history (#217 S1 / U6). The parser now stamps a structured
    ``block["agent_id"]`` (+ usage) at INGEST — over the FULL raw, before the clip
    — so the offset-0 re-parse re-derives the link with zero new consumption code
    (the kernel's existing ``b.pop("agent_id")`` consumer picks it up). Until
    consumed, old rows fall back to the read-time regex over the (clipped) text —
    today's behavior, no worse.

    Sets the DISTINCT ``conversation_reingest_nested_agent_pending`` flag (NOT the
    shared ``conversation_reingest_pending``, which also gates migration 005's
    read-time human-fallback in the query kernel — re-arming it could misclassify
    a genuine human prompt during the pre-reingest window). Consumption rides the
    #179 RESUMABLE per-file reingest (``_resumable_reingest_conversation_messages``)
    — the flag is wired into ``_TARGETED_DECLINE_FLAGS`` + ``_REINGEST_FLAG_KEYS``
    + the resumable-reingest flag SELECT + the two cleanup DELETE lists in
    ``_cctally_cache.py`` (all five sites; missing one either never triggers or
    re-arms forever). Central stamp via the dispatcher (#140); a fresh install
    stamps it WITHOUT running (empty table -> the flag, if ever set, is a harmless
    no-op). Mirrors 014/009/007."""
    _set_cache_meta(conn, "conversation_reingest_nested_agent_pending", "1")
    conn.commit()


@cache_migration("018_create_conversation_title_fts")
def _018_create_conversation_title_fts(conn: sqlite3.Connection) -> None:
    """#217 S2 / E7: arm the external-content title FTS over
    ``conversation_ai_titles`` so AI titles are findable via ``kind=title``.

    Flag-only arm. The ``conversation_title_fts`` virtual table + its
    conv_title_fts_ai/ad/au sync triggers are created by ``_apply_cache_schema``
    (runs on every open, fresh + existing installs) inside the SAME FTS5-available
    envelope as the message FTS (P1-6) — so on a no-FTS5 build the table+triggers
    are simply absent and a title upsert never rolls back the ingest. This handler
    does NO DDL (mirrors 012's flag-only pattern): it just arms the DISTINCT
    ``conversation_title_fts_backfill_pending`` flag (the "distinct reingest flag
    per enrichment" rule — its own flag, never the shared one) so the next
    flock-held full sync runs ``_consume_title_fts`` (an FTS5 ``'rebuild'``, P1-7)
    to populate the index from existing history.

    The flag joins ``_TARGETED_DECLINE_FLAGS`` ONLY — NEVER ``_REINGEST_FLAG_KEYS``
    (P1-2): that set means "run ``_resumable_reingest_conversation_messages``" (a
    full message delete/reinsert + rowid churn) which a title-FTS backfill must
    not trigger; the title index is external-content over conversation_ai_titles
    and a ``'rebuild'`` repopulates it without touching conversation_messages.

    No data work here -> the dispatcher's central stamp (#140) marks a complete
    handler; a fresh install stamps WITHOUT a populated history (its incremental
    walk fills the title FTS via the AI trigger as titles ingest, and the consumed
    backfill 'rebuild' no-ops). Mirrors 012's flag-only arm."""
    _set_cache_meta(conn, "conversation_title_fts_backfill_pending", "1")
    conn.commit()


@cache_migration("019_create_conversation_file_touches")
def _019_create_conversation_file_touches(conn: sqlite3.Connection) -> None:
    """#217 S2 / I-3: arm the file-path search axis backfill so existing history's
    WRITE-class file touches (Edit/MultiEdit/Write/NotebookEdit) are searchable via
    ``kind=files``.

    Flag-only arm (mirrors 018's pattern). The ``conversation_file_touches`` table
    + its ``COLLATE NOCASE`` path index (``idx_file_touches_path``) are created by
    ``_apply_cache_schema`` (runs on every open, fresh + existing installs) — and
    CRITICALLY before the FTS5 ``legacy_present`` early-return, since the table is
    plain and has NO dependency on the FTS shape (so a legacy-shape upgrade still
    gets it). The NOCASE collation is what lets the kind=files PREFIX search ride
    the index (default LIKE is case-insensitive; a BINARY index can't serve it —
    review Important #1). This handler does NO DDL: it just
    arms the DISTINCT ``conversation_reingest_file_touches_pending`` flag (the
    "distinct reingest flag per enrichment" rule — its own flag, never the shared
    one) so the next flock-held full sync runs ``_consume_file_touches`` to derive
    touches from existing ``blocks_json`` history.

    The flag joins ``_TARGETED_DECLINE_FLAGS`` ONLY — NEVER ``_REINGEST_FLAG_KEYS``
    (P1-2): that set means "run ``_resumable_reingest_conversation_messages``" (a
    full message delete/reinsert + rowid churn) which a file-touches backfill must
    not trigger; the backfill derives from the already-present ``blocks_json`` and
    INSERT-OR-IGNOREs into a separate table without touching conversation_messages.

    No data work here -> the dispatcher's central stamp (#140) marks a complete
    handler; a fresh install stamps WITHOUT a populated history (its incremental
    walk fills touches per ingested tick via _fill_file_touches, and the consumed
    backfill no-ops). Mirrors 018's flag-only arm."""
    _set_cache_meta(conn, "conversation_reingest_file_touches_pending", "1")
    conn.commit()


@cache_migration("020_session_entries_physical_unique")
def _020_session_entries_physical_unique(conn: sqlite3.Connection) -> None:
    """#279 S3 F3: dedup historical (source_path, line_offset) duplicates
    (keep-first-id) and ensure the physical-key UNIQUE backstop index exists.

    Duplicated physical keys are strictly ingest-bug artifacts (spec §4: correct
    offset bookkeeping cannot produce them) with no principled winner; MIN(id)
    keeps the first-ingested row, matching the pre-bug state. cache.db is
    re-derivable — `cache-sync --rebuild` remains the escape hatch. Mirrors
    cache 001's flock-then-BEGIN-IMMEDIATE pattern (#105): mutual exclusion with
    a mid-walk sync_cache, deferring via MigrationGateNotMet on contention.

    Fresh installs never run this handler: the dispatcher stamps it WITHOUT
    running (the fresh-install branch), and `_apply_cache_schema` already created
    the index on the fresh DB. NO self-stamp — the dispatcher central-stamps on a
    clean return (#140). Run-twice safe: the DELETE is idempotent (no dupes left
    on a second run) and the index create is `IF NOT EXISTS`; safe on an empty
    table.
    """
    lock_path = _cache_db_lock_path_for_conn(conn)
    lock_fh = None
    if lock_path is not None:
        lock_fh = open(lock_path, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fh.close()
            raise MigrationGateNotMet(
                "cache.db.lock held by a concurrent sync_cache; deferring "
                "cache 020 physical-unique dedup (#279 S3)"
            )
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM session_entries WHERE id NOT IN ("
                "SELECT MIN(id) FROM session_entries "
                "GROUP BY source_path, line_offset)")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_physical "
                "ON session_entries(source_path, line_offset)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()


@cache_migration("021_index_conversation_messages_cwd")
def _021_index_conversation_messages_cwd(conn: sqlite3.Connection) -> None:
    """#289: partial covering index on conversation_messages(cwd) to collapse the
    full-table `SELECT DISTINCT cwd` scan in build_anon_plan_for_db (~20s cold on
    a 5.5 GB cache) into an index-only search.

    Fresh installs never run this handler: the dispatcher stamps it WITHOUT
    running (the fresh-install branch), and `_apply_cache_schema` already created
    the index on the fresh DB. NO self-stamp — the dispatcher central-stamps on a
    clean return (#140). Run-twice safe: CREATE INDEX is IF NOT EXISTS. cache.db
    is re-derivable — `cache-sync --rebuild` is the escape hatch.

    No flock/BEGIN IMMEDIATE (unlike 020): a single CREATE INDEX IF NOT EXISTS is
    atomic and touches no rows, so it needs no mutual-exclusion scaffolding.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_messages_cwd "
        "ON conversation_messages(cwd) WHERE cwd IS NOT NULL AND cwd != ''"
    )


@cache_migration("022_index_conversation_messages_model")
def _022_index_conversation_messages_model(conn: sqlite3.Connection) -> None:
    """#301: partial covering index on conversation_messages(model, session_id) to
    collapse the full-table `SELECT DISTINCT ... model` walks in
    list_conversation_facets and _model_clause (~22s cold on a 5.5 GB cache) into
    index-only walks/seeks.

    Fresh installs never run this handler: the dispatcher stamps it WITHOUT running
    (the fresh-install branch), and `_apply_cache_schema` already created the index
    on the fresh DB. NO self-stamp — the dispatcher central-stamps on a clean return
    (#140). Run-twice safe: CREATE INDEX is IF NOT EXISTS. cache.db is re-derivable —
    `cache-sync --rebuild` is the escape hatch.

    No flock/BEGIN IMMEDIATE (like 021): a single CREATE INDEX IF NOT EXISTS performs
    no row mutations (it reads the table once to build the index but modifies no
    rows), so it needs no mutual-exclusion scaffolding.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_messages_model_session "
        "ON conversation_messages(model, session_id) "
        "WHERE model IS NOT NULL AND model != ''"
    )


@cache_migration("023_conversation_sessions_enrichment_columns")
def _023_conversation_sessions_enrichment_columns(conn: sqlite3.Connection) -> None:
    """Flag-only arm for the #302 browse-rail enrichment columns (git_branch,
    models_json, title). The columns themselves are added by _apply_cache_schema's
    CREATE TABLE + add_column_if_missing (the repo's column-addition rule, NOT an
    ALTER here); this migration only arms the SHARED
    conversation_sessions_backfill_pending flag so the next sync_cache full
    recompute fills the new (empty) columns via the augmented
    _fill_conversation_sessions_filter_columns. Central stamp via the dispatcher
    (#140); the handler does NOT self-stamp.

    A fresh install gets the columns from _apply_cache_schema's CREATE TABLE and
    stamps 023 WITHOUT running this handler; its empty rollup needs no backfill
    (the incremental DELETE+INSERT re-derive fills all columns in lockstep).
    Mirrors 013/015."""
    _set_cache_meta(conn, "conversation_sessions_backfill_pending", "1")
    conn.commit()


@cache_migration("024_codex_fused_ingest_rebuild")
def _024_codex_fused_ingest_rebuild(conn: sqlite3.Connection) -> None:
    """Clear stale Codex cache state for #294 S1's fused source reingest.

    Pre-S1 Codex rows lack provider-root identity, qualified conversation
    linkage, quota observations, and physical event payloads. Those facts are
    not recoverable from the cache, so this migration clears only the Codex
    accounting/file surface and partial S1 derived rows; the next normal Codex
    sync rederives them from rollout source. Claude tables and Claude quota
    snapshots remain untouched.

    The handler takes ``cache.db.codex.lock`` before ``BEGIN IMMEDIATE`` — the
    same fcntl-then-SQLite order as ``sync_codex_cache``. Contention raises
    ``MigrationGateNotMet`` before any DML, so both ordinary and eager cache
    dispatch defer without a partial clear. The DELETE-only transition is safe
    to retry after a crash between this commit and the dispatcher's separate
    marker stamp: rerunning against its own empty Codex state is a no-op.
    Never self-stamp; the dispatcher owns ``schema_migrations`` and
    ``user_version``.
    """
    lock_path = _cache_db_codex_lock_path_for_conn(conn)
    lock_fh = None
    if lock_path is not None:
        lock_fh = open(lock_path, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fh.close()
            raise MigrationGateNotMet(
                "cache.db.codex.lock held by a concurrent sync_codex_cache; "
                "deferring cache 024 fused-ingest rebuild (#294 S1)"
            )
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM codex_session_entries")
            conn.execute("DELETE FROM codex_session_files")
            conn.execute("DELETE FROM quota_window_snapshots WHERE source = 'codex'")
            conn.execute("DELETE FROM codex_conversation_threads")
            conn.execute("DELETE FROM codex_conversation_events")
            conn.execute("DELETE FROM codex_source_roots")
            # F3 (#313): clearing Codex quota state without invalidating the
            # quota-projection certificate would leave a stale-valid cert
            # (cache sequence unchanged) that lets the reconcile short-circuit
            # skip over now-deleted data. Delete it in the same transaction.
            conn.execute(
                "DELETE FROM cache_meta WHERE key='codex_quota_projection_certificate'"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()


@cache_migration("025_codex_conversation_normalization")
def _025_codex_conversation_normalization(conn: sqlite3.Connection) -> None:
    """Derive S6 normalized conversation state for caches whose Codex events were
    ingested before S6 (#294 S6, §3.5).

    Clears the three derived tables (codex_conversation_messages,
    codex_conversation_file_touches, codex_conversation_rollups) via the §3.4
    storm-free full-clear helper, then replays the pure normalization kernel over
    the stored ``codex_conversation_events`` per file in
    ``(source_path ASC, line_offset ASC)`` order — rebuilding session_meta /
    turn_context sticky state as it goes. The physical event log and Codex
    accounting/thread/quota rows are the replay SOURCE and are never touched.

    Takes ``cache.db.codex.lock`` with the same fcntl-then-``BEGIN IMMEDIATE``
    order as ``sync_codex_cache``, raising ``MigrationGateNotMet`` on contention
    (defer, never partially normalize) from both the ordinary and eager cache
    dispatch paths. Deterministic replay order + the non-AUTOINCREMENT ``id``
    rowid alias + the storm-free full-clear make a re-run (handler success, crash
    before the central stamp) byte-idempotent INCLUDING the FTS shadow tables.
    Never self-stamps; the dispatcher owns ``schema_migrations`` and
    ``user_version``.
    """
    lock_path = _cache_db_codex_lock_path_for_conn(conn)
    lock_fh = None
    if lock_path is not None:
        lock_fh = open(lock_path, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fh.close()
            raise MigrationGateNotMet(
                "cache.db.codex.lock held by a concurrent sync_codex_cache; "
                "deferring cache 025 conversation normalization (#294 S6)"
            )
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Full clear of only the DERIVED normalized tables (the event log
            # remains as the replay source), then re-derive from it.
            _codex_conversation_fts_full_clear(conn)
            import _cctally_cache
            _cctally_cache._replay_codex_normalization(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()


@cache_migration("026_codex_conversation_key_backfill")
def _026_codex_conversation_key_backfill(conn: sqlite3.Connection) -> None:
    """Arm a byte-zero Codex replay for missing conversation-key metadata (#312).

    Historical accounting rows predate the source-derived conversation-key
    enrichment.  The cache cannot truthfully recover those keys on its own, so
    clear exactly the existing Codex-derived families and let the next Codex
    sync replay retained rollout data from byte zero.  This shares the runtime
    clear helper with ``sync_codex_cache(rebuild=True)`` so new or future Codex
    families cannot drift from the migration's destructive scope.

    The Codex flock is acquired before ``BEGIN IMMEDIATE``.  Contention defers
    through ``MigrationGateNotMet`` before any DML; after a handler/data commit
    but before the dispatcher's central stamp, a rerun against the empty state
    is a byte-idempotent no-op.  The physical mutation sequence advances only
    when the shared clear actually changed persisted Codex state.  The handler
    never self-stamps.
    """
    lock_path = _cache_db_codex_lock_path_for_conn(conn)
    lock_fh = None
    if lock_path is not None:
        lock_fh = open(lock_path, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fh.close()
            raise MigrationGateNotMet(
                "cache.db.codex.lock held by a concurrent sync_codex_cache; "
                "deferring cache 026 conversation-key backfill (#312)"
            )
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            import _cctally_cache

            if _cctally_cache._clear_codex_derived_rows(conn):
                _cctally_cache._bump_codex_physical_mutation_seq(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()


@cache_migration("027_codex_fork_preamble_rebuild")
def _027_codex_fork_preamble_rebuild(conn: sqlite3.Connection) -> None:
    """Replay Codex rollouts after suppressing copied fork-preamble accounting.

    Forked/subagent JSONL can retain parent ``token_count`` records before the
    child's first model-bearing ``turn_context``.  Older ingest projected those
    copied records into ``codex_session_entries`` as ``model='unknown'``, double
    counting usage already present in the parent rollout.  The source records
    remain authoritative, so clear all re-derivable Codex families and let the
    corrected fused parser replay them from byte zero.

    The Codex flock precedes ``BEGIN IMMEDIATE``.  Contention defers before DML;
    a markerless retry against already-cleared state is a no-op.  The shared
    clear helper keeps this migration aligned with runtime rebuild scope and
    advances the physical mutation sequence only when state actually changed.
    The dispatcher owns the migration marker.
    """
    lock_path = _cache_db_codex_lock_path_for_conn(conn)
    lock_fh = None
    if lock_path is not None:
        lock_fh = open(lock_path, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fh.close()
            raise MigrationGateNotMet(
                "cache.db.codex.lock held by a concurrent sync_codex_cache; "
                "deferring cache 027 fork-preamble rebuild"
            )
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            import _cctally_cache

            if _cctally_cache._clear_codex_derived_rows(conn):
                _cctally_cache._bump_codex_physical_mutation_seq(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()


@cache_migration("028_split_conversation_store")
def _028_split_conversation_store(conn: sqlite3.Connection) -> None:
    """Move the re-derivable transcript corpus out of cache.db (#320).

    The upgrade deliberately does not copy gigabytes of prose/events. It creates
    the independent current-schema store, arms provider-local byte-zero replay,
    then drops the legacy transcript tables from the hot cache. Compact Codex
    thread metadata stays in cache.db because accounting/project attribution
    joins it directly.
    """
    core = _cctally_core
    lock_paths = (
        core.CACHE_LOCK_MAINTENANCE_PATH,
        core.CACHE_LOCK_PATH,
        core.CACHE_LOCK_CODEX_PATH,
        core.CONVERSATIONS_LOCK_MAINTENANCE_PATH,
        core.CONVERSATIONS_LOCK_PATH,
        core.CONVERSATIONS_LOCK_CODEX_PATH,
    )
    held = []
    try:
        core.APP_DIR.mkdir(parents=True, exist_ok=True)
        for path in lock_paths:
            fh = open(path, "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fh.close()
                raise MigrationGateNotMet(
                    "cache/conversation sync lock held; deferring cache 028 split"
                )
            held.append(fh)

        conv = sqlite3.connect(core.CONVERSATIONS_DB_PATH)
        try:
            conv.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conv.execute("PRAGMA journal_mode=WAL")
            _apply_conversations_schema(conv)
            _set_cache_meta(conv, "conversation_rebuild_claude_pending", "1")
            _set_cache_meta(conv, "conversation_rebuild_codex_pending", "1")
            conv.commit()
        finally:
            conv.close()

        conn.execute("BEGIN IMMEDIATE")
        try:
            # Preserve exact model-scoped quota-pool identity before discarding
            # the legacy physical event corpus. The correlated lookup is the
            # same nearest-prior context rule the quota reader used pre-split.
            if conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='codex_conversation_events'"
            ).fetchone() is not None:
                conn.execute(
                    "UPDATE quota_window_snapshots AS q SET observed_model=("
                    " SELECT json_extract(e.payload_json, '$.payload.model')"
                    " FROM codex_conversation_events AS e"
                    " WHERE e.source_path=q.source_path"
                    "   AND e.line_offset<=q.line_offset"
                    "   AND e.record_type IN ('turn_context','session_meta')"
                    "   AND json_valid(e.payload_json)"
                    "   AND json_type(e.payload_json, '$.payload.model')='text'"
                    " ORDER BY e.line_offset DESC LIMIT 1)"
                    " WHERE q.source='codex' AND q.observed_model IS NULL"
                )
            for drop in (
                "DROP TRIGGER IF EXISTS conv_fts_ai",
                "DROP TRIGGER IF EXISTS conv_fts_ad",
                "DROP TRIGGER IF EXISTS conv_fts_au",
                "DROP TRIGGER IF EXISTS conv_fts_aux_ai",
                "DROP TRIGGER IF EXISTS conv_fts_aux_ad",
                "DROP TRIGGER IF EXISTS conv_fts_aux_au",
                "DROP TRIGGER IF EXISTS conv_title_fts_ai",
                "DROP TRIGGER IF EXISTS conv_title_fts_ad",
                "DROP TRIGGER IF EXISTS conv_title_fts_au",
                "DROP TRIGGER IF EXISTS codex_conv_fts_ai",
                "DROP TRIGGER IF EXISTS codex_conv_fts_ad",
                "DROP TRIGGER IF EXISTS codex_conv_fts_au",
                "DROP TABLE IF EXISTS conversation_fts_aux",
                "DROP TABLE IF EXISTS conversation_fts",
                "DROP TABLE IF EXISTS conversation_title_fts",
                "DROP TABLE IF EXISTS codex_conversation_fts",
                "DROP TABLE IF EXISTS conversation_file_touches",
                "DROP TABLE IF EXISTS conversation_sessions",
                "DROP TABLE IF EXISTS conversation_ai_titles",
                "DROP TABLE IF EXISTS conversation_messages",
                "DROP TABLE IF EXISTS codex_conversation_file_touches",
                "DROP TABLE IF EXISTS codex_conversation_rollups",
                "DROP TABLE IF EXISTS codex_conversation_messages",
                "DROP TABLE IF EXISTS codex_conversation_events",
            ):
                conn.execute(drop)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        try:
            if conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:
                # Python 3.11/Linux may defer this zero-column PRAGMA until the
                # cursor is exhausted; fetchall drives it to completion.
                conn.execute("PRAGMA incremental_vacuum").fetchall()
        except sqlite3.DatabaseError:
            pass
    finally:
        for fh in reversed(held):
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()


# === Region 7d: Stats migration 008_recompute_weekly_cost_snapshots_dedup_fix ===

@stats_migration("008_recompute_weekly_cost_snapshots_dedup_fix")
def _008_recompute_weekly_cost_snapshots_dedup_fix(
    conn: sqlite3.Connection,
) -> None:
    """Recompute ``weekly_cost_snapshots.cost_usd`` from the now-corrected
    ``session_entries``. Gated on cache migration 001 having applied AND
    ``sync_cache`` having repopulated ``session_entries`` since.

    Scope: only rows with ``mode='auto'`` AND ``project IS NULL``.
    ``mode='display'`` rows preserve a user-supplied cost from a prior
    ``calculate`` run (``docs/commands/sync-week.md``); per-project
    snapshots have aggregation boundaries this fix doesn't know about.
    Both are left untouched.

    Legacy rows with ``range_start_iso IS NULL`` or
    ``range_end_iso IS NULL`` are skipped (their pre-fix value stays);
    CHANGELOG calls this out as the one exception to "post-fix
    ``report`` matches ``weekly``."

    Cross-DB plumbing
    -----------------
    Opens ``cache.db`` read-only via the ``file:?mode=ro`` URI form. We
    do NOT ``ATTACH DATABASE`` — the existing transactional isolation
    (write side on ``conn`` inside ``BEGIN``/``COMMIT``, read side on a
    separate read-only connection) is the cleanest design and matches
    how Task 3's gate helper already wires it.

    Timestamp comparison
    --------------------
    ``range_start_iso`` and ``range_end_iso`` originate from
    ``insert_cost_snapshot`` → ``parse_iso_datetime(...).isoformat()``,
    which keeps the offset of whatever the caller passed (typically
    ``+00:00`` from ``week_start_at`` canonicalization, but
    ``parse_iso_datetime`` returns ``parsed.astimezone()`` so naive
    inputs end up host-local). ``session_entries.timestamp_utc`` is
    written via ``entry.timestamp.astimezone(dt.timezone.utc).isoformat()``
    in ``sync_cache`` — canonical UTC ISO with ``+00:00`` offset.
    Both sides are normalized at the Python boundary through
    ``_canonical_utc_iso_for_index`` so plain lex compare against
    ``timestamp_utc`` hits ``idx_entries_timestamp``. Mirrors the
    canonicalization that ``iter_entries`` /
    ``get_claude_session_entries`` apply to user-facing queries in
    ``bin/_cctally_cache.py``.

    Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3.
    """
    # Banner is gated on "we actually have rows to recompute" via the
    # shared ``_recompute_banner_should_emit`` helper (composed below).
    # The all-empty no-op case (most upgrade-time fresh-install
    # topologies, and most goldens with no snapshot rows) skips the
    # banner so we don't pollute thousands of test goldens /
    # per-command stderr with a benign one-line announcement. Heavy
    # users with 52+ snapshots still see it once.
    #
    # ``_open_cache_ro_with_gate_defer`` (shared with 009/010) eagerly
    # applies cache.db's dispatcher (V4: ensures cache migration 001's
    # marker is in place even on stats-only invocations) then opens
    # cache.db RO with the G4/G5 transient-defer translation baked in.
    cache_ro = _open_cache_ro_with_gate_defer()
    try:
        # Resolve projects dirs via the shared helper (mirrors 009/010).
        # Empty list returned only when NO projects/ dir resolves under
        # any env-configured or default root; the resolver classifies
        # that as ``disk_state="absent"`` and decides accordingly.
        projects_dirs = _resolve_projects_dirs_for_gate()

        # F3 scope: only rows we have authority over (see docstring).
        snapshot_rows = conn.execute(
            "SELECT id, range_start_iso, range_end_iso "
            "FROM weekly_cost_snapshots "
            "WHERE mode = 'auto' AND project IS NULL"
        ).fetchall()

        # The gate is now a pure state machine (cctally-dev#93): the old
        # inline G3 fail-closed block and the defensive default-dir
        # fallback are gone. An empty ``projects_dirs`` is the legitimate
        # ``disk_state="absent"`` topology — the resolver DEFERS (row 7)
        # when ``data_present`` and PROCEEDS (row 5, body no-ops) when
        # there's nothing to protect, with the operator-guidance reason
        # text baked into the resolver. No body-level recompute guard
        # (spec D7): the recompute below computes every in-range value
        # from surviving ``session_entries``, INCLUDING to $0 — the
        # wholesale-zeroing protection lives entirely in the gate.
        _gate_001_post_ingest_completed(
            cache_ro, projects_dirs, data_present=bool(snapshot_rows),
        )

        # Banner gated on "we actually have eligible rows to recompute"
        # AND "active subcommand is not in _BANNER_SUPPRESSED_COMMANDS"
        # — composed via the shared ``_recompute_banner_should_emit``
        # helper that 001/008/009/010 all funnel through. Empty-
        # snapshot topologies (most goldens, fresh-install upgrades)
        # plus hot/scripted paths (`blocks`, `record-usage`, etc.)
        # stay quiet. Heavy users invoking interactive non-report
        # commands (52+ weekly snapshots) still see it once.
        if _recompute_banner_should_emit(data_present=bool(snapshot_rows)):
            eprint(
                "[cctally] Recomputing weekly_cost_snapshots from "
                "corrected session_entries (one-time; may take 30-60s "
                "on heavy histories)..."
            )

        conn.execute("BEGIN")
        try:
            for snap_id, range_start_iso, range_end_iso in snapshot_rows:
                if range_start_iso is None or range_end_iso is None:
                    # Legacy row written before range_*_iso columns
                    # existed. Skip (not crash) — leaves the snapshot at
                    # its pre-fix value; CHANGELOG calls this out.
                    continue
                # V1 — closed interval ``<=`` matches the production
                # writer (``iter_entries`` in bin/_cctally_cache.py: lex
                # ``timestamp_utc >= ? AND timestamp_utc <= ?``). The
                # migration's prior half-open ``<`` end silently excluded
                # any ``session_entries`` row whose ``timestamp_utc``
                # equalled the snapshot's ``range_end_iso`` boundary —
                # an edge with positive probability on subscription-week
                # boundaries where Claude Code's status-line tick can
                # land an entry exactly on the reset instant. After this
                # fix, the migration's recompute is byte-for-byte
                # symmetric with every subsequent ``sync-week`` row that
                # gets written through ``compute_week_cost`` →
                # ``iter_entries`` — so R-DEDUP2 in
                # ``bin/cctally-reconcile-test`` no longer needs to
                # caveat the divergence.
                # Canonicalize range bounds to the same UTC ISO shape
                # ``session_entries.timestamp_utc`` carries on disk so
                # lex compare hits ``idx_entries_timestamp`` instead of
                # SCANning. See ``_canonical_utc_iso_for_index`` for the
                # EXPLAIN QUERY PLAN rationale; mirrors
                # ``iter_entries`` in bin/_cctally_cache.py.
                entries = cache_ro.execute(
                    "SELECT model, input_tokens, output_tokens, "
                    "cache_create_tokens, cache_read_tokens, "
                    "usage_extra_json, cost_usd_raw "
                    "FROM session_entries "
                    "WHERE timestamp_utc >= ? AND timestamp_utc <= ?",
                    (
                        _canonical_utc_iso_for_index(range_start_iso),
                        _canonical_utc_iso_for_index(range_end_iso),
                    ),
                ).fetchall()
                total = 0.0
                for model, i, o, cc, cr, extras_json, raw in entries:
                    usage = {
                        "input_tokens": i,
                        "output_tokens": o,
                        "cache_creation_input_tokens": cc,
                        "cache_read_input_tokens": cr,
                    }
                    # #181: usage_extra_json is cost-irrelevant (cost is
                    # token-only); parsed here only for pre-008 rows that may
                    # still carry the legacy blob — NOT a speed reader, so the
                    # write-side NULL going forward is safe.
                    if extras_json:
                        usage.update(json.loads(extras_json))
                    total += _calculate_entry_cost(
                        model, usage, mode="auto", cost_usd=raw,
                    )
                conn.execute(
                    "UPDATE weekly_cost_snapshots "
                    "SET cost_usd = ? WHERE id = ?",
                    (total, snap_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        cache_ro.close()


# === Region 7e: Shared cross-DB gate setup for 008/009/010 ==================


def _open_cache_ro_with_gate_defer() -> sqlite3.Connection:
    """Shared bootstrap for stats migrations 008/009/010 that recompute
    from cache.db's ``session_entries``.

    Eagerly applies cache.db's dispatcher (so cache 001's marker is in
    place even on stats-only invocations), then opens cache.db read-only
    for the gate check. Either step's failure modes translate to
    ``MigrationGateNotMet`` so the dispatcher's defer machinery handles
    them cleanly (no migration-error banner). Mirrors the V4 + G4/G5
    fixes baked into 008's body.

    Returns the read-only cache.db connection. Caller is responsible for
    ``.close()``.
    """
    try:
        _eagerly_apply_cache_migrations()
    except sqlite3.OperationalError as exc:
        if _is_transient_sqlite_error(exc):
            raise MigrationGateNotMet(
                "cache.db not yet initialized or transiently locked; "
                "run any JSONL-reading command (e.g. `cctally weekly`) "
                "once and retry."
            ) from None
        raise

    cache_db_path = _cctally_core.CACHE_DB_PATH
    try:
        cache_ro = sqlite3.connect(
            f"file:{cache_db_path}?mode=ro", uri=True,
        )
    except sqlite3.OperationalError as exc:
        if _is_transient_sqlite_error(exc):
            raise MigrationGateNotMet(
                "cache.db not yet initialized or transiently locked; "
                "run any JSONL-reading command (e.g. `cctally weekly`) "
                "once and retry."
            ) from None
        raise

    # Pin a SINGLE consistent cache.db snapshot for the whole recompute
    # (cctally-dev#93 review). cache.db is WAL (bin/_cctally_cache.py:
    # `PRAGMA journal_mode=WAL`), and Python's sqlite3 only auto-BEGINs
    # before DML — every read on this RO connection would otherwise run
    # in autocommit, so each `cache_ro.execute(SELECT …)` could observe a
    # NEWER `session_entries` snapshot if `record-usage`/`hook-tick`/
    # `cache-sync` committed between loop iterations. That lets one
    # migration run recompute different rows from different cache states
    # and still stamp its schema marker (an internally-inconsistent
    # recompute). An explicit deferred BEGIN starts a read transaction
    # whose snapshot is locked at the first read (the gate's
    # schema_migrations probe) and held until COMMIT/ROLLBACK; in WAL the
    # reader never blocks the writer, so concurrent ingests still
    # proceed — they just land in a newer WAL frame this transaction
    # won't see. The caller's `finally: cache_ro.close()` ends the
    # transaction. The recompute writes target stats.db (`conn`), NOT
    # this connection, so a still-open read txn here never blocks them.
    try:
        cache_ro.execute("BEGIN")
    except sqlite3.OperationalError as exc:
        cache_ro.close()
        if _is_transient_sqlite_error(exc):
            raise MigrationGateNotMet(
                "cache.db not yet initialized or transiently locked; "
                "run any JSONL-reading command (e.g. `cctally weekly`) "
                "once and retry."
            ) from None
        raise
    return cache_ro


def _resolve_projects_dirs_for_gate() -> list[pathlib.Path]:
    """Shared resolver for stats migrations 008/009/010 gate checks.

    Returns the list of Claude projects/ dirs to feed to
    ``_gate_001_post_ingest_completed``. Mirrors 008's resolution chain:
    env-aware resolver first, defensive fallback to
    ``CLAUDE_PROJECTS_DIR`` when the resolver returns ``[]`` but the
    default exists on disk (covers test-time monkeypatch overrides).

    Empty list returned only when NO projects/ dir resolves under any
    env-configured or default root. An empty list is the legitimate
    ``disk_state="absent"`` topology — callers no longer fail-closed
    inline (Task 5 removed every caller's G3 block); they unconditionally
    delegate the empty-list decision to the resolver, which DEFERs at
    row 7 when historical rows remain and PROCEEDs at row 5 otherwise.
    """
    projects_dirs = _cctally_core._resolve_claude_projects_dirs()
    if not projects_dirs and _cctally_core.CLAUDE_PROJECTS_DIR.is_dir():
        projects_dirs = [_cctally_core.CLAUDE_PROJECTS_DIR]
    return projects_dirs


def _canonical_utc_iso_for_index(value: str) -> str:
    """Normalize an ISO-8601 timestamp string to the canonical UTC form
    that ``session_entries.timestamp_utc`` stores on disk, so a lex
    comparison against the indexed column hits ``idx_entries_timestamp``
    instead of degrading to a full SCAN.

    Why this exists
    ---------------
    ``session_entries.timestamp_utc`` is always written via
    ``entry.timestamp.astimezone(dt.timezone.utc).isoformat()`` in
    ``sync_cache`` (bin/_cctally_cache.py — the only writer). On disk
    every row therefore looks like ``2026-05-01T12:34:56.789012+00:00``.

    Migration 008/009/010's range bounds arrive in mixed shapes:

      * ``weekly_cost_snapshots.range_start_iso`` /
        ``range_end_iso`` — host-local-offset bytes if the writer's
        ``parse_iso_datetime`` saw a naive input (returns
        ``parsed.astimezone()``) or ``+00:00`` when fed canonical
        week-start instants.
      * ``five_hour_blocks.block_start_at`` — host-local-offset
        bytes (same ``parse_iso_datetime`` chokepoint).
      * ``five_hour_blocks.last_observed_at_utc`` — always
        ``Z``-suffixed (``now_utc_iso()``).
      * ``percent_milestones.week_start_at`` /
        ``captured_at_utc`` — same mix.

    The prior implementation wrapped both sides of the WHERE in
    ``unixepoch(...)`` to absorb the offset mix. That made the
    comparison correct but defeated ``idx_entries_timestamp`` —
    ``EXPLAIN QUERY PLAN`` rendered ``SCAN session_entries`` on every
    range slice. On a heavy user's cache.db (10k+ rows) that turned
    the one-time recompute from "30-60s" into multiple minutes.

    By canonicalizing at the Python boundary into the same shape the
    writer uses, both sides of ``WHERE timestamp_utc >= ? AND
    timestamp_utc <= ?`` carry the same offset notation and lex
    compare is correct. Index hit:
    ``SEARCH session_entries USING INDEX idx_entries_timestamp
    (timestamp_utc>? AND timestamp_utc<?)``.

    Matches the canonicalization pattern in
    ``bin/_cctally_cache.py``'s ``iter_entries`` /
    ``get_claude_session_entries`` (the production read paths).
    """
    return parse_iso_datetime(
        value, "migration-range-bound",
    ).astimezone(dt.timezone.utc).isoformat()


# === Region 7f: Stats migration 009_recompute_five_hour_blocks_dedup_fix ====

@stats_migration("009_recompute_five_hour_blocks_dedup_fix")
def _009_recompute_five_hour_blocks_dedup_fix(
    conn: sqlite3.Connection,
) -> None:
    """Recompute ``five_hour_blocks.total_*`` + rollup-children
    (``five_hour_block_models`` / ``five_hour_block_projects``) from the
    now-corrected ``session_entries``. Gated on cache migration 001
    having applied AND ``sync_cache`` having re-walked the on-disk JSONL
    since (the ``cache_meta`` ``claude_ingest_walk_complete`` marker is
    present) — the shared gate (``_gate_001_post_ingest_completed`` →
    ``resolve_upgrade_gate``), same as 008.

    Scope (B1)
    ----------
    The 5h block writer (``maybe_update_five_hour_block``) only recomputes
    totals for the CURRENTLY ACTIVE block — closed historical blocks
    keep their pre-dedup totals forever. ``five_hour_block_models`` /
    ``five_hour_block_projects`` are recompute-every-tick on the active
    block too. Without this migration, every historical 5h block + its
    rollup children stays at the inflated pre-dedup numbers.

    This migration walks EVERY row in ``five_hour_blocks`` (active and
    closed), recomputes ``total_*`` from the corrected
    ``session_entries`` over ``[block_start_at, last_observed_at_utc]``,
    and replace-alls the per-(window, model) and per-(window, project)
    rollup children. Mirrors the live writer's algorithm in
    ``maybe_update_five_hour_block`` byte-for-byte — same closed
    interval, same ``unixepoch()`` cross-offset normalization, same
    ``LEFT JOIN session_files`` for project attribution, same
    ``project_path or '(unknown)'`` sentinel.

    Timestamp comparison
    --------------------
    ``block_start_at`` is stored with the host's display offset
    (``parse_iso_datetime`` returns ``parsed.astimezone()``;
    ``+03:00`` on a non-UTC host); ``last_observed_at_utc`` is
    ``Z``-suffixed (``now_utc_iso()``); ``session_entries.timestamp_utc``
    is canonical UTC ISO (``+00:00``) on disk. Both range bounds
    normalize through ``_canonical_utc_iso_for_index`` at the Python
    boundary so plain lex compare against ``timestamp_utc`` hits
    ``idx_entries_timestamp`` — same shape as 008/010 and the
    user-facing read paths in ``bin/_cctally_cache.py``.

    Closed interval (V1)
    --------------------
    ``<=`` matches the live writer's ``get_claude_session_entries``
    predicate (``timestamp >= ? AND timestamp <= ?``). A pre-fix
    half-open ``<`` would silently exclude any session_entries row
    whose ``timestamp_utc`` exactly equalled a block's
    ``last_observed_at_utc`` — an edge with positive probability since
    ``last_observed_at_utc`` IS the timestamp of some session-line tick.

    Banner
    ------
    Gated on ``five_hour_blocks`` non-emptiness so test goldens and
    fresh-install upgrades stay quiet (mirrors 008's ``snapshot_rows``
    gate). Heavy users with dozens of historical blocks still see it
    once.

    Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3 (B1).
    """
    cache_ro = _open_cache_ro_with_gate_defer()
    try:
        projects_dirs = _resolve_projects_dirs_for_gate()

        block_rows = conn.execute(
            "SELECT id, five_hour_window_key, block_start_at, "
            "last_observed_at_utc "
            "FROM five_hour_blocks"
        ).fetchall()

        # Pure state-machine gate (cctally-dev#93): the inline G3
        # fail-closed block and the default-dir fallback are gone; an
        # empty ``projects_dirs`` is the ``disk_state="absent"`` topology
        # the resolver handles (row 7 DEFER when data_present, row 5
        # PROCEED otherwise). No body-level recompute guard (spec D7) —
        # every in-range block recomputes from surviving
        # ``session_entries``, including to $0.
        _gate_001_post_ingest_completed(
            cache_ro, projects_dirs, data_present=bool(block_rows),
        )

        # SW5-style banner gating via the shared
        # ``_recompute_banner_should_emit`` helper: only print when
        # block_rows is non-empty AND the active subcommand is not in
        # ``_BANNER_SUPPRESSED_COMMANDS`` (notably ``blocks``, whose
        # stdout-formatted table would otherwise get prefixed by a
        # stderr announcement — surfaced by floor-band-trap fixture).
        if _recompute_banner_should_emit(data_present=bool(block_rows)):
            eprint(
                "[cctally] Recomputing closed 5h block totals after "
                "dedup fix (one-time; may take 30-60s on heavy "
                "histories)..."
            )

        conn.execute("BEGIN")
        try:
            for (
                block_id, window_key, block_start_at,
                last_observed_at_utc,
            ) in block_rows:
                # Walk session_entries over [block_start, last_observed]
                # joined to session_files for project_path attribution.
                # NULL session_files.project_path collapses to
                # '(unknown)' at the bucket layer — same sentinel as the
                # live writer (_compute_block_totals at
                # bin/_cctally_record.py).
                # Canonicalize range bounds to the same UTC ISO shape
                # ``session_entries.timestamp_utc`` carries on disk so
                # lex compare hits ``idx_entries_timestamp`` instead of
                # SCANning. See ``_canonical_utc_iso_for_index`` for the
                # EXPLAIN QUERY PLAN rationale; mirrors
                # ``get_claude_session_entries`` in
                # bin/_cctally_cache.py.
                entries = cache_ro.execute(
                    "SELECT se.model, se.input_tokens, se.output_tokens, "
                    "       se.cache_create_tokens, se.cache_read_tokens, "
                    "       se.usage_extra_json, se.cost_usd_raw, "
                    "       sf.project_path "
                    "FROM session_entries se "
                    "LEFT JOIN session_files sf "
                    "  ON sf.path = se.source_path "
                    "WHERE se.timestamp_utc >= ? "
                    "  AND se.timestamp_utc <= ?",
                    (
                        _canonical_utc_iso_for_index(block_start_at),
                        _canonical_utc_iso_for_index(
                            last_observed_at_utc
                        ),
                    ),
                ).fetchall()

                total_in = 0
                total_out = 0
                total_cc = 0
                total_cr = 0
                total_cost = 0.0
                by_model: dict[str, dict[str, Any]] = {}
                by_project: dict[str, dict[str, Any]] = {}
                for (
                    model, in_t, out_t, cc_t, cr_t,
                    extras_json, raw_cost, project_path,
                ) in entries:
                    usage = {
                        "input_tokens": in_t,
                        "output_tokens": out_t,
                        "cache_creation_input_tokens": cc_t,
                        "cache_read_input_tokens": cr_t,
                    }
                    # #181: usage_extra_json is cost-irrelevant (cost is
                    # token-only); parsed here only for pre-008 rows that may
                    # still carry the legacy blob — NOT a speed reader, so the
                    # write-side NULL going forward is safe.
                    if extras_json:
                        usage.update(json.loads(extras_json))
                    cost = _calculate_entry_cost(
                        model, usage, mode="auto", cost_usd=raw_cost,
                    )
                    total_in += int(in_t or 0)
                    total_out += int(out_t or 0)
                    total_cc += int(cc_t or 0)
                    total_cr += int(cr_t or 0)
                    total_cost += cost

                    proj_key = project_path or "(unknown)"
                    for bucket_key, bucket_dict in (
                        (model, by_model),
                        (proj_key, by_project),
                    ):
                        b = bucket_dict.setdefault(
                            bucket_key,
                            {
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "cache_create_tokens": 0,
                                "cache_read_tokens": 0,
                                "cost_usd": 0.0,
                                "entry_count": 0,
                            },
                        )
                        b["input_tokens"] += int(in_t or 0)
                        b["output_tokens"] += int(out_t or 0)
                        b["cache_create_tokens"] += int(cc_t or 0)
                        b["cache_read_tokens"] += int(cr_t or 0)
                        b["cost_usd"] += cost
                        b["entry_count"] += 1

                conn.execute(
                    "UPDATE five_hour_blocks "
                    "SET total_input_tokens = ?, "
                    "    total_output_tokens = ?, "
                    "    total_cache_create_tokens = ?, "
                    "    total_cache_read_tokens = ?, "
                    "    total_cost_usd = ? "
                    "WHERE id = ?",
                    (
                        total_in, total_out, total_cc, total_cr,
                        total_cost, block_id,
                    ),
                )

                # Replace-all per-(window, model) and per-(window,
                # project) rollup-children. Same pattern as the live
                # writer (DELETE WHERE five_hour_window_key = ? +
                # bulk INSERT). DELETE keyed on window_key (NOT
                # block_id) so the replace-all sweeps any orphans from
                # earlier parent rebuilds.
                conn.execute(
                    "DELETE FROM five_hour_block_models "
                    "WHERE five_hour_window_key = ?",
                    (int(window_key),),
                )
                if by_model:
                    conn.executemany(
                        "INSERT INTO five_hour_block_models "
                        "(block_id, five_hour_window_key, model, "
                        " input_tokens, output_tokens, "
                        " cache_create_tokens, cache_read_tokens, "
                        " cost_usd, entry_count) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                int(block_id),
                                int(window_key),
                                model,
                                b["input_tokens"],
                                b["output_tokens"],
                                b["cache_create_tokens"],
                                b["cache_read_tokens"],
                                b["cost_usd"],
                                b["entry_count"],
                            )
                            for model, b in by_model.items()
                        ],
                    )

                conn.execute(
                    "DELETE FROM five_hour_block_projects "
                    "WHERE five_hour_window_key = ?",
                    (int(window_key),),
                )
                if by_project:
                    conn.executemany(
                        "INSERT INTO five_hour_block_projects "
                        "(block_id, five_hour_window_key, "
                        " project_path, "
                        " input_tokens, output_tokens, "
                        " cache_create_tokens, cache_read_tokens, "
                        " cost_usd, entry_count) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                int(block_id),
                                int(window_key),
                                proj,
                                b["input_tokens"],
                                b["output_tokens"],
                                b["cache_create_tokens"],
                                b["cache_read_tokens"],
                                b["cost_usd"],
                                b["entry_count"],
                            )
                            for proj, b in by_project.items()
                        ],
                    )

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        cache_ro.close()


# === Region 7g: Stats migration 010_recompute_percent_milestones_dedup_fix ==

@stats_migration("010_recompute_percent_milestones_dedup_fix")
def _010_recompute_percent_milestones_dedup_fix(
    conn: sqlite3.Connection,
) -> None:
    """Recompute ``percent_milestones.cumulative_cost_usd`` +
    ``marginal_cost_usd`` from the now-corrected ``session_entries``.
    Gated on cache migration 001 having applied AND ``sync_cache``
    having repopulated ``session_entries`` since.

    Scope (B2)
    ----------
    ``percent_milestones`` is normally write-once forward-only (per
    "Write-once milestones" gotcha): the cost-at-moment-of-crossing is
    captured at insert time and never recomputed. After the upstream
    dedup fix, every historical milestone's ``cumulative_cost_usd`` is
    inflated by the same factor that inflated
    ``weekly_cost_snapshots`` — keeping them as-recorded would leave
    ``percent-breakdown`` showing systematically higher numbers than
    the corrected weekly cost for the same window.

    This migration is the one-time scoped exception. For each row:

      * ``cumulative_cost_usd`` = SUM cost over
        ``[week_start_at_iso, captured_at_utc]`` from the corrected
        ``session_entries``. Sentinel for week_start: prefer
        ``week_start_at`` (ISO); fall back to ``week_start_date``
        normalized to midnight UTC if ``week_start_at IS NULL``
        (legacy rows; same shape as ``weekly_cost_snapshots``).
      * ``marginal_cost_usd`` = ``cumulative - prior.cumulative``,
        where ``prior`` is the immediately lower
        ``percent_threshold`` for the same ``(week_start_date,
        reset_event_id)``. First milestone of a week has
        ``marginal == cumulative``.

    Forward-going behavior is unchanged — new crossings keep their
    "write-once at moment of crossing" semantics. This migration only
    rewrites the historical rows once.

    Timestamp comparison
    --------------------
    Range bounds normalize through ``_canonical_utc_iso_for_index``
    at the Python boundary so plain lex compare against
    ``timestamp_utc`` hits ``idx_entries_timestamp``. Same rule as
    008/009 and the user-facing read paths in
    ``bin/_cctally_cache.py``.

    Closed interval (V1)
    --------------------
    Same ``<=`` rule as 008/009 — matches the live writer's
    ``iter_entries`` predicate.

    Banner
    ------
    Gated on ``percent_milestones`` non-emptiness (symmetric with
    008's ``snapshot_rows`` and 009's ``block_rows`` gates).

    Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3 (B2).
    """
    cache_ro = _open_cache_ro_with_gate_defer()
    try:
        projects_dirs = _resolve_projects_dirs_for_gate()

        milestone_rows = conn.execute(
            "SELECT id, week_start_date, week_start_at, captured_at_utc, "
            "       percent_threshold, reset_event_id "
            "FROM percent_milestones "
            "ORDER BY week_start_date ASC, reset_event_id ASC, "
            "         percent_threshold ASC, id ASC"
        ).fetchall()

        # Pure state-machine gate (cctally-dev#93): no inline G3 block, no
        # default-dir fallback. The resolver classifies an empty
        # ``projects_dirs`` as ``disk_state="absent"`` and decides (row 7
        # DEFER when data_present, row 5 PROCEED otherwise). No body-level
        # recompute guard and NO segment guard (spec D7): every milestone
        # recomputes from surviving ``session_entries`` — a zero-entry
        # segment correctly yields cumulative=0/marginal=0, kept isolated
        # by the ``seg_key`` partitioning of the marginal chain.
        _gate_001_post_ingest_completed(
            cache_ro, projects_dirs, data_present=bool(milestone_rows),
        )

        # SW5-style banner gating via the shared
        # ``_recompute_banner_should_emit`` helper: only print when
        # milestone_rows is non-empty AND the active subcommand is not
        # in ``_BANNER_SUPPRESSED_COMMANDS``. Mirrors 008/009.
        if _recompute_banner_should_emit(
            data_present=bool(milestone_rows)
        ):
            eprint(
                "[cctally] Recomputing percent milestone costs after "
                "dedup fix (one-time; may take 30-60s on heavy "
                "histories)..."
            )

        conn.execute("BEGIN")
        try:
            # Track per-(week_start_date, reset_event_id) the cumulative
            # cost of the immediately-prior threshold in the SAME segment
            # so we can derive marginal = cumulative - prior.cumulative.
            # The ORDER BY week_start_date, reset_event_id, threshold
            # above is what makes this single-pass safe.
            prev_cum_by_segment: dict[tuple[str, int], float] = {}

            for (
                mid, week_start_date, week_start_at, captured_at_utc,
                threshold, reset_event_id,
            ) in milestone_rows:
                # week_start_at preferred; legacy rows fall back to
                # week_start_date treated as midnight UTC (same shape
                # weekly_cost_snapshots writers use when week_start_at
                # is absent).
                if week_start_at:
                    range_start_iso = week_start_at
                elif week_start_date:
                    range_start_iso = f"{week_start_date}T00:00:00+00:00"
                else:
                    # Truly unrecoverable boundary — skip the row, leave
                    # cumulative_cost as-recorded. CHANGELOG notes
                    # parallel to 008's NULL range_*_iso skip.
                    continue

                # Canonicalize range bounds to the same UTC ISO shape
                # ``session_entries.timestamp_utc`` carries on disk so
                # lex compare hits ``idx_entries_timestamp`` instead of
                # SCANning. See ``_canonical_utc_iso_for_index`` for the
                # EXPLAIN QUERY PLAN rationale; mirrors 008/009.
                entries = cache_ro.execute(
                    "SELECT model, input_tokens, output_tokens, "
                    "       cache_create_tokens, cache_read_tokens, "
                    "       usage_extra_json, cost_usd_raw "
                    "FROM session_entries "
                    "WHERE timestamp_utc >= ? AND timestamp_utc <= ?",
                    (
                        _canonical_utc_iso_for_index(range_start_iso),
                        _canonical_utc_iso_for_index(captured_at_utc),
                    ),
                ).fetchall()

                cumulative = 0.0
                for (
                    model, i, o, cc, cr, extras_json, raw,
                ) in entries:
                    usage = {
                        "input_tokens": i,
                        "output_tokens": o,
                        "cache_creation_input_tokens": cc,
                        "cache_read_input_tokens": cr,
                    }
                    # #181: usage_extra_json is cost-irrelevant (cost is
                    # token-only); parsed here only for pre-008 rows that may
                    # still carry the legacy blob — NOT a speed reader, so the
                    # write-side NULL going forward is safe.
                    if extras_json:
                        usage.update(json.loads(extras_json))
                    cumulative += _calculate_entry_cost(
                        model, usage, mode="auto", cost_usd=raw,
                    )

                seg_key = (week_start_date, int(reset_event_id or 0))
                prior_cum = prev_cum_by_segment.get(seg_key)
                marginal = (
                    cumulative
                    if prior_cum is None
                    else cumulative - prior_cum
                )
                prev_cum_by_segment[seg_key] = cumulative

                conn.execute(
                    "UPDATE percent_milestones "
                    "SET cumulative_cost_usd = ?, "
                    "    marginal_cost_usd = ? "
                    "WHERE id = ?",
                    (cumulative, marginal, mid),
                )

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        cache_ro.close()


@stats_migration("011_budget_milestone_period_keys")
def _migration_budget_milestone_period_keys(conn: sqlite3.Connection) -> None:
    """Add a write-once ``period`` column to the three budget milestone tables
    and include it in each UNIQUE key (issue #137).

    ``budget_milestones``     -> UNIQUE(week_start_at, period, threshold)
    ``codex_budget_milestones`` -> UNIQUE(period_start_at, period, threshold)
    ``projected_milestones``  -> UNIQUE(week_start_at, period, metric, threshold)

    Fixes (1) stale dashboard period labels and (2) the calendar-week /
    calendar-month dedup collision when the 1st of the month lands on the
    configured week-start weekday.

    Historical rows are backfilled to ``period = NULL`` (the "pre-011 unknown
    period" sentinel) rather than a fabricated value, honoring write-once
    milestones. The firing pre-probe matches ``period = ? OR period IS NULL``
    so unknown-period rows never re-fire (no spurious upgrade alert), and the
    dashboard COALESCEs NULL to the vendor-default noun.

    SQLite cannot ALTER an inline UNIQUE in place -> rename-recreate-copy idiom
    (same as migration 005). Idempotent: a table that already has ``period``
    (fresh install where the live CREATE made the new shape, or a prior run) is
    skipped; when all three are present the handler returns and the dispatcher
    central-stamps the marker (#140).
    """
    specs = [
        (
            "budget_milestones",
            """
            CREATE TABLE budget_milestones (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start_at   TEXT    NOT NULL,
                period          TEXT,
                threshold       INTEGER NOT NULL,
                budget_usd      REAL    NOT NULL,
                spent_usd       REAL    NOT NULL,
                consumption_pct REAL    NOT NULL,
                crossed_at_utc  TEXT    NOT NULL,
                alerted_at      TEXT,
                UNIQUE(week_start_at, period, threshold)
            )
            """,
            # (cols copied target<-source) — period omitted from source => NULL
            "id, week_start_at, threshold, budget_usd, spent_usd, "
            "consumption_pct, crossed_at_utc, alerted_at",
        ),
        (
            "codex_budget_milestones",
            """
            CREATE TABLE codex_budget_milestones (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                period_start_at TEXT    NOT NULL,
                period          TEXT,
                threshold       INTEGER NOT NULL,
                budget_usd      REAL    NOT NULL,
                spent_usd       REAL    NOT NULL,
                consumption_pct REAL    NOT NULL,
                crossed_at_utc  TEXT    NOT NULL,
                alerted_at      TEXT,
                UNIQUE(period_start_at, period, threshold)
            )
            """,
            "id, period_start_at, threshold, budget_usd, spent_usd, "
            "consumption_pct, crossed_at_utc, alerted_at",
        ),
        (
            "projected_milestones",
            """
            CREATE TABLE projected_milestones (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start_at   TEXT    NOT NULL,
                period          TEXT,
                metric          TEXT    NOT NULL,
                threshold       INTEGER NOT NULL,
                projected_value REAL    NOT NULL,
                denominator     REAL    NOT NULL,
                crossed_at_utc  TEXT    NOT NULL,
                alerted_at      TEXT,
                UNIQUE(week_start_at, period, metric, threshold)
            )
            """,
            "id, week_start_at, metric, threshold, projected_value, "
            "denominator, crossed_at_utc, alerted_at",
        ),
    ]

    def _has_period(table: str) -> bool:
        cols = {
            str(r[1])
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        return "period" in cols

    def _table_exists(table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    # Compute needs-rebuild BEFORE any transaction (no deferred-BEGIN-then-read on
    # stats.db — SQLITE_BUSY_SNAPSHOT, migrations-gotchas.md). A spec table that
    # does not exist (e.g. codex_budget_milestones on a DB predating that feature,
    # now that v012 no longer live-creates it — #143) needs no period column.
    pending = [s for s in specs if _table_exists(s[0]) and not _has_period(s[0])]

    if not pending:
        # Fresh install (live CREATE already made the new shape) or prior run.
        return

    conn.execute("BEGIN IMMEDIATE")  # write-lock up front; DDL is first DML
    try:
        for table, create_sql, cols in pending:
            old = f"{table}_old_011"
            conn.execute(f"ALTER TABLE {table} RENAME TO {old}")
            conn.execute(create_sql)
            # period omitted from the SELECT => NULL for every historical row
            conn.execute(
                f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {old}"
            )
            conn.execute(f"DROP TABLE {old}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@stats_migration("012_unify_budget_milestones_vendor")
def _migration_unify_budget_milestones_vendor(conn: sqlite3.Connection) -> None:
    """Merge ``codex_budget_milestones`` into a vendor-tagged ``budget_milestones``
    (issue #143).

    ``budget_milestones`` (Claude, keyed ``week_start_at``) and
    ``codex_budget_milestones`` (Codex, keyed ``period_start_at``) are
    structurally identical modulo vendor + key-column name. This migration
    rebuilds ``budget_milestones`` with a ``vendor`` column and the renamed
    ``period_start_at`` key, copies Claude rows (``week_start_at``->``period_start_at``,
    ``vendor='claude'``) and Codex rows (``vendor='codex'``), and drops the Codex
    table. History + ``alerted_at`` + ``period`` are preserved verbatim; the
    write-once ``period`` NULL sentinel is carried as-is. ``id`` is NOT copied
    (AUTOINCREMENT reassigns — the envelope/dispatch ids are composite strings,
    never the row PK).

    State machine (idempotent / partial-state safe): the Claude rebuild and the
    Codex absorb are independently guarded, so a retry after a crash-before-stamp
    (table already unified, Codex maybe gone) is a clean no-op or a Codex-only
    absorb. Reads happen BEFORE BEGIN IMMEDIATE (SQLITE_BUSY_SNAPSHOT).
    """
    def _cols(table: str) -> set:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _table_exists(table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    claude_needs_rebuild = "vendor" not in _cols("budget_milestones")
    codex_present = _table_exists("codex_budget_milestones")
    if not claude_needs_rebuild and not codex_present:
        return  # already unified, no Codex leftover -> dispatcher fast-stamps

    new_table = """
        CREATE TABLE budget_milestones (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor          TEXT    NOT NULL,
            period_start_at TEXT    NOT NULL,
            period          TEXT,
            threshold       INTEGER NOT NULL,
            budget_usd      REAL    NOT NULL,
            spent_usd       REAL    NOT NULL,
            consumption_pct REAL    NOT NULL,
            crossed_at_utc  TEXT    NOT NULL,
            alerted_at      TEXT,
            UNIQUE(vendor, period_start_at, period, threshold)
        )
    """
    cols = ("vendor, period_start_at, period, threshold, budget_usd, spent_usd, "
            "consumption_pct, crossed_at_utc, alerted_at")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if claude_needs_rebuild:
            conn.execute("ALTER TABLE budget_milestones RENAME TO budget_milestones_old_012")
            conn.execute(new_table)
            conn.execute(
                f"INSERT INTO budget_milestones ({cols}) "
                "SELECT 'claude', week_start_at, period, threshold, budget_usd, "
                "spent_usd, consumption_pct, crossed_at_utc, alerted_at "
                "FROM budget_milestones_old_012"
            )
            conn.execute("DROP TABLE budget_milestones_old_012")
        if codex_present:
            conn.execute(
                f"INSERT INTO budget_milestones ({cols}) "
                "SELECT 'codex', period_start_at, period, threshold, budget_usd, "
                "spent_usd, consumption_pct, crossed_at_utc, alerted_at "
                "FROM codex_budget_milestones"
            )
            conn.execute("DROP TABLE codex_budget_milestones")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@stats_migration("013_codex_quota_projection_state")
def _migration_codex_quota_projection_state(conn: sqlite3.Connection) -> None:
    """Create the durable provider-neutral quota projection tables.

    All physical evidence stays in cache.db's ``quota_window_snapshots``.  This
    migration deliberately performs no cache read or data backfill: a later
    full projector transaction creates the interpreted rows from the committed
    physical set.  The schema helper is safe after a crash before central
    stamping, so the dispatcher can retry without duplicate state.
    """
    _cctally_core._apply_quota_projection_schema(conn)


# ── Stats registry FROZEN at the legacy head (DB journal redesign §7.1) ──
# stats.db moved to epoch-rebuild versioning (STATS_INDEX_EPOCH); NO stats
# migration is ever written again — a schema change bumps the epoch instead. This
# module-load assertion fires if a 14th @stats_migration lands, mirroring the
# import-time contiguity check in `_make_migration_decorator`. It runs BEFORE the
# test-injection block below (which registers a 14th ONLY under
# CCTALLY_MIGRATION_TEST_MODE), so the production shape is asserted at exactly 13.
STATS_REGISTRY_FROZEN_HEAD = 13
assert len(_STATS_MIGRATIONS) == STATS_REGISTRY_FROZEN_HEAD, (
    f"stats registry is frozen at {STATS_REGISTRY_FROZEN_HEAD} (DB journal "
    f"redesign §7.1) but has {len(_STATS_MIGRATIONS)} migrations — a new "
    f"@stats_migration was added. Schema changes now bump STATS_INDEX_EPOCH; the "
    f"stats registry is frozen and must not grow."
)


# === Region 8: Test-only migration registration (was bin/cctally:12086-12140) ===

# ──────────────────────────────────────────────────────────────────────
# Test-only migrations — registered ONLY when CCTALLY_MIGRATION_TEST_MODE=1
# AND HARNESS_FAKE_HOME_BASE points at a fixture home. Production runs
# never register these. Numbering is dynamic so the test migrations
# always slot at len(registry)+1 — future-proof against the next real
# migration colliding on the prefix (Codex P2 #8).
# ──────────────────────────────────────────────────────────────────────
if os.environ.get("CCTALLY_MIGRATION_TEST_MODE") == "1":
    if not os.environ.get("HARNESS_FAKE_HOME_BASE"):
        eprint(
            "cctally: CCTALLY_MIGRATION_TEST_MODE=1 set but "
            "HARNESS_FAKE_HOME_BASE is empty; refusing to register "
            "test-only migrations against a non-fixture home."
        )
        sys.exit(2)

    _stats_test_seq = len(_STATS_MIGRATIONS) + 1
    _stats_test_name = f"{_stats_test_seq:03d}_test_failure_injection"

    @stats_migration(_stats_test_name)
    def _test_migration_failure_injection(conn):
        """Test-only migration: raises RuntimeError when test_failure_trigger
        table is non-empty; otherwise it is a no-op (the dispatcher stamps)."""
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='test_failure_trigger'"
        ).fetchone() and conn.execute(
            "SELECT 1 FROM test_failure_trigger LIMIT 1"
        ).fetchone():
            raise RuntimeError("test failure injected")
        return

    _cache_test_seq = len(_CACHE_MIGRATIONS) + 1
    _cache_test_name = f"{_cache_test_seq:03d}_test_cache_migration"

    @cache_migration(_cache_test_name)
    def _test_cache_migration(conn):
        """Test-only cache migration: no-op body; the dispatcher stamps."""
        return


# === Region 9: db CLI subcommands (was bin/cctally:19707-20043) ===

def cmd_db_status(args: argparse.Namespace) -> int:
    """Render migration status across both DBs.

    Spec: docs/superpowers/specs/2026-05-06-migration-framework-design.md §4.2.
    Glyphs: ✓ applied, ✗ failed, · pending, ~ skipped.
    """
    payload = {
        "schema_version": 1,
        "databases": {
            "stats.db": _db_status_for(_cctally_core.DB_PATH, _STATS_MIGRATIONS, "stats.db"),
            "cache.db": _db_status_for(_cctally_core.CACHE_DB_PATH, _CACHE_MIGRATIONS, "cache.db"),
            "conversations.db": _db_status_for(
                _cctally_core.CONVERSATIONS_DB_PATH, _CONVERSATIONS_MIGRATIONS,
                "conversations.db",
            ),
        },
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for db_label in ("stats.db", "cache.db", "conversations.db"):
        info = payload["databases"][db_label]
        skipped_count = sum(1 for m in info["migrations"] if m["status"] == "skipped")
        suffix = f" ({skipped_count} skipped)" if skipped_count else ""
        print(
            f"{db_label} ({info['path']})  "
            f"version {info['user_version']} / {info['registry_size']} known{suffix}"
        )
        for m in info["migrations"]:
            line = _db_status_format_row(m)
            print(line)
        print()  # blank line between DBs
    return 0


def _db_status_for(
    db_path: pathlib.Path, registry: list[Migration], db_label: str,
) -> dict:
    """Build per-DB status dict.

    Tolerates missing tables: cache.db never opened by the framework
    (registry empty in v1) won't have schema_migrations /
    schema_migrations_skipped, so each lookup is wrapped in a try /
    except sqlite3.OperationalError.
    """
    if not db_path.exists():
        return {
            "path": str(db_path),
            "user_version": 0,
            "registry_size": len(registry),
            "migrations": [
                {"seq": m.seq, "name": m.name, "status": "pending"}
                for m in registry
            ],
        }
    conn = sqlite3.connect(db_path)
    try:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        # Tolerate missing tables (e.g., cache.db never opened by framework).
        # Alias legacy unprefixed names to their NNN_ equivalents so a
        # `db status` that runs before any open_db()-via-bootstrap rename
        # still reports legacy-marker DBs as applied, not pending.
        aliases = _LEGACY_MARKER_ALIASES_BY_DB.get(db_label, {})
        applied_rows: dict[str, str] = {}
        try:
            for row in conn.execute(
                "SELECT name, applied_at_utc FROM schema_migrations"
            ).fetchall():
                applied_rows[aliases.get(row[0], row[0])] = row[1]
        except sqlite3.OperationalError:
            pass
        skipped_rows: dict[str, tuple[str, str | None]] = {}
        try:
            for row in conn.execute(
                "SELECT name, skipped_at_utc, reason FROM schema_migrations_skipped"
            ).fetchall():
                skipped_rows[row[0]] = (row[1], row[2])
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()

    failed_names = _db_status_failed_names_from_log(db_label)
    migrations_out: list[dict] = []
    for m in registry:
        if m.name in skipped_rows:
            ts, reason = skipped_rows[m.name]
            migrations_out.append({
                "seq": m.seq, "name": m.name,
                "status": "skipped",
                "skipped_at": ts,
                "reason": reason,
            })
        elif m.name in applied_rows:
            migrations_out.append({
                "seq": m.seq, "name": m.name,
                "status": "applied",
                "applied_at": applied_rows[m.name],
            })
        elif m.name in failed_names:
            migrations_out.append({
                "seq": m.seq, "name": m.name,
                "status": "failed",
                "last_failure_at": failed_names[m.name],
                "log_path": str(_cctally_core.MIGRATION_ERROR_LOG_PATH),
            })
        else:
            migrations_out.append({
                "seq": m.seq, "name": m.name,
                "status": "pending",
            })
    return {
        "path": str(db_path),
        "user_version": user_version,
        "registry_size": len(registry),
        "migrations": migrations_out,
    }


def _db_status_failed_names_from_log(db_label: str) -> dict[str, str]:
    """Parse migration-errors.log for the most-recent failure per qualified name.

    Returns {migration_name (unqualified): last_failure_iso}. Names whose
    log entries lack the db_label prefix (pre-framework legacy entries)
    are NOT included for cache.db; for stats.db, legacy names like
    `merge_5h_block_duplicates_v1` are bootstrap-renamed at next open
    (via Task 4) so they don't accumulate post-PR.
    """
    if not _cctally_core.MIGRATION_ERROR_LOG_PATH.exists():
        return {}
    out: dict[str, str] = {}
    try:
        content = _cctally_core.MIGRATION_ERROR_LOG_PATH.read_text()
    except Exception:
        return {}
    blocks = [b for b in content.split("\n\n") if b.strip()]
    for block in blocks:
        first_line = block.splitlines()[0] if block.splitlines() else ""
        if not first_line.startswith("[") or "] " not in first_line:
            continue
        ts_part, _, name_part = first_line[1:].partition("] ")
        ts = ts_part.strip()
        qualified = name_part.strip()
        if ":" not in qualified:
            continue
        prefix, _, name = qualified.partition(":")
        if prefix != db_label:
            continue
        out[name] = ts  # later block overrides earlier — most-recent wins
    return out


def _db_status_format_row(m: dict) -> str:
    name = m["name"]
    status = m["status"]
    if status == "applied":
        return f"  ✓ {name:<46} applied {m['applied_at']}"
    if status == "skipped":
        line = f"  ~ {name:<46} skipped {m['skipped_at']}"
        if m.get("reason"):
            line += f"\n                                                Reason: {m['reason']}"
        return line
    if status == "failed":
        return (
            f"  ✗ {name:<46} FAILED last at {m['last_failure_at']}\n"
            f"                                                See {m['log_path']}"
        )
    return f"  · {name:<46} pending"


def _db_resolve_migration_name(name_arg: str) -> tuple[str, str, list[Migration]]:
    """Resolve a name argument to (db_label, unqualified_name, registry).

    Spec §4.1 routing:
      - "stats.db:NNN_…" → stats registry only.
      - "cache.db:NNN_…" → cache registry only.
      - "NNN_…" (bare)  → both; ambiguous if matches both, exit 2.

    Raises:
      LookupError: name not found in any registry (caller exits 1).
      RuntimeError: ambiguous bare name (caller exits 2).
    """
    if name_arg.startswith("stats.db:"):
        unq = name_arg[len("stats.db:"):]
        if any(m.name == unq for m in _STATS_MIGRATIONS):
            return "stats.db", unq, _STATS_MIGRATIONS
        raise LookupError(name_arg)
    if name_arg.startswith("cache.db:"):
        unq = name_arg[len("cache.db:"):]
        if any(m.name == unq for m in _CACHE_MIGRATIONS):
            return "cache.db", unq, _CACHE_MIGRATIONS
        raise LookupError(name_arg)
    if name_arg.startswith("conversations.db:"):
        unq = name_arg[len("conversations.db:"):]
        if any(m.name == unq for m in _CONVERSATIONS_MIGRATIONS):
            return "conversations.db", unq, _CONVERSATIONS_MIGRATIONS
        raise LookupError(name_arg)
    in_stats = any(m.name == name_arg for m in _STATS_MIGRATIONS)
    in_cache = any(m.name == name_arg for m in _CACHE_MIGRATIONS)
    in_conv = any(m.name == name_arg for m in _CONVERSATIONS_MIGRATIONS)
    if (in_stats + in_cache + in_conv) > 1:
        raise RuntimeError(name_arg)
    if in_stats:
        return "stats.db", name_arg, _STATS_MIGRATIONS
    if in_cache:
        return "cache.db", name_arg, _CACHE_MIGRATIONS
    if in_conv:
        return "conversations.db", name_arg, _CONVERSATIONS_MIGRATIONS
    raise LookupError(name_arg)


def _db_path_for_label(db_label: str) -> pathlib.Path:
    if db_label == "stats.db":
        return _cctally_core.DB_PATH
    if db_label == "cache.db":
        return _cctally_core.CACHE_DB_PATH
    if db_label == "conversations.db":
        return _cctally_core.CONVERSATIONS_DB_PATH
    raise ValueError(f"unknown db_label: {db_label}")


def cmd_db_skip(args: argparse.Namespace) -> int:
    """Mark a migration as skipped.

    Spec §4.3. Bypasses ``open_db()`` (raw ``sqlite3.connect``) so the
    pending migration cannot be triggered en route to recording the
    skip — that defeats the entire point.
    """
    name_arg = args.name
    try:
        db_label, name, _ = _db_resolve_migration_name(name_arg)
    except RuntimeError:
        eprint(
            f"cctally: ambiguous migration name '{name_arg}'; "
            f"qualify as 'stats.db:{name_arg}', 'cache.db:{name_arg}', or 'conversations.db:{name_arg}'"
        )
        return 2
    except LookupError:
        eprint(f"cctally: unknown migration '{name_arg}'.")
        return 1

    path = _db_path_for_label(db_label)
    # Ensure the parent dir exists — fresh-install HOME may not have
    # ~/.local/share/cctally/ yet, and sqlite3.connect() does NOT
    # create parent directories (only the DB file itself).
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations_skipped (
                name           TEXT PRIMARY KEY,
                skipped_at_utc TEXT NOT NULL,
                reason         TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
        )
        # Reject if already applied. Also check the legacy unprefixed
        # alias: pre-framework DBs may store the marker under e.g.
        # `merge_5h_block_duplicates_v1` until the next open_db()
        # bootstrap rename. Without this, `db skip 003_…` succeeds
        # against an already-applied legacy-marker DB and leaves the
        # row in BOTH schema_migrations (post-rename) AND
        # schema_migrations_skipped — inconsistent state.
        applied_check_names = [name]
        for legacy, new in _LEGACY_MARKER_ALIASES_BY_DB.get(db_label, {}).items():
            if new == name:
                applied_check_names.append(legacy)
        placeholders = ",".join("?" * len(applied_check_names))
        if conn.execute(
            f"SELECT 1 FROM schema_migrations WHERE name IN ({placeholders})",
            applied_check_names,
        ).fetchone():
            eprint(f"cctally: {name} is already applied; nothing to skip.")
            return 1
        # Reject if already skipped.
        if conn.execute(
            "SELECT 1 FROM schema_migrations_skipped WHERE name = ?", (name,)
        ).fetchone():
            eprint(f"cctally: {name} is already skipped.")
            return 1
        conn.execute(
            "INSERT INTO schema_migrations_skipped "
            "(name, skipped_at_utc, reason) VALUES (?, ?, ?)",
            (name, now_utc_iso(), args.reason),
        )
        conn.commit()
    finally:
        conn.close()
    print(f"Skipped: {name}")
    return 0


def cmd_db_unskip(args: argparse.Namespace) -> int:
    """Remove a skip mark.

    Spec §4.4. After deleting the row, writes ``PRAGMA user_version = 0``
    to invalidate the dispatcher's fast path (Codex P1 #1) — without
    this, the unskipped migration would silently never run because
    ``cur_version == len(registry)`` short-circuits on the next open.

    Bypasses ``open_db()`` so the migration about to be unskipped can
    actually run on the next legitimate open, not from inside this
    handler.
    """
    name_arg = args.name
    try:
        db_label, name, _ = _db_resolve_migration_name(name_arg)
    except RuntimeError:
        eprint(
            f"cctally: ambiguous migration name '{name_arg}'; "
            f"qualify as 'stats.db:{name_arg}', 'cache.db:{name_arg}', or 'conversations.db:{name_arg}'"
        )
        return 2
    except LookupError:
        eprint(f"cctally: unknown migration '{name_arg}'.")
        return 1

    path = _db_path_for_label(db_label)
    # If the DB file doesn't even exist, the migration cannot have been
    # skipped. Avoid creating an empty stats.db / cache.db just to print
    # the no-op message (sqlite3.connect would create the file
    # otherwise — leaving stale empty DBs around for fresh-install
    # users who poke at unskip).
    if not path.exists():
        print(f"cctally: {name} is not skipped; nothing to do.")
        return 0
    conn = sqlite3.connect(path)
    try:
        try:
            cur = conn.execute(
                "DELETE FROM schema_migrations_skipped WHERE name = ?", (name,)
            )
        except sqlite3.OperationalError:
            # Table doesn't exist → nothing was ever skipped.
            print(f"cctally: {name} is not skipped; nothing to do.")
            return 0
        if cur.rowcount == 0:
            print(f"cctally: {name} is not skipped; nothing to do.")
            return 0
        # Invalidate the fast path so the dispatcher re-walks (Codex P1 #1).
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.close()
    print(f"Unskipped: {name} (will run on next open).")
    return 0


def cmd_db_recover(args: argparse.Namespace) -> int:
    """Revert a version-ahead DB to this binary's known schema head (#145).

    cache.db is fully re-derivable, so `--db cache` heals without --yes.
    stats.db holds non-re-derivable snapshots/milestones, so `--db stats`
    requires explicit --yes and may need a re-record afterward, AND honors the
    #146 prod guard (a dev/worktree binary refuses to trim+revert the real prod
    stats.db unless CCTALLY_ALLOW_PROD_MIGRATION=1). Bypasses
    open_db()/open_cache_db() (raw connect) so it never re-triggers the
    dispatcher. Idempotent: a no-op when the DB is not ahead.
    """
    which = args.db  # "cache" | "stats"
    if which == "stats":
        # RETIRED (DB journal redesign §7.1). stats.db is now a disposable
        # journal index — a version mismatch resolves by rebuild, not by
        # trim-and-revert. The old trim path would mangle an epoch-stamped DB
        # (user_version 1000 > 13 looks "version-ahead" to the legacy registry).
        # Point the operator at the rebuild surface instead.
        eprint(
            "cctally: `db recover --db stats` is retired — stats.db is now a "
            "disposable index materialized from the append-only journal, so a "
            "version mismatch self-heals by rebuild rather than trim-and-revert. "
            "Run `cctally db rebuild --db stats` to rebuild it from the journal."
        )
        return 2
    path, registry, label = (
        _cctally_core.CACHE_DB_PATH, _CACHE_MIGRATIONS, "cache.db")

    # Absent file → nothing to recover; do NOT connect (sqlite3.connect would
    # create an empty DB file — mirrors cmd_db_unskip).
    if not path.exists():
        print(f"cctally: {label} not present; nothing to recover.")
        return 0

    conn = sqlite3.connect(path)
    try:
        cur_version = conn.execute("PRAGMA user_version").fetchone()[0]
        head = len(registry)
        if cur_version <= head:
            print(
                f"cctally: {label} is at version {cur_version} "
                f"(≤ known {head}); nothing to recover."
            )
            return 0
        # `--db stats` returned early above (retired, §7.1), so control only
        # reaches here for cache.db: re-derivable, healed in place with no --yes
        # and no #146 prod guard (the dispatcher's opt-in auto-heal already covers
        # it). The former `which == "stats"` prod-guard + --yes gates were dead
        # after the retirement and have been removed.
        info = _recover_version_ahead(conn, registry, label)
        print(
            f"cctally: reverted {label} v{info['reverted_from']} → "
            f"v{info['reverted_to']}, dropped {info['trimmed']} unknown marker(s)."
        )
        return 0
    finally:
        conn.close()


def _db_backup_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _repair_marker_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name("stats.db.repairing")


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _claim_repair_marker(path: pathlib.Path) -> "tuple[bool, str]":
    """Atomically block new cctally stats opens; reclaim dead-owner markers."""
    marker = _repair_marker_path(path)
    for _attempt in range(2):
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                owner = int(marker.read_text().strip())
            except (OSError, ValueError):
                owner = -1
            if _pid_is_alive(owner):
                return False, f"another stats.db repair owns {marker} (pid {owner})"
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                return False, f"could not remove stale repair marker {marker}: {exc}"
            continue
        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(marker.parent)
        return True, ""
    return False, f"could not claim repair marker {marker}"


def _release_repair_marker(path: pathlib.Path) -> None:
    marker = _repair_marker_path(path)
    try:
        marker.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(marker.parent)


def _db_family_open_pids(path: pathlib.Path) -> "set[int] | None":
    """Return processes with main/WAL/SHM open; None means unverifiable."""
    family = [
        pathlib.Path(str(path) + suffix)
        for suffix in ("", "-wal", "-shm")
        if pathlib.Path(str(path) + suffix).exists()
    ]
    lsof = shutil.which("lsof")
    if lsof:
        try:
            result = subprocess.run(
                [lsof, "-F", "p", "--", *(str(item) for item in family)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
        except OSError:
            return None
        return {
            int(line[1:])
            for line in result.stdout.splitlines()
            if line.startswith("p") and line[1:].isdigit()
        }

    proc = pathlib.Path("/proc")
    if not proc.is_dir():
        return None
    try:
        identities = {(item.stat().st_dev, item.stat().st_ino) for item in family}
    except OSError:
        return None
    pids: set[int] = set()
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            descriptors = (process / "fd").iterdir()
            for descriptor in descriptors:
                try:
                    st = descriptor.stat()
                except OSError:
                    continue
                if (st.st_dev, st.st_ino) in identities:
                    pids.add(int(process.name))
                    break
        except (OSError, PermissionError):
            continue
    return pids


def _unique_sibling_path(path: pathlib.Path) -> pathlib.Path:
    """Return ``path`` or a numbered sibling without overwriting owner data."""
    def family_exists(candidate: pathlib.Path) -> bool:
        return any(
            pathlib.Path(str(candidate) + suffix).exists()
            for suffix in ("", "-wal", "-shm")
        )

    if not family_exists(path):
        return path
    for number in range(2, 10_000):
        candidate = path.with_name(f"{path.name}-{number}")
        if not family_exists(candidate):
            return candidate
    raise OSError(f"could not allocate a unique backup path beside {path}")


def _fsync_file(path: pathlib.Path) -> None:
    with path.open("rb") as fh:
        os.fsync(fh.fileno())


def _fsync_directory(path: pathlib.Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_db_family(
    source: pathlib.Path,
    destination: pathlib.Path,
    *,
    suffixes: "tuple[str, ...]" = ("", "-wal", "-shm"),
) -> None:
    """Copy main/WAL/SHM bytes while the caller holds SQLite's writer lock."""
    for suffix in suffixes:
        src = pathlib.Path(str(source) + suffix)
        if not src.exists():
            continue
        dst = pathlib.Path(str(destination) + suffix)
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
        _fsync_file(dst)


def _read_user_version_header(path: pathlib.Path) -> "int | None":
    """Read SQLite's big-endian user_version field without opening pages."""
    try:
        with path.open("rb") as fh:
            header = fh.read(100)
    except OSError:
        return None
    if len(header) < 64 or header[:16] != b"SQLite format 3\x00":
        return None
    return int.from_bytes(header[60:64], "big", signed=False)


def _table_counts_best_effort(conn: sqlite3.Connection) -> dict[str, "int | None"]:
    try:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
    except sqlite3.DatabaseError:
        return {}
    counts: dict[str, int | None] = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        try:
            counts[name] = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        except sqlite3.DatabaseError:
            counts[name] = None
    return counts


def _run_sqlite_recover(
    sqlite_binary: str,
    source: pathlib.Path,
    destination: pathlib.Path,
    scratch: pathlib.Path,
) -> "tuple[bool, str]":
    """Stream sqlite3 ``.recover`` through a SQL file into a fresh database."""
    sql_path = scratch / "recover.sql"
    try:
        with sql_path.open("wb") as sql_out:
            recovered = subprocess.run(
                [sqlite_binary, str(source), ".recover"],
                stdout=sql_out,
                stderr=subprocess.PIPE,
                check=False,
            )
    except OSError as exc:
        return False, str(exc)
    if recovered.returncode != 0:
        reason = recovered.stderr.decode("utf-8", "replace").strip()
        return False, reason or f"sqlite3 .recover exited {recovered.returncode}"
    try:
        with sql_path.open("rb") as sql_in:
            imported = subprocess.run(
                [sqlite_binary, str(destination)],
                stdin=sql_in,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
    except OSError as exc:
        return False, str(exc)
    if imported.returncode != 0:
        reason = imported.stderr.decode("utf-8", "replace").strip()
        return False, reason or f"sqlite3 import exited {imported.returncode}"
    return True, ""


def _probe_sqlite_recover(sqlite_binary: str) -> "tuple[bool, str]":
    """Prove the selected shell has a functional ``.recover`` command."""
    try:
        probe = subprocess.run(
            [sqlite_binary, ":memory:", ".recover"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        return False, str(exc)
    stderr = probe.stderr.decode("utf-8", "replace").strip()
    if probe.returncode != 0:
        return False, stderr or f"sqlite3 .recover probe exited {probe.returncode}"
    if b"BEGIN;" not in probe.stdout or b"COMMIT;" not in probe.stdout:
        return False, stderr or "sqlite3 .recover probe produced incomplete SQL"
    return True, ""


def _repair_preflight_and_copy(
    path: pathlib.Path,
    backup: pathlib.Path,
    snapshot: pathlib.Path,
    *,
    timeout_ms: int,
) -> "tuple[int, dict[str, int | None], int | None, sqlite3.Connection | None, str]":
    """Preserve forensic bytes, drain WAL, and return a held writer guard."""
    conn = sqlite3.connect(
        f"file:{path}?mode=rw", uri=True, timeout=max(timeout_ms, 0) / 1000
    )
    try:
        if _would_block_prod_migration(conn):
            conn.close()
            return 2, {}, None, None, "prod guard"
        conn.execute(f"PRAGMA busy_timeout={max(timeout_ms, 0)}")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.DatabaseError as exc:
            conn.close()
            return 3, {}, None, None, str(exc)
        try:
            quick = conn.execute("PRAGMA quick_check(1)").fetchone()
        except sqlite3.DatabaseError:
            quick = None
        if quick is not None and quick[0] == "ok":
            conn.rollback()
            conn.close()
            return 4, {}, None, None, "quick_check ok"

        # Preserve the exact corrupt family before checkpointing mutates the
        # main/WAL representation. No other cctally process can open after the
        # repair marker, and the caller has already proved no old handle exists.
        _copy_db_family(path, backup)
        _fsync_directory(path.parent)
        conn.rollback()

        try:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.DatabaseError as exc:
            conn.close()
            return 3, {}, None, None, f"WAL checkpoint failed: {exc}"
        wal_path = pathlib.Path(str(path) + "-wal")
        wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
        if checkpoint is None or int(checkpoint[0]) != 0 or wal_bytes != 0:
            conn.close()
            return 3, {}, None, None, "WAL could not be fully checkpointed"

        # Hold this one write exclusion continuously through .recover and the
        # main-file replace. Since the WAL is empty, replacement failure leaves
        # the old main file coherent and no committed frames can be lost.
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.DatabaseError as exc:
            conn.close()
            return 3, {}, None, None, str(exc)
        try:
            source_version = int(
                conn.execute("PRAGMA user_version").fetchone()[0]
            )
        except sqlite3.DatabaseError:
            source_version = None
        counts = _table_counts_best_effort(conn)
        _copy_db_family(path, snapshot, suffixes=("",))
        return 0, counts, source_version, conn, ""
    except Exception:
        try:
            conn.rollback()
        except sqlite3.DatabaseError:
            pass
        conn.close()
        raise


def cmd_db_repair(args: argparse.Namespace) -> int:
    """Claim exclusive maintenance ownership, then repair stats.db (#314)."""
    path = _cctally_core.DB_PATH
    if not path.exists():
        print("cctally: stats.db not present; nothing to repair.")
        return 0
    if not getattr(args, "yes", False):
        eprint(
            "cctally: repairing stats.db replaces the live non-re-derivable "
            "database after preserving the corrupt original. Re-run with "
            "--yes after stopping the dashboard and other cctally processes."
        )
        return 2

    try:
        claimed, reason = _claim_repair_marker(path)
    except OSError as exc:
        eprint(f"cctally: could not create stats.db repair marker: {exc}")
        return 3
    if not claimed:
        eprint(f"cctally: {reason}")
        return 3
    try:
        rc = _cmd_db_repair_exclusive(args, path)
    except (OSError, sqlite3.DatabaseError) as exc:
        eprint(f"cctally: stats.db repair failed before completion: {exc}")
        rc = 3
    except Exception as exc:  # defensive: marker cleanup still must run
        eprint(f"cctally: unexpected stats.db repair failure: {exc}")
        rc = 3
    try:
        _release_repair_marker(path)
    except OSError as exc:
        eprint(
            f"cctally: stats.db repair marker cleanup failed ({exc}); "
            f"remove {_repair_marker_path(path)} after confirming no repair runs."
        )
        return 3
    return rc


def _cmd_db_repair_exclusive(args: argparse.Namespace, path: pathlib.Path) -> int:
    """Verify no pre-marker handle remains, then enter the repair body."""
    open_pids = _db_family_open_pids(path)
    if open_pids is None:
        eprint(
            "cctally: cannot verify that stats.db has no open handles on "
            "this platform; refusing the destructive repair."
        )
        return 3
    if open_pids:
        eprint(
            "cctally: stats.db is still open in process(es) "
            + ", ".join(str(pid) for pid in sorted(open_pids))
            + ". Stop the dashboard and other cctally processes, then retry."
        )
        return 3
    return _cmd_db_repair_claimed(args, path)


def _cmd_db_repair_claimed(args: argparse.Namespace, path: pathlib.Path) -> int:
    """Repair body; caller owns the marker and has proved no old handles."""

    timeout_ms = int(getattr(args, "busy_timeout_ms", 250) or 250)
    sqlite_binary = (
        getattr(args, "sqlite3_binary", None) or shutil.which("sqlite3")
    )
    if not sqlite_binary:
        eprint(
            "cctally: db repair requires the sqlite3 command-line tool for "
            'its corruption-tolerant ".recover" operation. The live DB is '
            "untouched and no corrupt backup was created."
        )
        return 3
    recover_supported, recover_reason = _probe_sqlite_recover(
        str(sqlite_binary)
    )
    if not recover_supported:
        eprint(
            "cctally: the selected sqlite3 build does not support SQLite "
            f".recover ({recover_reason}). Install the official sqlite.org "
            "CLI built with SQLITE_ENABLE_DBPAGE_VTAB. The live DB is "
            "untouched and no corrupt backup was created."
        )
        return 3

    stamp = _db_backup_timestamp()
    backup = _unique_sibling_path(
        path.with_name(f"{path.name}.bak-corrupt-malformed-{stamp}")
    )
    with tempfile.TemporaryDirectory(
        prefix=".cctally-repair-", dir=path.parent
    ) as scratch_raw:
        scratch = pathlib.Path(scratch_raw)
        snapshot = scratch / path.name
        recovered_path = scratch / "recovered.db"
        try:
            (
                preflight_rc,
                source_counts,
                source_version,
                guard,
                preflight_reason,
            ) = _repair_preflight_and_copy(
                path, backup, snapshot, timeout_ms=timeout_ms
            )
        except (OSError, sqlite3.DatabaseError) as exc:
            eprint(f"cctally: could not preserve stats.db before repair: {exc}")
            return 3
        if preflight_rc == 2:
            eprint(
                "cctally: refusing to repair stats.db in the prod data dir "
                "(~/.local/share/cctally) from a dev checkout. Run the "
                "installed binary, or override with "
                "CCTALLY_ALLOW_PROD_MIGRATION=1."
            )
            return 2
        if preflight_rc == 3:
            eprint(
                "cctally: could not establish a quiescent stats.db writer "
                f"guard ({preflight_reason}). The DB is still in use or too "
                "damaged to lock safely. "
                "Stop the dashboard and other cctally processes, then retry; "
                "nothing was changed."
            )
            return 3
        if preflight_rc == 4:
            eprint(
                "cctally: stats.db quick_check is ok; refusing a destructive "
                "repair. Use `cctally db backup --db stats` for a safe backup."
            )
            return 2

        assert guard is not None

        def close_guard() -> None:
            nonlocal guard
            if guard is None:
                return
            try:
                guard.rollback()
            except sqlite3.DatabaseError:
                pass
            guard.close()
            guard = None

        # PRAGMA user_version above is WAL-aware. The WAL was then checkpointed
        # before the snapshot; use its main header only if the pragma itself was
        # unreadable rather than inventing a version (#148).
        if source_version is None:
            source_version = _read_user_version_header(snapshot)
        if source_version is None:
            eprint(
                "cctally: the SQLite header is too damaged to preserve "
                "PRAGMA user_version safely; refusing the automated swap. "
                f"The live DB is untouched and the corrupt backup is {backup}."
            )
            close_guard()
            return 3

        ok, reason = _run_sqlite_recover(
            str(sqlite_binary), snapshot, recovered_path, scratch
        )
        if not ok:
            eprint(
                f"cctally: sqlite3 recovery failed: {reason}. The corrupt "
                f"original was preserved at {backup}."
            )
            close_guard()
            return 3

        try:
            recovered_conn = sqlite3.connect(recovered_path)
            try:
                if source_version is not None:
                    recovered_conn.execute(
                        f"PRAGMA user_version={int(source_version)}"
                    )
                    recovered_conn.commit()
                recovered_version = int(
                    recovered_conn.execute("PRAGMA user_version").fetchone()[0]
                )
                integrity_rows = recovered_conn.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
                recovered_counts = _table_counts_best_effort(recovered_conn)
            finally:
                recovered_conn.close()
        except sqlite3.DatabaseError as exc:
            eprint(
                f"cctally: recovered stats.db could not be verified ({exc}); "
                f"the live DB is untouched and the corrupt backup is {backup}."
            )
            close_guard()
            return 3

        if integrity_rows != [("ok",)]:
            eprint(
                "cctally: recovered stats.db failed integrity_check; the live "
                f"DB is untouched and the corrupt backup is {backup}."
            )
            close_guard()
            return 3
        recovered_usage = recovered_counts.get("weekly_usage_snapshots")
        if recovered_usage is None:
            eprint(
                "cctally: recovered stats.db has no readable "
                "weekly_usage_snapshots table; refusing to replace the live DB. "
                f"The corrupt backup is {backup}."
            )
            close_guard()
            return 3
        source_usage = source_counts.get("weekly_usage_snapshots")
        if source_usage is None:
            eprint(
                "cctally: source weekly_usage_snapshots count is unreadable; "
                "refusing an automated swap that cannot prove preservation. "
                f"The live DB is untouched and the corrupt backup is {backup}."
            )
            close_guard()
            return 3
        if recovered_usage != source_usage:
            eprint(
                "cctally: recovered weekly_usage_snapshots count changed "
                f"({source_usage} -> {recovered_usage}); refusing the swap. "
                f"The corrupt backup is {backup}."
            )
            close_guard()
            return 3

        try:
            os.chmod(recovered_path, 0o600)
            _fsync_file(recovered_path)
            # WAL is already fully checkpointed and this same guard has blocked
            # every writer since capture. Replace the coherent main file first;
            # a failed replace therefore leaves the old coherent main + empty
            # sidecars intact. New cctally opens remain blocked by the marker.
            os.replace(recovered_path, path)
            _fsync_directory(path.parent)
            close_guard()
            for suffix in ("-wal", "-shm"):
                try:
                    pathlib.Path(str(path) + suffix).unlink()
                except FileNotFoundError:
                    pass
            _fsync_directory(path.parent)
        except OSError as exc:
            close_guard()
            eprint(
                f"cctally: final stats.db swap failed ({exc}); the corrupt "
                f"original remains preserved at {backup}."
            )
            return 3

    print(f"cctally: repaired stats.db; integrity_check ok; user_version {recovered_version}.")
    print(
        "cctally: weekly_usage_snapshots: "
        f"{source_usage} -> {recovered_usage}."
    )
    differences = []
    for name in sorted(set(source_counts) | set(recovered_counts)):
        before = source_counts.get(name)
        after = recovered_counts.get(name)
        if name == "weekly_usage_snapshots" or before == after:
            continue
        before_label = (
            "missing" if name not in source_counts
            else ("source unreadable" if before is None else str(before))
        )
        after_label = (
            "missing" if name not in recovered_counts
            else ("unreadable" if after is None else str(after))
        )
        differences.append(f"{name}: {before_label} -> {after_label}")
    if differences:
        print("cctally: recovered row differences: " + "; ".join(differences))
    print(f"cctally: corrupt original preserved at {backup}")
    return 0


def cmd_db_backup(args: argparse.Namespace) -> int:
    """Create one consistent SQLite online-backup snapshot (#314)."""
    which = args.db
    if which == "cache":
        path, label = _cctally_core.CACHE_DB_PATH, "cache.db"
    else:
        path, label = _cctally_core.DB_PATH, "stats.db"
    if not path.exists():
        print(f"cctally: {label} not present; nothing to back up.")
        return 0

    raw_output = (
        getattr(args, "backup_output", None)
        or getattr(args, "output", None)  # direct-call compatibility
    )
    if raw_output:
        output = pathlib.Path(raw_output).expanduser()
    else:
        output = _unique_sibling_path(
            path.with_name(f"{path.name}.bak-{_db_backup_timestamp()}")
        )
    if output.exists():
        eprint(f"cctally: backup destination already exists: {output}")
        return 2
    if not output.parent.exists():
        eprint(f"cctally: backup destination directory does not exist: {output.parent}")
        return 2

    timeout_ms = int(getattr(args, "busy_timeout_ms", 15_000) or 15_000)
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{output.name}.tmp-", dir=output.parent
        ) as scratch_raw:
            temp_path = pathlib.Path(scratch_raw) / output.name
            source = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, timeout=max(timeout_ms, 0) / 1000
            )
            destination = sqlite3.connect(temp_path)
            try:
                source.execute(f"PRAGMA busy_timeout={max(timeout_ms, 0)}")
                source.backup(destination)
                destination.commit()
                rows = destination.execute("PRAGMA integrity_check").fetchall()
            finally:
                destination.close()
                source.close()
            if rows != [("ok",)]:
                eprint(f"cctally: backup integrity_check failed for {label}")
                return 3
            os.chmod(temp_path, 0o600)
            _fsync_file(temp_path)
            try:
                # Hard-link publication is same-filesystem and fails atomically
                # if another process created the destination after validation.
                # TemporaryDirectory then removes only its original link.
                os.link(temp_path, output)
            except FileExistsError:
                eprint(f"cctally: backup destination already exists: {output}")
                return 2
            _fsync_directory(output.parent)
    except sqlite3.DatabaseError as exc:
        if which == "stats" and _is_sqlite_corruption_error(exc):
            eprint(f"cctally: {_stats_corruption_guidance()}")
        else:
            eprint(f"cctally: could not back up {label}: {exc}")
        return 3
    except OSError as exc:
        eprint(f"cctally: could not back up {label}: {exc}")
        return 3

    print(f"cctally: backed up {label} to {output} (integrity_check ok).")
    return 0


def cmd_db_checkpoint(args: argparse.Namespace) -> int:
    """Fast, non-destructive WAL drain (TRUNCATE checkpoint) for cache.db /
    stats.db (#297).

    Opens a RAW existing-file-only connection — NOT open_cache_db()/open_db(),
    which apply schema, run the migration dispatcher, can DELETE Codex rows on a
    column-add, and CREATE a missing DB; a checkpoint may do none of those.
    Precedent: cmd_db_recover's raw connect above. It flushes WAL frames into
    the main DB and truncates the -wal file — it changes no data, no schema, no
    user_version — so there is no prod guard and no --yes.

    Exit 0 when drained / already-small / the DB is absent; 3 (staged) if the
    target stayed busy or was not fully truncated through the timeout — an
    actionable "something is still holding it" signal.
    """
    import _cctally_cache
    from _lib_json_envelope import stamp_schema_version

    which = args.db  # "cache" | "stats"
    if which == "cache":
        path, label = _cctally_core.CACHE_DB_PATH, "cache.db"
    else:
        path, label = _cctally_core.DB_PATH, "stats.db"
    timeout = int(getattr(args, "busy_timeout_ms", None)
                  or _cctally_cache.CHECKPOINT_CMD_BUSY_TIMEOUT_MS)
    as_json = bool(getattr(args, "json", False))

    # Absent file → nothing to drain; a missing re-derivable cache is not an
    # error (mirrors cmd_db_recover / cmd_db_unskip). Do NOT connect — mode=rw
    # would refuse to create it, but skip it entirely for a clean message.
    if not path.exists():
        if as_json:
            payload = {"db": label, "walBytesBefore": 0, "walBytesAfter": 0,
                       "framesCheckpointed": 0, "busy": False, "truncated": True,
                       "present": False}
            print(json.dumps(stamp_schema_version(payload, version=1)))
        else:
            print(f"cctally: no {label} database file present; nothing to drain.")
        return 0

    conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True)
    try:
        conn.execute(f"PRAGMA busy_timeout={timeout}")
        result = _cctally_cache._run_wal_truncate(conn, path, db_label=label)
    finally:
        conn.close()

    if as_json:
        payload = {"db": result.db, "walBytesBefore": result.wal_bytes_before,
                   "walBytesAfter": result.wal_bytes_after,
                   "framesCheckpointed": result.frames_checkpointed,
                   "busy": result.busy, "truncated": result.truncated,
                   "present": True}
        print(json.dumps(stamp_schema_version(payload, version=1)))
    else:
        mb_b = result.wal_bytes_before / (1024 * 1024)
        mb_a = result.wal_bytes_after / (1024 * 1024)
        state = "drained" if result.truncated else ("still busy" if result.busy else "partial")
        print(f"cctally: {result.db} WAL {mb_b:.1f} MB -> {mb_a:.1f} MB "
              f"({result.frames_checkpointed} frames; {state}).")
    return 0 if result.truncated else 3


# VACUUM writes a full fresh copy of the database into a temporary file and then
# swaps it in, so it transiently needs roughly the DB's own size on top of the
# existing file, plus room for the drained WAL. A short busy_timeout keeps a
# contended VACUUM from hanging (F13).
_VACUUM_BUSY_TIMEOUT_MS = 250


def _free_disk_bytes(directory) -> int:
    """Free bytes on the filesystem holding ``directory`` (mockable in tests)."""
    import shutil
    return shutil.disk_usage(str(directory)).free


def _vacuum_required_free_bytes(path) -> int:
    """Conservative free-space floor to VACUUM ``path``: ~2x the DB file plus its
    current WAL sidecar (F13 — the VACUUM temp copy + a WAL/temp margin)."""
    try:
        db_bytes = path.stat().st_size
    except OSError:
        db_bytes = 0
    wal = path.parent / (path.name + "-wal")
    try:
        wal_bytes = wal.stat().st_size
    except OSError:
        wal_bytes = 0
    return 2 * db_bytes + wal_bytes


def _run_vacuum_exclusive(path, label: str) -> int:
    """Checkpoint + VACUUM ``path`` under a real SQLite EXCLUSIVE lock (F13).

    ``locking_mode=EXCLUSIVE`` + a short ``busy_timeout`` make a concurrent
    reader/writer FAIL PROMPTLY (no TOCTOU gap — the exclusion is the DB's own
    lock, which the advisory flocks do not provide against dashboard readers).
    Exit 0 on success, 3 when the DB is in use."""
    conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True)
    try:
        conn.execute(f"PRAGMA busy_timeout={_VACUUM_BUSY_TIMEOUT_MS}")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        before = conn.execute("PRAGMA page_count").fetchone()[0]
        try:
            conn.execute("PRAGMA locking_mode=EXCLUSIVE")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            if "lock" in str(exc).lower() or "busy" in str(exc).lower():
                eprint(
                    f"cctally: {label} is in use — VACUUM needs exclusive access. "
                    f"Stop the dashboard and any other cctally process holding "
                    f"{label}, then retry."
                )
                return 3
            raise
        after = conn.execute("PRAGMA page_count").fetchone()[0]
        freed_mb = max(0, (before - after)) * page_size / (1024 * 1024)
        print(
            f"cctally: {label} reclaimed {freed_mb:.1f} MB "
            f"({before} -> {after} pages)."
        )
        return 0
    finally:
        conn.close()


def _vacuum_one_db(path, label: str, provider_locked: bool) -> int:
    if not path.exists():
        print(f"cctally: no {label} database file present; nothing to reclaim.")
        return 0
    needed = _vacuum_required_free_bytes(path)
    free = _free_disk_bytes(path.parent)
    if free < needed:
        eprint(
            f"cctally: not enough free disk to VACUUM {label}: need ~"
            f"{needed // (1024 * 1024)} MB free, have {free // (1024 * 1024)} MB. "
            f"Free up space and retry."
        )
        return 3
    core = _cctally_core
    try:
        core.APP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # Serialize against the retention prune and concurrent vacuums via the
    # dedicated maintenance flock (F13/F7), then the provider flocks for
    # cache.db. All non-blocking: fail promptly rather than hang.
    conversation_store = path == core.CONVERSATIONS_DB_PATH
    maintenance_path = (
        core.CONVERSATIONS_LOCK_MAINTENANCE_PATH
        if conversation_store else core.CACHE_LOCK_MAINTENANCE_PATH
    )
    maint_fh = open(maintenance_path, "w")
    try:
        try:
            fcntl.flock(maint_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            eprint(
                f"cctally: {label} VACUUM skipped: another maintenance operation "
                f"is running. Retry shortly."
            )
            return 3
        held = []
        try:
            if provider_locked:
                provider_locks = (
                    (
                        (core.CONVERSATIONS_LOCK_PATH, "claude conversations"),
                        (core.CONVERSATIONS_LOCK_CODEX_PATH, "codex conversations"),
                    )
                    if conversation_store else
                    (
                        (core.CACHE_LOCK_PATH, "claude"),
                        (core.CACHE_LOCK_CODEX_PATH, "codex"),
                    )
                )
                for lock_path, lname in provider_locks:
                    fh = open(lock_path, "w")
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except (BlockingIOError, OSError):
                        fh.close()
                        eprint(
                            f"cctally: {label} VACUUM skipped: a {lname} sync is in "
                            f"progress. Stop the dashboard / other cctally "
                            f"processes and retry."
                        )
                        return 3
                    held.append(fh)
            return _run_vacuum_exclusive(path, label)
        finally:
            for fh in held:
                try:
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except OSError:
                    pass
                fh.close()
    finally:
        try:
            fcntl.flock(maint_fh, fcntl.LOCK_UN)
        except OSError:
            pass
        maint_fh.close()


def cmd_db_vacuum(args: argparse.Namespace) -> int:
    """Reclaim disk space via VACUUM after a transcript prune (#313 P3, F13).

    NEVER automatic. Holds the maintenance flock + (for cache.db) the provider
    flocks, then runs a checkpoint + VACUUM under a real SQLite EXCLUSIVE lock so
    a concurrent dashboard reader fails promptly instead of racing. Refuses when
    free disk is below ~2x the file + WAL. Exit 0 on success, 3 when a target is
    in use or disk is short."""
    which = getattr(args, "db", "cache")
    targets = []
    if which in ("cache", "all"):
        targets.append((_cctally_core.CACHE_DB_PATH, "cache.db", True))
    if which in ("conversations", "all"):
        targets.append((
            _cctally_core.CONVERSATIONS_DB_PATH, "conversations.db", True,
        ))
    if which in ("stats", "all"):
        targets.append((_cctally_core.DB_PATH, "stats.db", False))
    overall = 0
    for path, label, provider_locked in targets:
        rc = _vacuum_one_db(path, label, provider_locked)
        if rc != 0:
            overall = rc
    return overall
