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
- Path constants ``STATS_DB_PATH``, ``CACHE_DB_PATH``,
  ``MIGRATION_ERROR_LOG_PATH``, ``LOG_DIR`` (spec §86–92 — every
  path constant stays so monkeypatched HOME redirects propagate).
- ``open_db`` / ``open_cache_db`` — DB-open primitives that CALL
  the dispatcher; they're the boundary owners, not internal to the
  migration system.
- ``now_utc_iso``, ``parse_iso_datetime``, ``_compute_block_totals``,
  ``eprint``, ``format_local_iso`` — tiny helpers / hot-path entry
  points consumed by migration handlers + cmd_db_status renderers.

§5.6 audit: zero monkeypatch sites on any moved symbol — the
extraction is pure-mechanical. No Option C call-site rewrites
required for test propagation.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
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


# Module-level back-ref shims for the four callables most heavily used
# across migration handlers + cmd_db_* renderers. Each shim resolves
# `sys.modules['cctally'].X` at CALL TIME (not bind time), so
# monkeypatches on cctally's namespace propagate into the moved code
# unchanged. This lets the moved function bodies stay byte-identical
# at every bare-name call site (`now_utc_iso(...)`,
# `parse_iso_datetime(...)`, etc.) without requiring per-function
# `c = _cctally()` boilerplate or `c.X` rewrites at every call site.
#
# Path constants and rarer helpers (`MIGRATION_ERROR_LOG_PATH`,
# `LOG_DIR`, `DB_PATH`, `CACHE_DB_PATH`, `format_local_iso`) are
# accessed via the standard `c = _cctally()` + `c.X` pattern instead
# (call-time lookup so fixture-HOME redirects propagate).
def now_utc_iso(*args, **kwargs):
    return sys.modules["cctally"].now_utc_iso(*args, **kwargs)


def parse_iso_datetime(*args, **kwargs):
    return sys.modules["cctally"].parse_iso_datetime(*args, **kwargs)


def _compute_block_totals(*args, **kwargs):
    return sys.modules["cctally"]._compute_block_totals(*args, **kwargs)


def eprint(*args, **kwargs):
    return sys.modules["cctally"].eprint(*args, **kwargs)


# === BEGIN MOVED REGIONS ===
# Regions below are inserted verbatim from bin/cctally. Bare-name
# references to `now_utc_iso(...)`, `parse_iso_datetime(...)`,
# `_compute_block_totals(...)`, and `eprint(...)` resolve to the shims
# above. Path-constant references (`MIGRATION_ERROR_LOG_PATH`,
# `LOG_DIR`, `DB_PATH`, `CACHE_DB_PATH`) get rewritten to `c.X` form
# with a top-of-function `c = _cctally()` binding inserted.

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
        post-bootstrap) → stamp every migration applied without invoking
        handlers.
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

    # Fresh install: schema_migrations was just CREATE'd this open AND
    # has zero rows post-bootstrap. (If bootstrap renamed pre-existing
    # rows, those rows now appear in `applied`; not a fresh install.)
    fresh_install = (not schema_migrations_existed) and len(applied) == 0

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
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = now_utc_iso()
        one_line_err = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
        indented_tb = "\n".join("  " + line for line in tb.rstrip().splitlines())
        block = f"[{ts}] {name}\n  {one_line_err}\n{indented_tb}\n\n"
        with open(MIGRATION_ERROR_LOG_PATH, "a") as fh:
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
        if not MIGRATION_ERROR_LOG_PATH.exists():
            return
        content = MIGRATION_ERROR_LOG_PATH.read_text()
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
            MIGRATION_ERROR_LOG_PATH.unlink()
            return
        MIGRATION_ERROR_LOG_PATH.write_text("\n\n".join(kept) + "\n\n")
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
    if not MIGRATION_ERROR_LOG_PATH.exists():
        return None
    try:
        content = MIGRATION_ERROR_LOG_PATH.read_text()
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
                    f"See {MIGRATION_ERROR_LOG_PATH}"
                )
        except Exception:
            pass
    return (
        f"⚠ cctally: migration error logged. "
        f"See {MIGRATION_ERROR_LOG_PATH}"
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
            ms_id_placeholders = ",".join(
                "?" * (len(dropped_ids) + 1)
            )
            all_milestones = conn.execute(
                f"SELECT id, percent_threshold, captured_at_utc "
                f"  FROM five_hour_milestones "
                f" WHERE block_id IN ({ms_id_placeholders})",
                [canonical["id"], *dropped_ids],
            ).fetchall()
            by_threshold: dict[int, dict] = {}
            for m in all_milestones:
                t = m["percent_threshold"]
                md = dict(m)
                if (
                    t not in by_threshold
                    or md["captured_at_utc"]
                    < by_threshold[t]["captured_at_utc"]
                ):
                    by_threshold[t] = md
            keep_ids = {m["id"] for m in by_threshold.values()}
            # DELETE non-keepers BEFORE rekeying keepers. Otherwise, when
            # both canonical and a dropped block hold a milestone for the
            # same percent_threshold and the dropped row's milestone is
            # the earlier keeper, UPDATEing it to the canonical key
            # collides with canonical's still-present non-keeper on
            # UNIQUE(five_hour_window_key, percent_threshold), rolling
            # back the migration. After this DELETE the only milestones
            # referencing dropped_keys are the keepers themselves
            # (one per threshold), so the UPDATE loop below is collision-
            # free.
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
            for m in by_threshold.values():
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
            "stats.db": _db_status_for(DB_PATH, _STATS_MIGRATIONS, "stats.db"),
            "cache.db": _db_status_for(CACHE_DB_PATH, _CACHE_MIGRATIONS, "cache.db"),
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
                "log_path": str(MIGRATION_ERROR_LOG_PATH),
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
    if not MIGRATION_ERROR_LOG_PATH.exists():
        return {}
    out: dict[str, str] = {}
    try:
        content = MIGRATION_ERROR_LOG_PATH.read_text()
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
        return DB_PATH
    if db_label == "cache.db":
        return CACHE_DB_PATH
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
