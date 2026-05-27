"""Session-entry cache subsystem (Claude + Codex) for cctally.

Eager I/O sibling: bin/cctally loads this at startup. Holds the
session-entry cache plumbing that every JSONL-reading subcommand
(``daily`` / ``monthly`` / ``weekly`` / ``blocks`` / ``session`` /
``range-cost`` / ``cache-report`` / ``sync-week`` / ``codex-*``) routes
through. Hot path: ``sync_cache`` and ``open_cache_db`` are invoked on
every ``cctally record-usage`` tick via the statusline/hook-tick
pipeline.

Holds:
- ``ProjectKey`` (frozen dataclass) + ``_resolve_project_key`` —
  canonical project bucket identity for the ``project`` subcommand.
- ``_get_codex_sessions_dir`` / ``_discover_codex_session_files`` —
  Codex JSONL discovery primitives.
- ``IngestStats`` / ``CodexIngestStats`` (dataclasses), ``_progress_stderr``
  / ``_progress_codex_stderr`` — ingest progress + per-call telemetry.
- ``_ensure_session_files_row`` — idempotent backfill of
  ``session_files.session_id`` / ``.project_path`` driven by ``sync_cache``.
- ``sync_cache`` / ``sync_codex_cache`` — read-through delta ingest of
  ``~/.claude/projects/**/*.jsonl`` and ``~/.codex/sessions/**/*.jsonl``,
  each gated by an exclusive ``fcntl.flock`` on its own ``.lock`` sibling
  of ``cache.db``.
- ``open_cache_db`` — schema + per-DB migration dispatcher
  (``_run_pending_migrations(_, registry=_CACHE_MIGRATIONS, …)``) +
  WAL/busy-timeout pragmas; safe on corrupt-file recreation because the
  cache is fully re-derivable from JSONL.
- ``iter_entries`` / ``iter_codex_entries`` — in-range SELECT helpers
  returning ``UsageEntry`` / ``CodexEntry`` (defined in
  ``bin/_lib_jsonl.py``).
- ``_collect_entries_direct`` / ``_collect_codex_entries_direct`` /
  ``_direct_parse_claude_session_entries`` — direct-JSONL parse
  fallbacks when cache.db can't be opened or an ingest lock is held.
- ``_JoinedClaudeEntry`` (dataclass) + ``get_claude_session_entries`` —
  cache-first ``LEFT JOIN`` of ``session_entries`` ↔ ``session_files``
  for the ``session`` / ``project`` / share-projects renderers.
- ``get_entries`` / ``get_codex_entries`` — top-level cache-first
  fetches that JSONL-reading commands MUST use rather than touching
  ``open_cache_db`` directly. Transparent fallback on cache-open
  failure or sync lock contention.
- ``cmd_cache_sync`` — entry point for ``cctally cache-sync
  [--source {claude,codex,all}] [--rebuild]``.

What lives in bin/_cctally_core (promoted 2026-05-22, #84):
- Path constants ``APP_DIR``, ``CACHE_DB_PATH``, ``CACHE_LOCK_PATH``,
  ``CACHE_LOCK_CODEX_PATH``. Moved bodies read these via call-time
  ``_cctally_core.X`` and tests patch via
  ``monkeypatch.setattr(_cctally_core, "X", v)`` (or the conftest
  ``redirect_paths()`` helper). The legacy
  ``setitem(ns, "CACHE_DB_PATH", …)`` pattern is forbidden by
  ``test_no_old_style_test_patches_for_promoted_globals``.

What stays in bin/cctally:
- ``CODEX_SESSIONS_DIR`` — out of scope for #84; still read via the
  ``c = _cctally()`` call-time accessor (spec §5.5).
- ``_sum_cost_for_range`` — sits at the cache↔report boundary; 6+
  callers outside cache (forecast, weekly, report, project, doctor),
  so the directive keeps it on the bin/cctally side.
- ``CacheModelBreakdown`` / ``CacheRow`` and the broader cache-report
  surface — that's Phase F territory, not the ingest/read primitives.
- ``_decode_escaped_cwd``, ``_discover_session_files``,
  ``_get_claude_data_dirs``, ``eprint`` — small shared helpers (JSONL
  discovery + stderr formatter) consumed by many non-cache paths.
  Routed through module-level callable shims (see below) so moved
  code keeps its bare-name call shape and monkeypatches on bin/cctally
  propagate via call-time ``sys.modules['cctally']`` lookup.

Direct sibling loads at module-load time (acyclic — both are pure leaves
in the sibling graph):
- ``_lib_jsonl`` for ``UsageEntry``, ``CodexEntry``, ``_CodexIterState``,
  ``_iter_jsonl_entries_with_offsets``, ``_iter_codex_jsonl_entries_with_offsets``,
  ``_parse_usage_entries``.
- ``_cctally_db`` for ``add_column_if_missing``, ``_run_pending_migrations``,
  ``_CACHE_MIGRATIONS``. Loading ``_cctally_db`` here is a no-op when
  bin/cctally already imported it at startup (the eager-load block
  there fires first), but the direct load makes this sibling
  self-contained for tests that load ``_cctally_cache`` in isolation.

§5.6 audit: zero monkeypatch sites on any moved symbol. The Section
5.6 audit grep on the candidate-symbol inventory (``sync_cache``,
``sync_codex_cache``, ``open_cache_db``, ``iter_entries``,
``get_entries``, ``get_claude_session_entries``, ``get_codex_entries``,
``_resolve_project_key``, ``ProjectKey``, ``IngestStats``,
``CodexIngestStats``, ``_JoinedClaudeEntry``, ``_ensure_session_files_row``,
``_discover_codex_session_files``, ``_get_codex_sessions_dir``,
``cmd_cache_sync``, ``_progress_stderr``, ``_progress_codex_stderr``,
``_collect_entries_direct``, ``_collect_codex_entries_direct``,
``_direct_parse_claude_session_entries``, ``iter_codex_entries``)
returns no ``monkeypatch.setattr/setitem`` sites — the only test-side
hits are ``ns["X"](...)`` direct-callers (e.g.
``tests/test_share_top_projects.py`` patches ``get_claude_session_entries``
via ``monkeypatch.setitem(ns, ...)`` on bin/cctally's namespace, which
propagates through the eager re-export of the same name in bin/cctally).
Pure-mechanical extraction.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib.util as _ilu
import json
import os
import pathlib
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Any, Callable


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17-cctally-core-kernel-extraction.md §3.3: kernel symbols
# (Z-leaf + Z-mid) import from _cctally_core. The legacy shim function
# for ``eprint`` is deleted.
import _cctally_core
from _cctally_core import eprint


# Module-level back-ref shims for the three out-of-scope JSONL/project
# discovery helpers that STAY in bin/cctally per spec §3.7. Each shim
# resolves ``sys.modules['cctally'].X`` at CALL TIME (not bind time),
# so monkeypatches on cctally's namespace propagate into the moved code
# unchanged.
def _decode_escaped_cwd(*args, **kwargs):
    return sys.modules["cctally"]._decode_escaped_cwd(*args, **kwargs)


def _discover_session_files(*args, **kwargs):
    return sys.modules["cctally"]._discover_session_files(*args, **kwargs)


def _get_claude_data_dirs(*args, **kwargs):
    return sys.modules["cctally"]._get_claude_data_dirs(*args, **kwargs)


# Direct sibling loads at module-load time. Both targets are
# self-contained: ``_lib_jsonl`` is a pure leaf (stdlib-only), and
# ``_cctally_db`` registers its three production migration handlers at
# import time — those decorators are idempotent across re-imports
# because the framework's ``sys.modules`` cache means each handler
# registers exactly once per sibling lifetime.
def _load_lib(name: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    p = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    spec = _ilu.spec_from_file_location(name, p)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_lib_jsonl = _load_lib("_lib_jsonl")
UsageEntry = _lib_jsonl.UsageEntry
CodexEntry = _lib_jsonl.CodexEntry
_CodexIterState = _lib_jsonl._CodexIterState
_iter_jsonl_entries_with_offsets = _lib_jsonl._iter_jsonl_entries_with_offsets
_iter_codex_jsonl_entries_with_offsets = _lib_jsonl._iter_codex_jsonl_entries_with_offsets
_parse_usage_entries = _lib_jsonl._parse_usage_entries
_should_replace = _lib_jsonl._should_replace

_cctally_db_sib = _load_lib("_cctally_db")
add_column_if_missing = _cctally_db_sib.add_column_if_missing
_run_pending_migrations = _cctally_db_sib._run_pending_migrations
_CACHE_MIGRATIONS = _cctally_db_sib._CACHE_MIGRATIONS


# === BEGIN MOVED REGIONS ===
# Path constants APP_DIR / CACHE_DB_PATH / CACHE_LOCK_PATH /
# CACHE_LOCK_CODEX_PATH live in _cctally_core (promoted 2026-05-22, #84);
# moved bodies read them via call-time ``_cctally_core.X`` and tests
# patch via ``monkeypatch.setattr(_cctally_core, "X", v)``.
# CODEX_SESSIONS_DIR stays in bin/cctally (out of scope for #84) and is
# still accessed via the ``c = _cctally()`` call-time accessor.

# === Region 1: ProjectKey + _resolve_project_key (was bin/cctally:1994-2069) ===


@dataclass(frozen=True)
class ProjectKey:
    """Canonical project identity for the `project` subcommand.

    Equality and hash are defined over `bucket_path` only — this is
    the canonical bucket identifier. `display_key` is the user-facing
    label and may be augmented later (e.g. basename-collision
    disambiguation) without breaking aggregation.
    """
    bucket_path: str
    display_key: str = field(compare=False)
    git_root: str | None = field(compare=False)
    is_unknown: bool = field(default=False, compare=False)
    is_no_git: bool = field(default=False, compare=False)


def _resolve_project_key(
    project_path: str | None,
    mode: str,                      # "git-root" | "full-path"
    cache: dict[str, ProjectKey],
) -> ProjectKey:
    """Resolve a raw project_path to its ProjectKey.

    Walks parents looking for `.git` (file or dir) to find the canonical
    git-root. Non-git paths fall back to the normalized path. NULL input
    becomes a literal `(unknown)` bucket.
    """
    if project_path is None:
        return ProjectKey(
            bucket_path="(unknown)",
            display_key="(unknown)",
            git_root=None,
            is_unknown=True,
        )

    if mode == "full-path":
        normalized = os.path.realpath(os.path.expanduser(project_path))
        key = cache.get(normalized)
        if key is not None:
            return key
        key = ProjectKey(
            bucket_path=normalized,
            display_key=project_path,   # raw, so user sees what they typed
            git_root=None,
        )
        cache[normalized] = key
        return key

    normalized = os.path.realpath(os.path.expanduser(project_path))
    cached = cache.get(normalized)
    if cached is not None:
        return cached

    home = os.path.expanduser("~")
    cur = normalized
    while True:
        if cur == home or cur == "/" or os.path.dirname(cur) == cur:
            break
        if os.path.exists(os.path.join(cur, ".git")):
            key = ProjectKey(
                bucket_path=cur,
                display_key=os.path.basename(cur) or cur,
                git_root=cur,
            )
            cache[normalized] = key
            return key
        cur = os.path.dirname(cur)

    key = ProjectKey(
        bucket_path=normalized,
        display_key=os.path.basename(project_path) or project_path,
        git_root=None,
        is_no_git=True,
    )
    cache[normalized] = key
    return key


# === Region 2: Codex sessions-dir helpers (was bin/cctally:2072-2099) ===


def _get_codex_sessions_dir() -> pathlib.Path | None:
    """Return the Codex sessions directory if present, else None."""
    c = _cctally()
    if c.CODEX_SESSIONS_DIR.is_dir():
        return c.CODEX_SESSIONS_DIR
    return None


def _discover_codex_session_files(
    range_start: dt.datetime,
) -> list[pathlib.Path]:
    """Glob each $CODEX_HOME session root's **/*.jsonl, mtime >= range_start.

    Iterates _cctally()._codex_session_roots() (multi-root). The "none found"
    notice fires ONLY when there are zero session-root directories at all (the
    multi-root analogue of the old single-dir-missing check) — NOT when roots
    exist but the mtime filter leaves the set empty (that stays silent, as
    today, so narrow-range queries gain no new stderr).
    """
    roots = _cctally()._codex_session_roots()
    if not roots:
        eprint("[codex] no Codex session directory found")
        return []
    start_ts = range_start.timestamp()
    seen: set[pathlib.Path] = set()
    result: list[pathlib.Path] = []
    for root in roots:
        for jp in root.glob("**/*.jsonl"):
            if jp in seen:
                continue
            seen.add(jp)
            if not jp.is_file():
                continue
            try:
                mtime = jp.stat().st_mtime
            except OSError:
                continue
            if mtime < start_ts:
                continue
            result.append(jp)
    return result


# === Region 3: IngestStats + Claude ingest path (was bin/cctally:2102-2400) ===


@dataclass
class IngestStats:
    files_total: int = 0
    files_processed: int = 0
    files_skipped_unchanged: int = 0
    files_reset_truncated: int = 0
    # Count of session_entries rows written by this sync — both genuinely-
    # new INSERTs and ccusage-parity ON CONFLICT DO UPDATE replacements
    # (the dedup tiebreaker swaps a streaming-intermediate row for the
    # post-stream finalization). SQLite's `total_changes` counter
    # increments on both, so this field is "rows changed", not "rows
    # newly inserted". Pre-dedup builds used INSERT OR IGNORE where
    # conflicts did NOT bump the counter; the name change preserves the
    # observability metric without misrepresenting UPSERT updates as
    # new inserts.
    rows_changed: int = 0
    lock_contended: bool = False


def _progress_stderr(stats: IngestStats, *, force: bool = False) -> None:
    """Default stderr progress callback. Every 200 files or when forced."""
    if not force and stats.files_processed % 200 != 0:
        return
    eprint(
        f"[cache-sync] {stats.files_processed}/{stats.files_total} files, "
        f"{stats.rows_changed} rows changed"
    )


def _ensure_session_files_row(conn: sqlite3.Connection, source_path: str) -> None:
    """Populate session_files.session_id and .project_path for this JSONL.

    Idempotent and safe to call every sync: uses UPSERT with COALESCE on the
    two new columns so already-populated rows are not overwritten. Scans the
    file from offset 0 looking for the first line carrying `sessionId`; also
    captures `cwd` for `project_path` when present. Falls back to filename
    UUID + decoded-escaped-directory when those fields are absent.

    Does not touch the delta-resume columns (size_bytes, mtime_ns,
    last_byte_offset, last_ingested_at) — those belong to the existing
    sync_cache path.

    No-op on files already populated on both new columns; cheap SELECT check
    up front to avoid re-reading the JSONL when the row is already complete.
    """
    # Quick check: skip if both columns already populated.
    existing = conn.execute(
        "SELECT session_id, project_path FROM session_files WHERE path = ?",
        (source_path,),
    ).fetchone()
    if existing is not None and existing[0] is not None and existing[1] is not None:
        return

    session_id: str | None = None
    cwd: str | None = None
    try:
        with open(source_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if session_id is None:
                    sid = obj.get("sessionId")
                    if isinstance(sid, str) and sid:
                        session_id = sid
                if cwd is None:
                    cwd_val = obj.get("cwd")
                    if isinstance(cwd_val, str) and cwd_val:
                        cwd = cwd_val
                if session_id is not None and cwd is not None:
                    break
    except OSError:
        return  # unreadable; retry on next sync

    # Fallbacks.
    if session_id is None:
        stem = os.path.splitext(os.path.basename(source_path))[0]
        session_id = stem
        # One-shot stderr warning per process per path — match the codex-side
        # pattern (grep for `filename_session_id_warned` for inspiration).
        # Keep simple: unconditional warning. Sync is rare, noise is low.
        print(
            f"Warning: no sessionId in {source_path}; "
            f"falling back to filename UUID {session_id}",
            file=sys.stderr,
        )
    if cwd is None:
        parent = os.path.basename(os.path.dirname(source_path))
        cwd = _decode_escaped_cwd(parent)

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO session_files (
            path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at,
            session_id, project_path
        ) VALUES (?, 0, 0, 0, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            session_id   = COALESCE(session_files.session_id,   excluded.session_id),
            project_path = COALESCE(session_files.project_path, excluded.project_path)
        """,
        (source_path, now_iso, session_id, cwd),
    )
    # Commit per-call so the write lock is released before the caller's
    # subsequent JSONL read+parse. Leaving the implicit transaction open
    # across the per-file loop would both hold a writer lock across reads
    # and risk losing updates if a file-loop iteration `continue`s without
    # hitting the caller's own commit.
    conn.commit()


