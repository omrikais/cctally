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
import sqlite3
import sys
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
        super().__init__(
            f"{db_label} is at version {db_version} but this cctally "
            f"only knows up to {max_known}."
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
    """Register a stats.db migration. Use as @stats_migration("NNN_descriptive_name")."""
    return _make_migration_decorator(_STATS_MIGRATIONS, "stats.db", name)


def cache_migration(name: str):
    """Register a cache.db migration. Use as @cache_migration("NNN_descriptive_name")."""
    return _make_migration_decorator(_CACHE_MIGRATIONS, "cache.db", name)


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


def _run_pending_migrations(
    conn: sqlite3.Connection,
    *,
    registry: list[Migration],
    db_label: str,
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
            ),
            "cache.db": ("session_entries",),
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
            if os.environ.get("CCTALLY_DEBUG"):
                eprint(
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

    Always inserts the schema_migrations marker at the end (inside the
    same transaction) so the gate closes regardless of how many child
    rows were written — empty `session_entries` for a block (real
    users with API/web-only blocks) yields zero child rows but MUST
    still close the gate (regression scenario Q2).
    """
    # Empty-table fast path: with no parent five_hour_blocks rows, this
    # backfill has nothing to do. We still must close the gate so the
    # dispatcher sees us as applied. INSERT OR IGNORE the marker and
    # return (replaces the prior `has_blocks` outer gate from the
    # pre-framework era).
    if not conn.execute("SELECT 1 FROM five_hour_blocks LIMIT 1").fetchone():
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
            ("001_five_hour_block_models_backfill_v1", now_utc_iso()),
        )
        conn.commit()
        return
    now_iso = now_utc_iso()
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

        # Mark migration done — closes the gate even when zero rows
        # were written (empty session_entries / API-only blocks).
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc)
            VALUES (?, ?)
            """,
            ("001_five_hour_block_models_backfill_v1", now_iso),
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
    buckets and inserts the projects-side schema_migrations marker.
    Cleans up orphan child rows defensively before the main loop.
    Marker insert fires regardless of child-row count so the gate
    closes for empty-row backfills too.
    """
    # Empty-table fast path: with no parent five_hour_blocks rows, this
    # backfill has nothing to do. We still must close the gate so the
    # dispatcher sees us as applied. INSERT OR IGNORE the marker and
    # return (replaces the prior `has_blocks` outer gate from the
    # pre-framework era).
    if not conn.execute("SELECT 1 FROM five_hour_blocks LIMIT 1").fetchone():
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
            ("002_five_hour_block_projects_backfill_v1", now_utc_iso()),
        )
        conn.commit()
        return
    now_iso = now_utc_iso()
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

        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc)
            VALUES (?, ?)
            """,
            ("002_five_hour_block_projects_backfill_v1", now_iso),
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

        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc)
            VALUES (?, ?)
            """,
            ("003_merge_5h_block_duplicates_v1", now_utc_iso()),
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
    row, INSERT the marker and return without opening a transaction.

    Spec hook: paired regression test in
    ``tests/test_heal_forked_week_start_date_buckets.py``.
    """
    # Empty-fork fast path. UNION ALL across the three tables; one
    # SELECT 1 / LIMIT 1 short-circuits on the first violator. When
    # zero rows are forked, skip the BEGIN/UPDATE block entirely and
    # just stamp the marker.
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
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) "
            "VALUES (?, ?)",
            ("004_heal_forked_week_start_date_buckets", now_utc_iso()),
        )
        conn.commit()
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

        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc)
            VALUES (?, ?)
            """,
            ("004_heal_forked_week_start_date_buckets", now_utc_iso()),
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
    present the marker still gets stamped — no schema edit needed.
    """
    # Fast-path probe: column already present means a prior run of this
    # migration (or a fresh-install fast-stamp from the dispatcher that
    # already picked up the new live-schema CREATE TABLE) has done the
    # work. Just stamp the marker and return.
    cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(percent_milestones)").fetchall()
    }
    if "reset_event_id" in cols:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) "
            "VALUES (?, ?)",
            ("005_percent_milestones_reset_event_id", now_utc_iso()),
        )
        conn.commit()
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
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) "
            "VALUES (?, ?)",
            ("005_percent_milestones_reset_event_id", now_utc_iso()),
        )
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
    the marker still gets stamped — no schema edit needed.
    """
    # Fast-path probe: column already present means a prior run of this
    # migration (or a fresh-install fast-stamp from the dispatcher that
    # already picked up the new live-schema CREATE TABLE) has done the
    # work. Just stamp the marker and return. The marker INSERT runs in
    # SQLite's implicit transaction (auto-opened by the write, closed by
    # ``commit()`` — same shape as migration 005's fast path); no explicit
    # ``BEGIN`` is needed for a single-statement DML.
    cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(five_hour_milestones)").fetchall()
    }
    if "reset_event_id" in cols:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) "
            "VALUES (?, ?)",
            ("006_five_hour_milestones_reset_event_id", now_utc_iso()),
        )
        conn.commit()
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
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) "
            "VALUES (?, ?)",
            ("006_five_hour_milestones_reset_event_id", now_utc_iso()),
        )
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
    already carries the column (fresh install), stamp the marker and
    return without an ALTER. Simple ADD COLUMN — no UNIQUE constraint
    change, so no rename-recreate-copy needed (contrast migrations
    005 / 006).
    """
    cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(week_reset_events)").fetchall()
    }
    if "observed_pre_credit_pct" in cols:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) "
            "VALUES (?, ?)",
            ("007_observed_pre_credit_pct", now_utc_iso()),
        )
        conn.commit()
        return

    conn.execute("BEGIN")
    try:
        conn.execute(
            "ALTER TABLE week_reset_events "
            "ADD COLUMN observed_pre_credit_pct REAL"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) "
            "VALUES (?, ?)",
            ("007_observed_pre_credit_pct", now_utc_iso()),
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
        cctally's floor is 3.13 per ``__min_python_version__``). The
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
    side-effect that stays in ``open_cache_db``: a future cross-DB migration
    that needs a Codex column on the eager-apply path must revisit that
    exception. The eager-apply path provably never touches Codex (cache 001 +
    the 008/009/010 RO joins are all Claude-side), so the column's absence here
    cannot surface a ``no such column``.
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
            cost_usd_raw        REAL
        );
        CREATE INDEX IF NOT EXISTS idx_entries_timestamp
            ON session_entries(timestamp_utc);
        CREATE INDEX IF NOT EXISTS idx_entries_source
            ON session_entries(source_path);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_dedup
            ON session_entries(msg_id, req_id)
            WHERE msg_id IS NOT NULL AND req_id IS NOT NULL;

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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_files_session_id "
        "ON session_files(session_id)"
    )


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
            # D3 — INSERT OR IGNORE for race safety. Mirrors the
            # convention applied to every other production migration
            # and the matching change to cache migration 001.
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations "
                "(name, applied_at_utc) VALUES (?, ?)",
                (
                    "008_recompute_weekly_cost_snapshots_dedup_fix",
                    now_utc_iso(),
                ),
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

            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations "
                "(name, applied_at_utc) VALUES (?, ?)",
                (
                    "009_recompute_five_hour_blocks_dedup_fix",
                    now_utc_iso(),
                ),
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

            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations "
                "(name, applied_at_utc) VALUES (?, ?)",
                (
                    "010_recompute_percent_milestones_dedup_fix",
                    now_utc_iso(),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        cache_ro.close()


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
        table is non-empty; otherwise inserts the marker and succeeds."""
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='test_failure_trigger'"
        ).fetchone() and conn.execute(
            "SELECT 1 FROM test_failure_trigger LIMIT 1"
        ).fetchone():
            raise RuntimeError("test failure injected")
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
                (_stats_test_name, now_utc_iso()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    _cache_test_seq = len(_CACHE_MIGRATIONS) + 1
    _cache_test_name = f"{_cache_test_seq:03d}_test_cache_migration"

    @cache_migration(_cache_test_name)
    def _test_cache_migration(conn):
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
                (_cache_test_name, now_utc_iso()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


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
        },
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for db_label in ("stats.db", "cache.db"):
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
    in_stats = any(m.name == name_arg for m in _STATS_MIGRATIONS)
    in_cache = any(m.name == name_arg for m in _CACHE_MIGRATIONS)
    if in_stats and in_cache:
        raise RuntimeError(name_arg)
    if in_stats:
        return "stats.db", name_arg, _STATS_MIGRATIONS
    if in_cache:
        return "cache.db", name_arg, _CACHE_MIGRATIONS
    raise LookupError(name_arg)


def _db_path_for_label(db_label: str) -> pathlib.Path:
    if db_label == "stats.db":
        return _cctally_core.DB_PATH
    if db_label == "cache.db":
        return _cctally_core.CACHE_DB_PATH
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
            f"qualify as 'stats.db:{name_arg}' or 'cache.db:{name_arg}'"
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
            f"qualify as 'stats.db:{name_arg}' or 'cache.db:{name_arg}'"
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
