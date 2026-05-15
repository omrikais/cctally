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

What stays in bin/cctally:
- Path constants ``APP_DIR``, ``CACHE_DB_PATH``, ``CACHE_LOCK_PATH``,
  ``CACHE_LOCK_CODEX_PATH``, ``CODEX_SESSIONS_DIR`` — referenced from
  the moved bodies via the ``c = _cctally()`` call-time accessor
  pattern (spec §5.5, same as ``bin/_lib_subscription_weeks.py`` and
  ``bin/_lib_aggregators.py``). The accessor resolves
  ``sys.modules['cctally'].X`` on every call, so
  ``monkeypatch.setitem(ns, "CACHE_DB_PATH", tmp)`` and conftest
  ``redirect_paths`` HOME redirects propagate transparently with NO
  test-side changes (tests already patch ``ns["CACHE_DB_PATH"]`` etc.
  by setitem on the dict-as-module bridge). We chose ``c.X`` over the
  ``_cctally_db.py``-style seed block here because cache tests are
  widely scattered (record-usage tick, dashboard panels, share render
  kernel, block tests, every JSONL-reading subcommand fixture) and
  Phase C-style inline patching would touch dozens of sites.
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


# Module-level back-ref shims for the four bare-name helpers consumed
# throughout the moved bodies. Each shim resolves
# ``sys.modules['cctally'].X`` at CALL TIME (not bind time), so
# monkeypatches on cctally's namespace propagate into the moved code
# unchanged. Mirrors the precedent established in ``bin/_cctally_db.py``
# (``now_utc_iso`` / ``parse_iso_datetime`` / ``_compute_block_totals``
# / ``eprint`` shims).
def eprint(*args, **kwargs):
    return sys.modules["cctally"].eprint(*args, **kwargs)


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

_cctally_db_sib = _load_lib("_cctally_db")
add_column_if_missing = _cctally_db_sib.add_column_if_missing
_run_pending_migrations = _cctally_db_sib._run_pending_migrations
_CACHE_MIGRATIONS = _cctally_db_sib._CACHE_MIGRATIONS


# === BEGIN MOVED REGIONS ===
# Path constants (APP_DIR, CACHE_DB_PATH, CACHE_LOCK_PATH,
# CACHE_LOCK_CODEX_PATH, CODEX_SESSIONS_DIR) are accessed via the
# `c = _cctally()` call-time accessor inside each function that
# needs them — so ``monkeypatch.setitem(ns, "CACHE_DB_PATH", tmp)``
# in tests resolves on every read (no stale module-level binding).

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
    """Glob ~/.codex/sessions/**/*.jsonl, filtering by mtime >= range_start."""
    root = _get_codex_sessions_dir()
    if root is None:
        eprint("[codex] no ~/.codex/sessions directory found")
        return []
    start_ts = range_start.timestamp()
    result: list[pathlib.Path] = []
    for jp in root.glob("**/*.jsonl"):
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
    rows_inserted: int = 0
    lock_contended: bool = False


def _progress_stderr(stats: IngestStats, *, force: bool = False) -> None:
    """Default stderr progress callback. Every 200 files or when forced."""
    if not force and stats.files_processed % 200 != 0:
        return
    eprint(
        f"[cache-sync] {stats.files_processed}/{stats.files_total} files, "
        f"{stats.rows_inserted} new rows"
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
    c.APP_DIR.mkdir(parents=True, exist_ok=True)
    c.CACHE_LOCK_PATH.touch()

    lock_fh = open(c.CACHE_LOCK_PATH, "w")
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            eprint("[cache] sync already in progress; using existing cache")
            stats.lock_contended = True
            return stats

        if rebuild:
            # Clear INSIDE the lock — a concurrent rebuild that lost the
            # race would otherwise have wiped this cache before bailing,
            # leaving the user with empty state. Done before the existing
            # SELECT so the subsequent delta-detection logic sees an
            # empty baseline.
            conn.execute("DELETE FROM session_entries")
            conn.execute("DELETE FROM session_files")
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
                    for offset, entry, msg_id, req_id in _iter_jsonl_entries_with_offsets(fh):
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
                    conn.executemany(
                        """INSERT OR IGNORE INTO session_entries
                           (source_path, line_offset, timestamp_utc, model,
                            msg_id, req_id, input_tokens, output_tokens,
                            cache_create_tokens, cache_read_tokens,
                            usage_extra_json, cost_usd_raw)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        rows,
                    )
                    stats.rows_inserted += conn.total_changes - before
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
        "cache_create_tokens, cache_read_tokens, usage_extra_json, cost_usd_raw "
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
        ))
    return entries


def _collect_entries_direct(
    range_start: dt.datetime,
    range_end: dt.datetime,
    *,
    project: str | None = None,
) -> list[UsageEntry]:
    """Legacy direct-parse fallback used when the cache DB can't be opened."""
    files = _discover_session_files(range_start, project=project)
    seen_hashes: set[str] = set()
    entries: list[UsageEntry] = []
    for fp in files:
        entries.extend(
            _parse_usage_entries(fp, range_start, range_end, seen_hashes=seen_hashes)
        )
    return entries


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
        "  se.cost_usd_raw "
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
    """
    results: list[_JoinedClaudeEntry] = []
    files = _discover_session_files(range_start, project=project)
    seen_hashes: set[str] = set()

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

        for entry in _parse_usage_entries(
            fp, range_start, range_end, seen_hashes=seen_hashes
        ):
            usage = entry.usage
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
                session_id=session_id,
                project_path=cwd,
                cost_usd=entry.cost_usd,
            ))

    return results


# === Region 5: CodexIngestStats + Codex ingest path (was bin/cctally:2671-2923) ===


@dataclass
class CodexIngestStats:
    files_total: int = 0
    files_processed: int = 0
    files_skipped_unchanged: int = 0
    files_reset_truncated: int = 0
    rows_inserted: int = 0
    lock_contended: bool = False


def _progress_codex_stderr(stats: CodexIngestStats, *, force: bool = False) -> None:
    """Default stderr progress callback for Codex ingest."""
    if not force and stats.files_processed % 200 != 0:
        return
    eprint(
        f"[codex-cache] {stats.files_processed}/{stats.files_total} files, "
        f"{stats.rows_inserted} new rows"
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
    c.APP_DIR.mkdir(parents=True, exist_ok=True)
    c.CACHE_LOCK_CODEX_PATH.touch()

    lock_fh = open(c.CACHE_LOCK_CODEX_PATH, "w")
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

        root = _get_codex_sessions_dir()
        paths: list[pathlib.Path] = []
        if root is not None:
            for jp in root.glob("**/*.jsonl"):
                if jp.is_file():
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
                    stats.rows_inserted += conn.total_changes - before
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
    c.APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(c.CACHE_DB_PATH)
        conn.execute("SELECT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        eprint(f"[cache] corrupt cache DB ({exc}); recreating")
        try:
            c.CACHE_DB_PATH.unlink()
        except FileNotFoundError:
            pass
        conn = sqlite3.connect(c.CACHE_DB_PATH)

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

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
                f"{stats.rows_inserted} rows inserted"
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
                f"{stats.rows_inserted} rows inserted"
            )

    return 0