def sync_cache(
    conn: sqlite3.Connection,
    *,
    progress: Callable[[IngestStats], None] | None = None,
    rebuild: bool = False,
) -> IngestStats:
    """Read-through delta ingest. Acquires an exclusive fcntl.flock; if
    another process holds it, returns immediately with lock_contended=True
    and the caller should proceed with whatever data is already cached.

    When `rebuild=True`, clears the cached rows AFTER acquiring the lock
    so a lost race does not wipe a cache another process is actively
    populating. If the lock is contended on a rebuild, the cache is left
    untouched and the caller sees `lock_contended=True`.
    """
    stats = IngestStats()
    c = _cctally()
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _cctally_core.CACHE_LOCK_PATH.touch()

    lock_fh = open(_cctally_core.CACHE_LOCK_PATH, "w")
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            eprint("[cache] sync already in progress; using existing cache")
            stats.lock_contended = True
            return stats

        # Walk-complete sentinel gating (cctally-dev#93, D5b/D6b). Capture
        # whether cache 001 was already applied at the moment this sync
        # acquired the lock. The end-of-loop marker write is gated on this so
        # a walk whose baseline predates the 001 wipe (the "straddle" run)
        # withholds the marker — it cannot vouch for a cache 001 wiped
        # underneath it. On the normal first-upgrade flow open_cache_db runs
        # the dispatcher (001 applies in-process) BEFORE sync_cache is ever
        # called, so this is True and the marker is written as expected. If
        # schema_migrations doesn't exist yet, treat as not-applied (False).
        try:
            applied_at_start = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name='001_dedup_highest_wins'"
            ).fetchone() is not None
        except sqlite3.OperationalError:
            applied_at_start = False

        # Tracks whether every file in this walk was either ingested cleanly
        # or confirmed-current. Any per-file error-skip (stat/read failure or
        # a DB error that rolls back + continues) flips it False so the marker
        # is withheld — an incomplete walk must not look complete. The
        # unchanged-file early-exit (`size == prev_size`) does NOT flip it: a
        # confirmed-current file still counts as walked.
        walk_clean = True

        if rebuild:
            # Clear INSIDE the lock — a concurrent rebuild that lost the
            # race would otherwise have wiped this cache before bailing,
            # leaving the user with empty state. Done before the existing
            # SELECT so the subsequent delta-detection logic sees an
            # empty baseline.
            conn.execute("DELETE FROM session_entries")
            conn.execute("DELETE FROM session_files")
            # Clear the walk-complete sentinel atomically with the wipe
            # (cctally-dev#93, D5/D2): a stale "complete" marker must never
            # survive a destructive rebuild. The end-of-loop write below
            # re-establishes it only after this rebuild's clean walk.
            conn.execute("DELETE FROM cache_meta WHERE key='claude_ingest_walk_complete'")
            conn.commit()
            eprint("[cache-sync] rebuild: cleared Claude cached entries")

        claude_dirs = _get_claude_data_dirs()
        paths: list[pathlib.Path] = []
        for claude_dir in claude_dirs:
            for jp in (claude_dir / "projects").glob("**/*.jsonl"):
                if jp.is_file():
                    paths.append(jp)
        stats.files_total = len(paths)

        # This SELECT does NOT open an implicit transaction (Python's
        # sqlite3 module only BEGINs on DML). Do NOT add any INSERT/
        # UPDATE/DELETE/REPLACE statement between here and the per-file
        # loop below — the read+parse inside that loop must run with
        # zero cache.db write lock held.
        existing = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT path, size_bytes, mtime_ns, last_byte_offset FROM session_files"
            )
        }

        # Orphaned-tracked-file detection (cctally-dev#93 review). A path
        # tracked in session_files (with data already ingested) but no
        # longer present on disk leaves orphaned session_entries rows that
        # the per-file loop below never visits — it iterates only on-disk
        # `paths`. sync_cache deliberately does NOT prune those orphans
        # in-place: a deleted file shares the truncation hazard (under the
        # sticky source_path dedup a surviving file may carry the same
        # (msg_id, req_id) yet keep its size_bytes, so a per-orphan DELETE
        # could drop a row the survivor still owns without re-ingesting
        # it), and a blanket full-reset would wrongly fire on the
        # legitimate "cache seeded with synthetic source paths" fixture
        # pattern. Instead we INVALIDATE the walk-complete marker: an
        # orphaned cache no longer faithfully mirrors disk, so it is — by
        # the marker's own definition — not a complete walk. We must
        # actively DELETE any marker a PRIOR clean walk left behind (not
        # merely withhold THIS run's end-of-loop rewrite — that rewrite is
        # gated on walk_clean, but a stale marker from a previous sync
        # would otherwise survive and keep vouching for completeness).
        # Setting walk_clean=False additionally suppresses the end-of-loop
        # rewrite so the marker stays absent for this run. With the marker
        # gone the upgrade gate DEFERs the 008/009/010 recomputes (rather
        # than certifying aggregates that still include data from files no
        # longer on disk); the operator clears the orphans by running
        # `cache-sync --rebuild` (the documented re-derive path), which
        # re-establishes the marker. Only paths whose row carried ingested
        # bytes (size_bytes > 0) count — a size_bytes=0 row holds no
        # session_entries, so its absence leaves no orphan. The DELETE +
        # commit lands BEFORE the per-file read+parse loop, so no write
        # lock is held into that loop (same discipline as the truncation
        # escalation just below).
        on_disk_paths = {str(jp) for jp in paths}
        orphaned_tracked_paths = [
            p for p, (size_bytes, _, _) in existing.items()
            if size_bytes and p not in on_disk_paths
        ]
        if orphaned_tracked_paths:
            eprint(
                f"[cache] {len(orphaned_tracked_paths)} tracked file(s) no "
                f"longer on disk; invalidating walk-complete marker "
                f"(run `cache-sync --rebuild` to prune orphaned entries)"
            )
            conn.execute(
                "DELETE FROM cache_meta WHERE key='claude_ingest_walk_complete'"
            )
            conn.commit()
            walk_clean = False  # orphaned rows -> cache doesn't mirror disk (D5a)

        # Pre-scan for any truncation among tracked files. Under the
        # ccusage-parity ON CONFLICT DO UPDATE, source_path is PINNED to
        # whichever file first inserted a (msg_id, req_id) row (see U1
        # in this file). Later UPSERTs from a DIFFERENT file may have
        # updated the token columns on that row while leaving source_path
        # pointing at the original (now possibly truncated) file. A
        # naive per-file truncation path then deletes by source_path and
        # loses data the other file is still carrying — but that other
        # file's `size_bytes` is unchanged, so the per-file early-exit
        # at `if size == prev_size: continue` skips its re-ingest.
        #
        # Escalation: when any file's size has shrunk, drop the entire
        # session_entries cache and force every file to re-ingest from
        # offset 0. The cache is fully re-derivable, this is rare (only
        # on JSONL rotation / manual edits), and it sidesteps the
        # per-key contributing-file bookkeeping that would otherwise be
        # required. The lock is already held, so this is atomic with
        # the subsequent per-file ingest.
        truncated_paths: set[str] = set()
        for jp in paths:
            prev = existing.get(str(jp))
            if prev is None:
                continue
            try:
                st = jp.stat()
            except OSError:
                continue
            if st.st_size < prev[0]:
                truncated_paths.add(str(jp))

        if truncated_paths:
            eprint(
                f"[cache-sync] truncation detected on {len(truncated_paths)} "
                f"file(s) — re-ingesting all files (safe under ccusage-parity "
                f"dedup)"
            )
            conn.execute("DELETE FROM session_entries")
            # Clear the walk-complete sentinel atomically with the truncation
            # full-reset (cctally-dev#93, D5/D2): the cache is being wiped, so
            # any "complete" marker is now stale. The end-of-loop write below
            # re-establishes it only after this run's clean re-ingest walk.
            conn.execute("DELETE FROM cache_meta WHERE key='claude_ingest_walk_complete'")
            # Crash-safety: also clear session_files's size/offset tracking
            # so a partial-state recovery on the NEXT sync forces every
            # file's per-file branch to take the fresh-ingest path. Without
            # this, if the process is killed (kill -9, power loss) between
            # this DELETE commit and the per-file re-ingest commits below,
            # the next sync would only re-detect the originally-truncated
            # file(s); other files still have matching size_bytes and the
            # `if size == prev_size: continue` early-exit would leave them
            # missing from session_entries until file size changes or an
            # operator runs `cache-sync --rebuild`. UPDATE (not DELETE)
            # preserves session_id / project_path columns lazy-backfilled
            # by _ensure_session_files_row (used by the `session`
            # subcommand's JOIN).
            conn.execute(
                "UPDATE session_files SET size_bytes = 0, last_byte_offset = 0"
            )
            conn.commit()
            stats.files_reset_truncated += len(truncated_paths)
            # Force every file to re-ingest from offset 0: clearing the
            # `existing` map makes `prev is None` true downstream, so the
            # per-file branch takes the fresh-ingest path (start_offset=0,
            # truncated=False since we already wiped the table above —
            # avoids a redundant per-file DELETE that would be a no-op).
            existing = {}

        for jp in paths:
            path_str = str(jp)
            # Backfill session_id/project_path for A2 `session` subcommand.
            # Idempotent upsert that preserves delta-resume columns.
            # Placed at the top so unchanged files (early-continue below) are
            # still covered. The downstream INSERT for session_files preserves
            # the two new columns via an explicit column list so this backfill
            # is not clobbered by delta-resume writes.
            _ensure_session_files_row(conn, path_str)
            try:
                st = jp.stat()
            except OSError as exc:
                eprint(f"[cache] stat failed for {jp}: {exc}")
                walk_clean = False  # skipped a file without ingesting (D5a)
                continue

            size = st.st_size
            mtime_ns = st.st_mtime_ns
            prev = existing.get(path_str)
            start_offset = 0
            truncated = False
            if prev is not None:
                # mtime_ns is stored in session_files for diagnostics but
                # intentionally NOT consulted for delta detection — size
                # is the only signal (Claude Code's JSONL sessions are
                # strictly append-only, so a size change is sufficient
                # and mtime is prone to clock-skew false-positives).
                prev_size, _, prev_offset = prev
                if size == prev_size:
                    stats.files_skipped_unchanged += 1
                    continue
                if size > prev_size:
                    start_offset = prev_offset
                else:
                    truncated = True
                    start_offset = 0

            # Read + parse is a pure read; do it OUTSIDE the write transaction
            # so a slow JSONL doesn't hold a SQLite lock.
            rows: list[tuple[Any, ...]] = []
            final_offset = start_offset
            try:
                with open(jp, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(start_offset)
                    for offset, entry, msg_id, req_id in _iter_jsonl_entries_with_offsets(fh, str(jp)):
                        usage = entry.usage
                        inp = int(usage.get("input_tokens", 0) or 0)
                        out = int(usage.get("output_tokens", 0) or 0)
                        cc = int(usage.get("cache_creation_input_tokens", 0) or 0)
                        cr = int(usage.get("cache_read_input_tokens", 0) or 0)
                        extras = {
                            k: v for k, v in usage.items()
                            if k not in (
                                "input_tokens", "output_tokens",
                                "cache_creation_input_tokens",
                                "cache_read_input_tokens",
                            )
                        }
                        rows.append((
                            path_str,
                            offset,
                            entry.timestamp.astimezone(dt.timezone.utc).isoformat(),
                            entry.model,
                            msg_id,
                            req_id,
                            inp, out, cc, cr,
                            json.dumps(extras, sort_keys=True) if extras else None,
                            entry.cost_usd,
                        ))
                    final_offset = fh.tell()
            except OSError as exc:
                eprint(f"[cache] could not read {jp}: {exc}")
                walk_clean = False  # skipped a file without ingesting (D5a)
                continue

            # Python's sqlite3 module starts an implicit transaction on the
            # first DML statement and commits on conn.commit(). We do NOT
            # call "BEGIN IMMEDIATE" ourselves — that would error with
            # "cannot start a transaction within a transaction" if a prior
            # statement already opened one. DELETE + INSERTs + UPDATE happen
            # atomically in a single commit.
            try:
                if truncated:
                    conn.execute(
                        "DELETE FROM session_entries WHERE source_path = ?",
                        (path_str,),
                    )
                    stats.files_reset_truncated += 1
                if rows:
                    before = conn.total_changes
                    # ccusage-parity ON CONFLICT DO UPDATE: higher-token total
                    # wins on conflict; speed-set breaks ties. The partial
                    # UNIQUE index `idx_entries_dedup` restricts the conflict
                    # target to (msg_id IS NOT NULL AND req_id IS NOT NULL),
                    # so the WHERE clause on the conflict target MUST repeat
                    # that predicate verbatim — bare `ON CONFLICT(msg_id,
                    # req_id)` raises OperationalError. NULL-keyed rows fall
                    # through to a plain INSERT, unchanged.
                    #
                    # `source_path` is INTENTIONALLY OMITTED from the DO
                    # UPDATE SET clause: it stays pinned to whichever JSONL
                    # FIRST INSERTed the (msg_id, req_id) row. The
                    # downstream `LEFT JOIN session_files ON sf.path =
                    # se.source_path` uses source_path to attribute tokens
                    # to a `project_path`. If a later UPSERT from a
                    # different file flipped source_path, the row's
                    # project attribution would move with the winner —
                    # `cctally project` would mis-aggregate. Sticky
                    # source_path matches pre-dedup INSERT OR IGNORE
                    # behavior and the operator's mental model.
                    # (`line_offset` is similarly sticky for the same
                    # reason — the offset only makes sense within the
                    # file that originally wrote the row.)
                    conn.executemany(
                        """INSERT INTO session_entries
                           (source_path, line_offset, timestamp_utc, model,
                            msg_id, req_id, input_tokens, output_tokens,
                            cache_create_tokens, cache_read_tokens,
                            usage_extra_json, cost_usd_raw)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(msg_id, req_id)
                           WHERE msg_id IS NOT NULL AND req_id IS NOT NULL
                           DO UPDATE SET
                               timestamp_utc = excluded.timestamp_utc,
                               model = excluded.model,
                               input_tokens = excluded.input_tokens,
                               output_tokens = excluded.output_tokens,
                               cache_create_tokens = excluded.cache_create_tokens,
                               cache_read_tokens = excluded.cache_read_tokens,
                               usage_extra_json = excluded.usage_extra_json,
                               cost_usd_raw = excluded.cost_usd_raw
                           WHERE
                               (excluded.input_tokens + excluded.output_tokens
                                + excluded.cache_create_tokens + excluded.cache_read_tokens)
                               >
                               (session_entries.input_tokens + session_entries.output_tokens
                                + session_entries.cache_create_tokens + session_entries.cache_read_tokens)
                            OR (
                               (excluded.input_tokens + excluded.output_tokens
                                + excluded.cache_create_tokens + excluded.cache_read_tokens)
                               =
                               (session_entries.input_tokens + session_entries.output_tokens
                                + session_entries.cache_create_tokens + session_entries.cache_read_tokens)
                               AND json_extract(excluded.usage_extra_json, '$.speed') IS NOT NULL
                               AND json_extract(session_entries.usage_extra_json, '$.speed') IS NULL
                            )""",
                        rows,
                    )
                    stats.rows_changed += conn.total_changes - before
                # UPSERT preserves session_id / project_path columns populated
                # by _ensure_session_files_row at the top of this loop. A plain
                # INSERT OR REPLACE would wipe them on every changed-file sync.
                conn.execute(
                    """INSERT INTO session_files
                       (path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET
                           size_bytes       = excluded.size_bytes,
                           mtime_ns         = excluded.mtime_ns,
                           last_byte_offset = excluded.last_byte_offset,
                           last_ingested_at = excluded.last_ingested_at""",
                    (
                        path_str, size, mtime_ns, final_offset,
                        dt.datetime.now(dt.timezone.utc).isoformat(),
                    ),
                )
                conn.commit()
                stats.files_processed += 1
            except sqlite3.DatabaseError as exc:
                eprint(f"[cache] db error on {jp}: {exc}")
                conn.rollback()
                walk_clean = False  # rolled back this file without ingesting (D5a)
                continue

            if progress is not None:
                progress(stats)

        if progress is not None:
            progress(stats)

        # Walk-complete sentinel write (cctally-dev#93, D5a). Still inside the
        # held fcntl lock, before the finally-unlock. Only when the entire walk
        # was clean AND cache 001 was already applied at the start of this run
        # (D5b): an unclean walk or a straddle run must not vouch for cache
        # completeness. A lock-contended sync returned early above and never
        # reaches here. Presence (not the timestamp) is the gate signal; the
        # value stores the completion instant for doctor/debugging.
        if walk_clean and applied_at_start:
            conn.execute(
                "INSERT INTO cache_meta(key, value) "
                "VALUES('claude_ingest_walk_complete', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (dt.datetime.now(dt.timezone.utc).isoformat(),),
            )
            conn.commit()
        return stats
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()


def iter_entries(
    conn: sqlite3.Connection,
    range_start: dt.datetime,
    range_end: dt.datetime,
    *,
    project: str | None = None,
) -> list[UsageEntry]:
    """Return cached UsageEntry rows whose timestamp falls in [range_start,
    range_end]. Optional `project` filters by the project slug (directory
    name under `<claude>/projects/`). Drop-in replacement for the old
    `_discover_session_files` + `_parse_usage_entries` loop; dedup is
    enforced at write time by the UNIQUE(msg_id, req_id) index.
    """
    start_iso = range_start.astimezone(dt.timezone.utc).isoformat()
    end_iso = range_end.astimezone(dt.timezone.utc).isoformat()

    sql = (
        "SELECT timestamp_utc, model, input_tokens, output_tokens, "
        "cache_create_tokens, cache_read_tokens, usage_extra_json, "
        "cost_usd_raw, source_path "
        "FROM session_entries "
        "WHERE timestamp_utc >= ? AND timestamp_utc <= ?"
    )
    params: list[Any] = [start_iso, end_iso]
    if project is not None:
        # Escape LIKE wildcards (_ matches any single char, % matches any
        # string). The old glob-based discovery matched project names
        # literally; preserve that semantics so e.g. "foo_bar" doesn't
        # also match "fooxbar".
        escaped = (
            project.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        )
        sql += r" AND source_path LIKE ? ESCAPE '\'"
        params.append(f"%/projects/{escaped}/%")
    sql += " ORDER BY timestamp_utc ASC"

    entries: list[UsageEntry] = []
    for row in conn.execute(sql, params):
        usage: dict[str, Any] = {
            "input_tokens":                row[2],
            "output_tokens":               row[3],
            "cache_creation_input_tokens": row[4],
            "cache_read_input_tokens":     row[5],
        }
        if row[6]:
            # Safe because sync_cache strips the four token keys from
            # extras before storing them in usage_extra_json. If that
            # write-side invariant ever changes, extras could shadow
            # the int-normalized token columns.
            usage.update(json.loads(row[6]))
        entries.append(UsageEntry(
            timestamp=dt.datetime.fromisoformat(row[0]),
            model=row[1],
            usage=usage,
            cost_usd=row[7],
            source_path=row[8],
        ))
    return entries


def _collect_entries_direct(
    range_start: dt.datetime,
    range_end: dt.datetime,
    *,
    project: str | None = None,
) -> list[UsageEntry]:
    """Legacy direct-parse fallback used when the cache DB can't be opened.

    Uses the ccusage-parity dict-keyed accumulator: dedup-keyed entries
    live in `dedupe_map` and are tiebroken via `_should_replace` (higher
    token total wins, speed-set breaks ties). Entries with NULL msg_id or
    req_id bypass the map and land verbatim — partial UNIQUE index on the
    cache mirrors this behavior. Flattened + sorted once at the end.
    """
    files = _discover_session_files(range_start, project=project)
    dedupe_map: dict[str, UsageEntry] = {}
    no_key: list[UsageEntry] = []
    for fp in files:
        no_key.extend(
            _parse_usage_entries(
                fp, range_start, range_end, dedupe_map=dedupe_map,
            )
        )
    all_entries = list(dedupe_map.values()) + no_key
    all_entries.sort(key=lambda e: e.timestamp)
    return all_entries


# === Region 4: _JoinedClaudeEntry + get_claude_session_entries (was bin/cctally:2478-2668) ===


@dataclass
class _JoinedClaudeEntry:
    """session_entries row LEFT JOIN session_files metadata.

    Row shape returned by `get_claude_session_entries`. `session_id` and
    `project_path` are both nullable — a LEFT JOIN preserves entries whose
    `session_files` metadata has not yet been backfilled by sync_cache's
    `_ensure_session_files_row` hook. The aggregator (Task 19) handles
    `session_id is None` by falling back to the filename UUID and emitting
    a one-shot warning.
    """
    timestamp: dt.datetime
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    source_path: str
    session_id: str | None
    project_path: str | None
    # Raw `costUSD` from the JSONL entry when present (None otherwise).
    # Honored by downstream aggregators so `cache-report --by-session`
    # reconciles with daily/range-cost paths that already pass
    # `entry.cost_usd` into `_calculate_entry_cost`.
    cost_usd: float | None = None
    # Non-token `usage` extras (parsed `usage_extra_json`) — notably
    # `speed`, which `_aggregate_buckets` reads to render `<model>-fast`.
    # `iter_entries` merges these into its `UsageEntry.usage`; the joined
    # path must carry them too so `_usage_entry_from_joined` can restore
    # them (else `daily -i`/`-p` lose fast-tier model labels). None when
    # the row has no extras.
    usage_extra: dict | None = None


def get_claude_session_entries(
    range_start: dt.datetime,
    range_end: dt.datetime,
    *,
    project: str | None = None,
    skip_sync: bool = False,
) -> list[_JoinedClaudeEntry]:
    """Fetch in-range Claude entries joined to per-file metadata.

    Executes a LEFT JOIN between `session_entries` and `session_files`
    (PK column `path`, NOT `source_path`) so rows still appear when the
    Task 16 backfill of `session_id` / `project_path` has not yet
    completed for a given file. Mirrors `get_entries`' cache-first
    pattern: open the cache DB, run `sync_cache` for delta ingest +
    metadata backfill, then query; fall back to a direct JSONL parse
    on cache open failure or lock contention.

    `project`, when set, matches against the escaped project directory
    name under `<claude>/projects/` via `source_path LIKE %/projects/<slug>/%`
    — same semantics as `iter_entries(project=...)`.

    When `skip_sync=True`, bypass the JSONL ingest and serve whatever is
    already cached (mirrors `get_entries`' opt-out). The cache-open fallback
    still fires if the cache DB is unusable.
    """
    try:
        conn = open_cache_db()
    except (sqlite3.DatabaseError, OSError) as exc:
        eprint(f"[cache] unavailable ({exc}); falling back to direct JSONL parse")
        return _direct_parse_claude_session_entries(
            range_start, range_end, project=project
        )

    if not skip_sync:
        stats = sync_cache(conn)
        if stats.lock_contended:
            # Partial cache window: a concurrent ingest may have committed some
            # files but not others. For correctness, fall back to a direct
            # JSONL parse — same rationale as `get_entries`.
            eprint(
                "[cache] concurrent ingest in progress; "
                "falling back to direct JSONL parse for correctness"
            )
            return _direct_parse_claude_session_entries(
                range_start, range_end, project=project
            )

    start_iso = range_start.astimezone(dt.timezone.utc).isoformat()
    end_iso = range_end.astimezone(dt.timezone.utc).isoformat()

    sql = (
        "SELECT "
        "  se.timestamp_utc, se.model, "
        "  se.input_tokens, se.output_tokens, "
        "  se.cache_create_tokens, se.cache_read_tokens, "
        "  se.source_path, "
        "  sf.session_id, sf.project_path, "
        "  se.cost_usd_raw, se.usage_extra_json "
        "FROM session_entries se "
        "LEFT JOIN session_files sf ON sf.path = se.source_path "
        "WHERE se.timestamp_utc >= ? AND se.timestamp_utc <= ?"
    )
    params: list[Any] = [start_iso, end_iso]
    if project is not None:
        escaped = (
            project.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        )
        sql += r" AND se.source_path LIKE ? ESCAPE '\'"
        params.append(f"%/projects/{escaped}/%")
    sql += " ORDER BY se.timestamp_utc ASC"

    rows = conn.execute(sql, params).fetchall()

    return [
        _JoinedClaudeEntry(
            timestamp=dt.datetime.fromisoformat(row[0]),
            model=row[1],
            input_tokens=row[2],
            output_tokens=row[3],
            cache_creation_tokens=row[4],
            cache_read_tokens=row[5],
            source_path=row[6],
            session_id=row[7],
            project_path=row[8],
            cost_usd=row[9],
            usage_extra=(json.loads(row[10]) if row[10] else None),
        )
        for row in rows
    ]


def _direct_parse_claude_session_entries(
    range_start: dt.datetime,
    range_end: dt.datetime,
    *,
    project: str | None = None,
) -> list[_JoinedClaudeEntry]:
    """Fallback when the cache DB is unavailable — direct JSONL scan.

    Returns `_JoinedClaudeEntry` rows. Unlike the cache-backed path,
    session_id/project_path are derived per-file here (not via JOIN):
    scan the file for the first `sessionId` / `cwd` value, else fall
    back to the filename UUID and the decoded-escaped parent directory
    — same logic as `_ensure_session_files_row`.

    Uses the ccusage-parity dict-keyed accumulator. Each per-file parse
    contributes into a global `(entry, source_path)` map keyed by
    `msg_id:req_id`; ties broken by `_should_replace`. NULL-keyed entries
    bypass dedup. After all files are walked, results are stamped with
    their owning file's session_id/cwd metadata and emitted in
    timestamp order.
    """
    files = _discover_session_files(range_start, project=project)

    # File metadata: source_path -> (session_id, project_path/cwd).
    meta_by_path: dict[str, tuple[str, str]] = {}

    # Global accumulator: (msg_id:req_id) -> (UsageEntry, source_path).
    dedupe_map: dict[str, tuple[UsageEntry, str]] = {}
    # Null-key entries (rare; same as the cache's partial-index fallthrough).
    no_key_with_meta: list[tuple[UsageEntry, str]] = []

    for fp in files:
        source_path = str(fp)

        # Pull sessionId / cwd from the JSONL (cheap: stops at first hit).
        session_id: str | None = None
        cwd: str | None = None
        try:
            with open(source_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if session_id is None:
                        sid = obj.get("sessionId")
                        if isinstance(sid, str) and sid:
                            session_id = sid
                    if cwd is None:
                        cwd_val = obj.get("cwd")
                        if isinstance(cwd_val, str) and cwd_val:
                            cwd = cwd_val
                    if session_id is not None and cwd is not None:
                        break
        except OSError:
            pass

        if session_id is None:
            session_id = os.path.splitext(os.path.basename(source_path))[0]
        if cwd is None:
            cwd = _decode_escaped_cwd(os.path.basename(os.path.dirname(source_path)))
        meta_by_path[source_path] = (session_id, cwd)

        # Parse this file with a fresh per-file dedupe_map so we can attach
        # the source_path provenance to whatever wins this file's local
        # contests. Then merge into the global map using the same
        # `_should_replace` rule. (A shared dedupe_map across files would
        # lose the source_path of the winning entry — _parse_usage_entries
        # has no awareness of per-file metadata.)
        file_dedupe_map: dict[str, UsageEntry] = {}
        file_no_key = _parse_usage_entries(
            fp, range_start, range_end, dedupe_map=file_dedupe_map,
        )

        # Merge file-local no-key entries directly (no dedup contest).
        for entry in file_no_key:
            no_key_with_meta.append((entry, source_path))

        # Merge file-local dedup-keyed entries into the global map.
        # Same tiebreaker as the cache's ON CONFLICT DO UPDATE clause:
        # higher-token total wins the entry DATA. But `source_path` is
        # STICKY to whichever file FIRST contributed the key — it is NOT
        # flipped to the winner. This mirrors the cache ingest path, where
        # `source_path` is intentionally OMITTED from the ON CONFLICT DO
        # UPDATE SET clause (see this file's UPSERT, ~line 636) so the
        # downstream `LEFT JOIN session_files ON sf.path = se.source_path`
        # attributes tokens to the project of the file that first wrote the
        # row. Replacing it here would move project attribution to the
        # winner's file — `cctally project` (and any session_files join)
        # would then disagree with the normal cached behavior exactly when
        # this fallback path is exercised.
        for key, entry in file_dedupe_map.items():
            existing = dedupe_map.get(key)
            if existing is None:
                dedupe_map[key] = (entry, source_path)
            elif _should_replace(entry, existing[0]):
                # Winner's DATA, first contributor's source_path (sticky).
                dedupe_map[key] = (entry, existing[1])

    # Flatten + emit.
    results: list[_JoinedClaudeEntry] = []
    flat: list[tuple[UsageEntry, str]] = list(dedupe_map.values()) + no_key_with_meta
    flat.sort(key=lambda pair: pair[0].timestamp)
    _token_keys = {
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    }
    for entry, source_path in flat:
        usage = entry.usage
        sid, cwd = meta_by_path[source_path]
        # Mirror the cache-backed path: carry non-token `usage` extras
        # (e.g. `speed`) so `_usage_entry_from_joined` can restore them.
        extras = {k: v for k, v in usage.items() if k not in _token_keys}
        results.append(_JoinedClaudeEntry(
            timestamp=entry.timestamp,
            model=entry.model,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_creation_tokens=int(
                usage.get("cache_creation_input_tokens", 0) or 0
            ),
            cache_read_tokens=int(
                usage.get("cache_read_input_tokens", 0) or 0
            ),
            source_path=source_path,
            session_id=sid,
            project_path=cwd,
            cost_usd=entry.cost_usd,
            usage_extra=(extras or None),
        ))

    return results


# === Region 5: CodexIngestStats + Codex ingest path (was bin/cctally:2671-2923) ===


@dataclass
class CodexIngestStats:
    files_total: int = 0
    files_processed: int = 0
    files_skipped_unchanged: int = 0
    files_reset_truncated: int = 0
    # Count of codex_session_entries rows written by this sync. Codex
    # ingest uses INSERT OR IGNORE — ignored conflicts do NOT bump
    # SQLite's `total_changes`, so this number is effectively "rows
    # newly inserted". Field is named ``rows_changed`` for parity with
    # ``IngestStats`` (Claude path) which carries an UPSERT and
    # therefore counts both new INSERTs and DO UPDATE replacements.
    rows_changed: int = 0
    lock_contended: bool = False


def _progress_codex_stderr(stats: CodexIngestStats, *, force: bool = False) -> None:
    """Default stderr progress callback for Codex ingest."""
    if not force and stats.files_processed % 200 != 0:
        return
    eprint(
        f"[codex-cache] {stats.files_processed}/{stats.files_total} files, "
        f"{stats.rows_changed} rows changed"
    )


def sync_codex_cache(
    conn: sqlite3.Connection,
    *,
    progress: Callable[[CodexIngestStats], None] | None = None,
    rebuild: bool = False,
) -> CodexIngestStats:
    """Read-through delta ingest of ~/.codex/sessions/**/*.jsonl.

    Acquires an exclusive fcntl.flock on cache.db.codex.lock (separate from
    the Claude sync lock so the two ingests can run concurrently). On
    contention returns immediately with lock_contended=True.

    When `rebuild=True`, clears the cached rows AFTER acquiring the lock
    so a lost race does not wipe a cache another process is actively
    populating. If the lock is contended on a rebuild, the cache is left
    untouched and the caller sees `lock_contended=True`.
    """
    stats = CodexIngestStats()
    c = _cctally()
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _cctally_core.CACHE_LOCK_CODEX_PATH.touch()

    lock_fh = open(_cctally_core.CACHE_LOCK_CODEX_PATH, "w")
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            eprint("[codex-cache] sync already in progress; using existing cache")
            stats.lock_contended = True
            return stats

        if rebuild:
            # Clear INSIDE the lock — see sync_cache() for the full
            # rationale. Done before the existing SELECT so delta
            # detection sees an empty baseline.
            conn.execute("DELETE FROM codex_session_entries")
            conn.execute("DELETE FROM codex_session_files")
            conn.commit()
            eprint("[cache-sync] rebuild: cleared Codex cached entries")

        roots = _cctally()._codex_session_roots()
        paths: list[pathlib.Path] = []
        seen: set[pathlib.Path] = set()
        for root in roots:
            for jp in root.glob("**/*.jsonl"):
                if jp in seen or not jp.is_file():
                    continue
                seen.add(jp)
                paths.append(jp)
        stats.files_total = len(paths)

        # This SELECT does NOT open an implicit transaction (Python's
        # sqlite3 module only BEGINs on DML). Do NOT add any INSERT/
        # UPDATE/DELETE/REPLACE statement between here and the per-file
        # loop below — the read+parse inside that loop must run with
        # zero cache.db write lock held.
        #
        # mtime_ns is selected into `existing` for diagnostics only —
        # delta detection consults size alone (Codex rollout JSONLs are
        # append-only, so a size change is a sufficient signal and mtime
        # is prone to clock-skew false-positives).
        existing = {
            row[0]: (row[1], row[2], row[3], row[4], row[5], row[6])
            for row in conn.execute(
                "SELECT path, size_bytes, mtime_ns, last_byte_offset, "
                "last_session_id, last_model, last_total_tokens "
                "FROM codex_session_files"
            )
        }

        for jp in paths:
            path_str = str(jp)
            try:
                st = jp.stat()
            except OSError as exc:
                eprint(f"[codex-cache] stat failed for {jp}: {exc}")
                continue

            size = st.st_size
            mtime_ns = st.st_mtime_ns
            prev = existing.get(path_str)
            start_offset = 0
            truncated = False
            initial_session_id: str | None = None
            initial_model: str | None = None
            initial_total_tokens = 0
            prev_total_tokens: int | None = None
            if prev is not None:
                (
                    prev_size, _, prev_offset, prev_sid, prev_model, prev_ttot,
                ) = prev
                prev_total_tokens = (
                    int(prev_ttot) if prev_ttot is not None else None
                )
                if size == prev_size:
                    stats.files_skipped_unchanged += 1
                    continue
                if size > prev_size:
                    start_offset = prev_offset
                    initial_session_id = prev_sid
                    initial_model = prev_model
                    initial_total_tokens = prev_total_tokens or 0
                else:
                    truncated = True
                    start_offset = 0
                    initial_session_id = None
                    initial_model = None
                    initial_total_tokens = 0
                    prev_total_tokens = None

            rows: list[tuple[Any, ...]] = []
            final_offset = start_offset
            # Mutable tracker that the iterator updates on every
            # session_meta / turn_context record, regardless of whether a
            # later token_count yields. Without this, a delta window that
            # ends on a metadata-only tail would lose the terminal
            # session_id/model and the next resume would mis-attribute the
            # first post-resume token_count.
            iter_state = _CodexIterState(
                session_id=initial_session_id,
                model=initial_model,
            )
            # Track the cumulative `total_token_usage.total_tokens` across this
            # call. The iterator only yields when the cumulative strictly
            # advances by the current turn's `last_token_usage.total_tokens`,
            # so summing the per-turn totals reconstructs the final cumulative.
            running_total = initial_total_tokens
            yielded_count = 0
            try:
                with open(jp, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(start_offset)
                    for offset, entry in _iter_codex_jsonl_entries_with_offsets(
                        fh,
                        path_str,
                        initial_session_id=initial_session_id,
                        initial_model=initial_model,
                        initial_total_tokens=initial_total_tokens,
                        state=iter_state,
                    ):
                        rows.append((
                            path_str,
                            offset,
                            entry.timestamp.astimezone(dt.timezone.utc).isoformat(),
                            entry.session_id,
                            entry.model,
                            entry.input_tokens,
                            entry.cached_input_tokens,
                            entry.output_tokens,
                            entry.reasoning_output_tokens,
                            entry.total_tokens,
                        ))
                        running_total += int(entry.total_tokens or 0)
                        yielded_count += 1
                    final_offset = fh.tell()
            except OSError as exc:
                eprint(f"[codex-cache] could not read {jp}: {exc}")
                continue

            # Pull terminal session_id/model from the iterator's tracker.
            # This picks up updates from session_meta / turn_context events
            # that occurred AFTER the last yielded token_count (or when no
            # token_count yielded at all), which the in-loop assignment
            # would have missed.
            new_last_session_id: str | None = (
                iter_state.session_id
                if iter_state.session_id is not None
                else initial_session_id
            )
            new_last_model: str | None = (
                iter_state.model
                if iter_state.model is not None
                else initial_model
            )

            # Persist the running cumulative if we yielded this call. Otherwise
            # preserve the prior value — never overwrite with 0, which would
            # re-enable double-counting on the next resume.
            new_last_total_tokens: int | None = (
                running_total if yielded_count > 0 else prev_total_tokens
            )

            # Python's sqlite3 module starts an implicit transaction on the
            # first DML statement and commits on conn.commit(). We do NOT
            # call "BEGIN IMMEDIATE" ourselves — see sync_cache() for the
            # full rationale. DELETE + INSERTs + UPDATE happen atomically in
            # a single commit.
            try:
                if truncated:
                    conn.execute(
                        "DELETE FROM codex_session_entries WHERE source_path = ?",
                        (path_str,),
                    )
                    stats.files_reset_truncated += 1
                if rows:
                    before = conn.total_changes
                    conn.executemany(
                        """INSERT OR IGNORE INTO codex_session_entries
                           (source_path, line_offset, timestamp_utc, session_id,
                            model, input_tokens, cached_input_tokens,
                            output_tokens, reasoning_output_tokens,
                            total_tokens)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        rows,
                    )
                    stats.rows_changed += conn.total_changes - before
                conn.execute(
                    """INSERT OR REPLACE INTO codex_session_files
                       (path, size_bytes, mtime_ns, last_byte_offset,
                        last_ingested_at, last_session_id, last_model,
                        last_total_tokens)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        path_str, size, mtime_ns, final_offset,
                        dt.datetime.now(dt.timezone.utc).isoformat(),
                        new_last_session_id, new_last_model,
                        new_last_total_tokens,
                    ),
                )
                conn.commit()
                stats.files_processed += 1
            except sqlite3.DatabaseError as exc:
                eprint(f"[codex-cache] db error on {jp}: {exc}")
                conn.rollback()
                continue

            if progress is not None:
                progress(stats)

        if progress is not None:
            progress(stats)
        return stats
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()


def iter_codex_entries(
    conn: sqlite3.Connection,
    range_start: dt.datetime,
    range_end: dt.datetime,
) -> list[CodexEntry]:
    """Return cached CodexEntry rows with timestamp in [range_start, range_end]."""
    start_iso = range_start.astimezone(dt.timezone.utc).isoformat()
    end_iso = range_end.astimezone(dt.timezone.utc).isoformat()
    sql = (
        "SELECT timestamp_utc, session_id, model, "
        "input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens, source_path "
        "FROM codex_session_entries "
        "WHERE timestamp_utc >= ? AND timestamp_utc <= ? "
        "ORDER BY timestamp_utc ASC"
    )
    entries: list[CodexEntry] = []
    for row in conn.execute(sql, (start_iso, end_iso)):
        entries.append(CodexEntry(
            timestamp=dt.datetime.fromisoformat(row[0]),
            session_id=row[1],
            model=row[2],
            input_tokens=row[3],
            cached_input_tokens=row[4],
            output_tokens=row[5],
            reasoning_output_tokens=row[6],
            total_tokens=row[7],
            source_path=row[8],
        ))
    return entries


def _collect_codex_entries_direct(
    range_start: dt.datetime,
    range_end: dt.datetime,
) -> list[CodexEntry]:
    """Legacy direct-parse fallback when cache.db is unavailable."""
    files = _discover_codex_session_files(range_start)
    entries: list[CodexEntry] = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                for _offset, entry in _iter_codex_jsonl_entries_with_offsets(fh, str(fp)):
                    if entry.timestamp < range_start or entry.timestamp > range_end:
                        continue
                    entries.append(entry)
        except OSError as exc:
            eprint(f"[codex] could not read {fp}: {exc}")
    return entries


def get_codex_entries(
    range_start: dt.datetime,
    range_end: dt.datetime,
) -> list[CodexEntry]:
    """Cache-first Codex entry fetch with transparent fallback.

    Every Codex-reading command must use this rather than touching
    open_cache_db directly.
    """
    try:
        conn = open_cache_db()
    except (sqlite3.DatabaseError, OSError) as exc:
        eprint(f"[cache] unavailable ({exc}); falling back to direct JSONL parse")
        return _collect_codex_entries_direct(range_start, range_end)
    stats = sync_codex_cache(conn)
    if stats.lock_contended:
        # Sync commits file-by-file, so contention on the ingest lock
        # (e.g. a concurrent --rebuild, or a first-run sync still in
        # flight) can leave the cache PARTIALLY populated — some files
        # ingested, others pending. An "is the table empty?" guard passes
        # in that window and we'd silently return results missing the
        # caller's range. Fall back to a direct JSONL parse unconditionally
        # on contention; correctness > speed in the rare-but-real window
        # where cache state does not match disk.
        eprint(
            "[cache] concurrent codex ingest in progress; "
            "falling back to direct JSONL parse for correctness"
        )
        return _collect_codex_entries_direct(range_start, range_end)
    return iter_codex_entries(conn, range_start, range_end)


def get_entries(
    range_start: dt.datetime,
    range_end: dt.datetime,
    *,
    project: str | None = None,
    skip_sync: bool = False,
) -> list[UsageEntry]:
    """Cache-first entry fetch with transparent fallback. Every JSONL-consuming
    command should use this instead of talking to open_cache_db directly.

    When `skip_sync=True`, bypass the JSONL ingest and serve whatever is
    already cached. The cache-open fallback still fires if the cache DB is
    unusable, but the ingest + lock-contention fallback are both skipped.
    """
    try:
        conn = open_cache_db()
    except (sqlite3.DatabaseError, OSError) as exc:
        eprint(f"[cache] unavailable ({exc}); falling back to direct JSONL parse")
        return _collect_entries_direct(range_start, range_end, project=project)
    if not skip_sync:
        stats = sync_cache(conn)
        if stats.lock_contended:
            # Sync commits file-by-file, so contention on the ingest lock
            # (e.g. a concurrent --rebuild, or a first-run sync still in
            # flight) can leave the cache PARTIALLY populated — some files
            # ingested, others pending. An "is the table empty?" guard passes
            # in that window and we'd silently return results missing the
            # caller's range. Fall back to a direct JSONL parse unconditionally
            # on contention; correctness > speed in the rare-but-real window
            # where cache state does not match disk.
            eprint(
                "[cache] concurrent ingest in progress; "
                "falling back to direct JSONL parse for correctness"
            )
            return _collect_entries_direct(range_start, range_end, project=project)
    return iter_entries(conn, range_start, range_end, project=project)


# === Region 6: open_cache_db (was bin/cctally:9040-9155) ===


def open_cache_db() -> sqlite3.Connection:
    """Open (or create) the session-entry cache DB.

    Enables WAL mode so queries can run concurrently with an in-progress
    ingest. On sqlite3.DatabaseError (corruption) the file is unlinked and
    recreated — the cache is fully re-derivable from JSONL, so this is safe.
    """
    c = _cctally()
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
        conn.execute("SELECT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        eprint(f"[cache] corrupt cache DB ({exc}); recreating")
        try:
            _cctally_core.CACHE_DB_PATH.unlink()
        except FileNotFoundError:
            pass
        conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    # Apply the shared cache.db schema (cctally-dev#93, D4): Claude tables +
    # indexes, the session_id / project_path column adds on session_files
    # (A2 `session` metadata, populated lazily in sync_cache() /
    # _ensure_session_files_row()), the Codex base tables + indexes, and the
    # cache_meta sentinel table. This is the single cache.db schema source —
    # the eager-apply path (_eagerly_apply_cache_migrations) uses the SAME
    # helper, so the two can no longer drift. The Codex last_total_tokens
    # ALTER + purge stays below (out of the shared helper — D4/P1#3).
    _cctally_db_sib._apply_cache_schema(conn)

    # Migration: add last_total_tokens to codex_session_files. When the column
    # is newly added (i.e. this is the first run after upgrade), purge the
    # Codex cache so the duplicate-counted rows produced by the previous
    # iterator are reingested cleanly by sync_codex_cache(). The cache is
    # fully re-derivable from ~/.codex/sessions/*.jsonl so this is safe.
    if add_column_if_missing(conn, "codex_session_files", "last_total_tokens", "INTEGER"):
        conn.execute("DELETE FROM codex_session_entries")
        conn.execute("DELETE FROM codex_session_files")
        conn.commit()
        eprint("[cache] migrated codex cache — re-ingesting")

    # Migration framework dispatcher for cache.db. The registry is empty in
    # v1 — this is preparatory wiring that activates when the next cache.db
    # migration ships. With an empty registry the dispatcher hits the
    # fast-path or fresh-install branch and returns immediately. See spec
    # §2.5, §3.3 + the @cache_migration decorator further down in this file.
    _run_pending_migrations(
        conn, registry=_CACHE_MIGRATIONS, db_label="cache.db",
    )
    return conn


# === Region 7: cmd_cache_sync (was bin/cctally:11563-11616) ===


def cmd_cache_sync(args: argparse.Namespace) -> int:
    """Explicitly sync (or rebuild) the session-entry cache.

    Transparent auto-sync happens on every JSONL-reading command; this
    subcommand exists for priming (e.g. via launchd) and for forcing a
    full rebuild after pricing-dict changes or cache corruption.

    --source {claude,codex,all} selects which half(s) to sync/rebuild;
    default is 'all'.
    """
    source = getattr(args, "source", "all")
    conn = open_cache_db()

    # Note: when --rebuild is set we delegate the DELETE to sync_cache /
    # sync_codex_cache, which execute it AFTER acquiring the flock. A
    # pre-sync DELETE here would wipe the cache even when the subsequent
    # sync loses the lock race and bails — leaving the user with empty
    # state. See sync_cache() / sync_codex_cache() docstrings.

    if source in ("claude", "all"):
        stats = sync_cache(conn, progress=_progress_stderr, rebuild=args.rebuild)
        _progress_stderr(stats, force=True)
        if stats.lock_contended and args.rebuild:
            eprint(
                "[cache-sync] rebuild skipped (claude): "
                "another process holds the lock"
            )
        elif not stats.lock_contended:
            eprint(
                f"[cache-sync] claude done: {stats.files_processed} processed, "
                f"{stats.files_skipped_unchanged} skipped, "
                f"{stats.files_reset_truncated} reset, "
                f"{stats.rows_changed} rows changed"
            )

    if source in ("codex", "all"):
        stats = sync_codex_cache(
            conn, progress=_progress_codex_stderr, rebuild=args.rebuild
        )
        _progress_codex_stderr(stats, force=True)
        if stats.lock_contended and args.rebuild:
            eprint(
                "[cache-sync] rebuild skipped (codex): "
                "another process holds the lock"
            )
        elif not stats.lock_contended:
            eprint(
                f"[cache-sync] codex done: {stats.files_processed} processed, "
                f"{stats.files_skipped_unchanged} skipped, "
                f"{stats.files_reset_truncated} reset, "
                f"{stats.rows_changed} rows changed"
            )

    return 0
