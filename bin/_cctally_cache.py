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
- ``_discover_codex_session_files`` / ``_iter_codex_jsonl_paths`` —
  Codex JSONL discovery primitives (multi-root $CODEX_HOME walk).
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
``_discover_codex_session_files``,
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
from typing import Any, Callable, Iterator


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

# Conversation-message parser kernel (Plan 1). Pure leaf (stdlib-only), so
# it loads at module-load time alongside _lib_jsonl. Since #138 the per-file
# sync ingest goes through the fused ``_iter_sync_entries`` walker (which calls
# ``_lib_conversation.parse_message_row`` directly); ``_iter_message_rows`` is
# now used only by ``backfill_conversation_messages``.
_lib_conversation = _load_lib("_lib_conversation")
_iter_message_rows = _lib_conversation.iter_message_rows

# Shared by the fused per-file walk AND backfill_conversation_messages so the
# column list, placeholders, and tuple order live in ONE place — a column
# add/reorder can't silently desync the two ingest paths (which would land
# values in the wrong columns on whichever path was missed).
_CONV_INSERT_SQL = (
    "INSERT OR IGNORE INTO conversation_messages"
    "(session_id,uuid,parent_uuid,source_path,byte_offset,"
    " timestamp_utc,entry_type,text,blocks_json,model,msg_id,"
    " req_id,cwd,git_branch,is_sidechain,source_tool_use_id,"
    " stop_reason,attribution_skill,attribution_plugin,"
    " search_tool,search_thinking)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

# #193: last non-null write wins (ai-title carries no timestamp; see spec S1). NO
# byte_offset guard — it can't order a cross-file resumed session. Ordering is
# made deterministic by ingest order: backfill_ai_titles walks files
# mtime-ascending so the newest file's last title is written last; the
# incremental fused walk appends only new bytes in file order.
_AI_TITLE_UPSERT_SQL = (
    "INSERT INTO conversation_ai_titles(session_id,ai_title,source_path,byte_offset) "
    "VALUES(?,?,?,?) "
    "ON CONFLICT(session_id) DO UPDATE SET "
    "ai_title=excluded.ai_title, source_path=excluded.source_path, byte_offset=excluded.byte_offset"
)


def _conv_row_tuple(m, path_str):
    """Flatten a ``MessageRow`` into the ``_CONV_INSERT_SQL`` column order.

    The #177 enrichment fields (stop_reason / attribution_skill /
    attribution_plugin / search_tool / search_thinking) are TAIL-APPENDED after
    source_tool_use_id — same order as the SQL column list — so both ingest paths
    (fused per-file walk + backfill_conversation_messages) carry them through this
    one tuple. #217 S1 / U7a: the documented-dead ``search_aux`` column is gone
    from the live schema (dropped by migration 016); the split
    ``search_tool``/``search_thinking`` columns carry the non-prose index."""
    return (
        m.session_id, m.uuid, m.parent_uuid, path_str, m.byte_offset,
        m.timestamp_utc, m.entry_type, m.text, m.blocks_json, m.model,
        m.msg_id, m.req_id, m.cwd, m.git_branch, m.is_sidechain,
        m.source_tool_use_id,
        m.stop_reason, m.attribution_skill, m.attribution_plugin,
        m.search_tool, m.search_thinking,
    )


def _iter_sync_entries(fh, path_str):
    """Fused single-pass sync walker (#138). Yields
    ``(byte_offset, cost_or_None, msgrow_or_None, aititle_or_None)`` for each
    JSONL line from ``fh``'s current position that produces a cost entry, a
    conversation message row, and/or an ai-title record.

    Each line is read once (readline()+tell()) and ``json.loads``-parsed ONCE,
    then classified by the pure per-line parsers (#138 one-parse-per-line stays
    intact — ``parse_ai_title`` runs on the SAME already-parsed ``obj``):

      * ``cost_or_None`` is ``(UsageEntry, msg_id, req_id)`` when the line is a
        billable assistant entry (``_lib_jsonl.parse_cost_entry``), else None.
      * ``msgrow_or_None`` is a ``MessageRow`` when the line is a user/assistant
        turn carrying a uuid (``_lib_conversation.parse_message_row``), else None.
      * ``aititle_or_None`` is an ``AiTitleRow`` when the line is an ai-title
        carrying a non-empty sessionId+aiTitle (#193), else None.

    The three are independent — a normal assistant line yields the first two;
    an ai-title line (a non-user/assistant type) yields only the third. This replaces
    the former cost walk + re-seek-and-walk over the identical byte span: with a
    single walk the "identical span" invariant is structural (one stop point),
    not a prose-enforced ``mrow.byte_offset >= final_offset`` runtime break. A
    partial mid-write tail line (no trailing newline) rewinds the handle and
    stops, so ``fh.tell()`` after the loop is the cost cursor's ``final_offset``
    and the next sync re-reads the line once the newline lands.
    """
    while True:
        offset = fh.tell()
        line = fh.readline()
        if not line:
            return
        if not line.endswith("\n"):
            # Partial tail line — writer is mid-flight. Rewind so the next sync
            # re-reads this line once the newline is in place (and so fh.tell()
            # reports the cost cursor's stop, never past the partial).
            fh.seek(offset)
            return
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        cost = _lib_jsonl.parse_cost_entry(obj, path_str)
        mrow = _lib_conversation.parse_message_row(obj, offset)
        ai = _lib_conversation.parse_ai_title(obj, offset)
        if cost is not None or mrow is not None or ai is not None:
            yield offset, cost, mrow, ai


def _iter_claude_jsonl_files():
    """Yield every Claude transcript ``*.jsonl`` under each data dir's
    ``projects/`` tree. Shared by ``sync_cache`` and the conversation backfill
    so both ingest paths enumerate the IDENTICAL file set."""
    for claude_dir in _get_claude_data_dirs():
        for jp in (claude_dir / "projects").glob("**/*.jsonl"):
            if jp.is_file():
                yield jp

_cctally_db_sib = _load_lib("_cctally_db")
add_column_if_missing = _cctally_db_sib.add_column_if_missing
_run_pending_migrations = _cctally_db_sib._run_pending_migrations
_CACHE_MIGRATIONS = _cctally_db_sib._CACHE_MIGRATIONS
# Storm-free conversation_messages + FTS full-clear (#138). Owns the trigger
# drop/recreate dance so the per-row delete trigger never fires O(rows) under
# the held lock on a rebuild / truncation escalation.
clear_conversation_messages = _cctally_db_sib.clear_conversation_messages
# cache_meta key/value upsert helper — reused by the resumable reingest cursor
# writes (#179) so the ON CONFLICT idiom lives in one place. Caller commits.
_set_cache_meta = _cctally_db_sib._set_cache_meta


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


def _iter_codex_jsonl_paths(roots: list[pathlib.Path]) -> Iterator[pathlib.Path]:
    """Yield each existing *.jsonl under the given roots, de-duped by RESOLVED
    path (first occurrence wins — collapses overlapping/prefix roots and
    symlink/`..` aliases of the same physical file).

    Pure read: globs + is_file() only, no DB access. Shared by both Codex
    walkers (_discover_codex_session_files and sync_codex_cache) so they stay
    in lock-step on dedup + is_file() ordering.
    """
    seen: set[pathlib.Path] = set()
    for root in roots:
        for jp in root.glob("**/*.jsonl"):
            # Dedup on the RESOLVED path, not the raw spelling. A symlinked
            # $CODEX_HOME root or an alias entry (`.../.codex`,
            # `.../sub/../.codex`) can glob the same physical file under
            # different spellings; UNIQUE(source_path, line_offset) keys on the
            # string, so distinct spellings would double-ingest (2-3x tokens /
            # cost) on a fresh walk. resolve() collapses the aliases (issue
            # #108). First spelling still wins for the yielded source_path.
            try:
                key = jp.resolve()
            except OSError:
                key = jp  # unresolvable (broken symlink, perms) — key on raw
            if key in seen:
                continue
            seen.add(key)
            if jp.is_file():
                yield jp


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
    result: list[pathlib.Path] = []
    for jp in _iter_codex_jsonl_paths(roots):
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
    # Targeted (only_paths) live-tail fast-path fields. Default-clean so the
    # only_paths=None callers (every existing caller) read targeted_clean=True
    # and are otherwise unaffected.
    files_failed: int = 0
    deferred_reason: "str | None" = None

    @property
    def targeted_clean(self) -> bool:
        """True ⇔ a targeted ingest fully applied: not contended, not deferred,
        and no per-file failure. The watch loop emits + advances `seen` only
        when this is True."""
        return (not self.lock_contended
                and self.deferred_reason is None
                and self.files_failed == 0)


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


# Flags whose presence means the cache is mid-migration / mid-reingest. A
# targeted (only_paths) ingest DECLINES when any is set and defers to the next
# full background sync — inserting through a half-migrated FTS shape or skipping
# a pending backfill would diverge from what a full sync produces (spec §
# "Targeted ingest contract"). Enumerated against the flag-consumption blocks
# guarded by the `if not rebuild and not targeted:` branch in sync_cache (the
# full-sync-only `_consume_*` calls); keep this tuple in sync with those.
_TARGETED_DECLINE_FLAGS = (
    "conversation_backfill_pending",
    "ai_titles_backfill_pending",
    "conversation_reingest_pending",
    "conversation_source_tool_use_reingest_pending",
    "conversation_reingest_enrichment_pending",
    "conversation_media_reingest_pending",
    "conversation_search_split_pending",
    "conversation_promote_command_args_pending",
    "conversation_sessions_backfill_pending",
    "conversation_queued_prompt_reingest_pending",   # migration 014
    "conversation_reingest_nested_agent_pending",    # migration 017
    "conversation_title_fts_backfill_pending",       # migration 018 (P1-2: HERE ONLY)
    "conversation_reingest_file_touches_pending",    # migration 019 (P1-2: HERE ONLY)
)


def _targeted_has_pending_global_work(conn) -> bool:
    placeholders = ",".join("?" for _ in _TARGETED_DECLINE_FLAGS)
    try:
        row = conn.execute(
            f"SELECT 1 FROM cache_meta WHERE key IN ({placeholders}) LIMIT 1",
            _TARGETED_DECLINE_FLAGS).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def sync_cache(
    conn: sqlite3.Connection,
    *,
    progress: Callable[[IngestStats], None] | None = None,
    rebuild: bool = False,
    only_paths: "set[str] | None" = None,
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

        targeted = only_paths is not None
        if targeted:
            if rebuild:
                raise ValueError("sync_cache: only_paths is incompatible with rebuild")
            if _targeted_has_pending_global_work(conn):
                stats.deferred_reason = "pending_global_flags"
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
            # Plan 1: conversation_messages shares the cost path's lifecycle.
            # A rebuild re-derives the whole cache from on-disk JSONL, so the
            # message index is wiped here (inside the held lock) and the
            # per-file fused walk repopulates it. clear_conversation_messages
            # drops the FTS triggers, truncates, and clears the index via
            # 'delete-all' so the per-row delete trigger never storms O(rows)
            # under the lock (#138) — NOT a bare DELETE that fires conv_fts_ad
            # per row.
            clear_conversation_messages(conn)
            # #193: ai-titles share the message lifecycle on a rebuild — wipe the
            # table (so a title for a since-deleted session can't linger) and the
            # pending-backfill flag in lockstep. The per-file fused walk below
            # repopulates from offset 0, satisfying any deferred backfill.
            conn.execute("DELETE FROM conversation_ai_titles")
            conn.execute("DELETE FROM cache_meta WHERE key='ai_titles_backfill_pending'")
            # Clear the walk-complete sentinel atomically with the wipe
            # (cctally-dev#93, D5/D2): a stale "complete" marker must never
            # survive a destructive rebuild. The end-of-loop write below
            # re-establishes it only after this rebuild's clean walk.
            conn.execute("DELETE FROM cache_meta WHERE key='claude_ingest_walk_complete'")
            # Issue #139: a rebuild walks every file from offset 0, so the
            # per-file fused walk below repopulates the whole message
            # index — that satisfies any deferred existing-install backfill.
            # Drop the pending flag here so the post-rebuild sync does not also
            # run a redundant (idempotent but wasteful) offset-0 backfill pass.
            conn.execute(
                "DELETE FROM cache_meta WHERE key='conversation_backfill_pending'")
            # Issue #164: a rebuild also clears + repopulates the message index
            # id-aware via the normal offset-0 walk, so drop the 003 reingest
            # flag too — the post-rebuild sync must not run a redundant
            # (idempotent but wasteful) clear+backfill pass. #166 migration 004
            # also sets this same flag (to land the subagent kind/meta fields);
            # the rebuild re-derives those fields via the same offset-0 walk, so
            # dropping the flag here covers the 004 reingest too. Migration 006
            # sets the DISTINCT conversation_source_tool_use_reingest_pending
            # flag (to land source_tool_use_id); the same offset-0 walk re-derives
            # it, so drop that flag here as well to avoid a redundant pass. #177
            # migration 007 sets the DISTINCT
            # conversation_reingest_enrichment_pending flag (to land structured
            # input / full_length / stop_reason / attribution / search_aux); the
            # same offset-0 walk re-derives those through the enriched parser, so
            # drop that flag here too — MISSING this site re-arms the flag on
            # every cache-sync --rebuild. #177 S4 migration 009 sets the DISTINCT
            # conversation_media_reingest_pending flag (to land tool_result
            # media[] placeholders + user-content media index + web_search/
            # web_fetch captures); the same offset-0 walk re-derives them, so drop
            # that flag here as well.
            # Migration 014 sets the DISTINCT
            # conversation_queued_prompt_reingest_pending flag (to land queued-
            # while-busy user prompts persisted as queued_command attachments); the
            # same offset-0 walk re-derives them through the current parser, so drop
            # that flag here too — MISSING this site re-arms the flag on every
            # cache-sync --rebuild. #217 S1 migration 017 sets the DISTINCT
            # conversation_reingest_nested_agent_pending flag (to land the
            # ingest-time structured agent_id stamp on >16 KB nested-subagent
            # grandchildren); the same offset-0 walk re-derives it through the
            # current parser, so drop that flag here as well. #217 S2 migrations
            # 018/019 set conversation_title_fts_backfill_pending (title FTS) and
            # conversation_reingest_file_touches_pending (+ its
            # conversation_file_touches_cursor); the offset-0 walk re-derives both
            # the title FTS and the file-touch axis, so drop them too (#219 S2.3).
            # NOTE: unlike the flags above, dropping the 018/019 keys here is
            # COSMETIC — their consumers run on the just-wiped (empty) tables and
            # self-clear before the offset-0 walk repopulates, so leaving them
            # caused no redundant expensive pass and no re-arming. We add them for
            # consistency with the documented convention only.
            conn.execute(
                "DELETE FROM cache_meta WHERE key IN "
                "('conversation_reingest_pending',"
                " 'conversation_source_tool_use_reingest_pending',"
                " 'conversation_reingest_enrichment_pending',"
                " 'conversation_media_reingest_pending',"
                " 'conversation_queued_prompt_reingest_pending',"
                " 'conversation_reingest_nested_agent_pending',"
                " 'conversation_title_fts_backfill_pending',"
                " 'conversation_reingest_file_touches_pending',"
                " 'conversation_file_touches_cursor',"
                " 'conversation_reingest_cursor',"
                " 'conversation_reingest_cursor_gen')")
            # #177 S6: a rebuild repopulates search_tool/search_thinking via the
            # offset-0 walk (the parser derives them), so the migration-010
            # backfill is redundant. But a LEGACY-shape DB still carries the old
            # prose+aux FTS pair that the split triggers can't write — swap to the
            # split shape NOW (the table is empty post-clear, so the walk below
            # populates it through the new triggers), then drop the pending flag +
            # cursor so the post-rebuild sync runs no redundant backfill/swap.
            # MISSING this site re-arms the flag on every cache-sync --rebuild.
            try:
                _split_pending = conn.execute(
                    "SELECT 1 FROM cache_meta "
                    "WHERE key='conversation_search_split_pending'"
                ).fetchone() is not None
            except sqlite3.OperationalError:
                _split_pending = False
            if _split_pending:
                _fts_off = conn.execute(
                    "SELECT 1 FROM cache_meta WHERE key='fts5_unavailable'"
                ).fetchone() is not None
                if not _fts_off and not _cctally_db_sib._conversation_fts_is_split(conn):
                    _cctally_db_sib._swap_conversation_fts_to_split(conn)
            conn.execute(
                "DELETE FROM cache_meta WHERE key IN "
                "('conversation_search_split_pending',"
                " 'conversation_search_split_cursor')")
            # #188 bug 4: a rebuild repopulates conversation_messages via the
            # offset-0 walk through the parser, which now classifies a
            # command-args invocation as entry_type='human' at INGEST (A2) — so
            # the migration-011 backfill is redundant. Drop its flag + cursor so
            # the post-rebuild sync runs no redundant promotion pass. MISSING
            # this site re-arms the flag on every cache-sync --rebuild.
            conn.execute(
                "DELETE FROM cache_meta WHERE key IN "
                "('conversation_promote_command_args_pending',"
                " 'conversation_promote_command_args_cursor')")
            # Browse-rail rollup: a rebuild re-derives conversation_messages from
            # offset 0, so wipe the rollup here (in the same destructive txn,
            # alongside clear_conversation_messages, so a crash-recovery read
            # can't surface ghost rows) and arm the durable backfill flag. The
            # post-walk recompute (after the per-file loop, still under the
            # flock) consumes the flag and rebuilds the rollup from the freshly
            # re-ingested messages, then drops it last (crash-safe).
            conn.execute("DELETE FROM conversation_sessions")
            _set_cache_meta(conn, "conversation_sessions_backfill_pending", "1")
            conn.commit()
            eprint("[cache-sync] rebuild: cleared Claude cached entries")

        # Issue #139: consume the deferred conversation_messages backfill. On an
        # existing-install upgrade, cache migration 002 sets
        # ``conversation_backfill_pending`` instead of walking the whole JSONL
        # history inline (which stalled the triggering command — even a
        # stats-only ``cctally report`` that fires the cache dispatcher but never
        # reads cache.db). sync_cache is the natural owner: it already holds the
        # flock + owns the walker, so a cache-consuming command or the
        # background hook-tick absorbs the one-time offset-0 walk. The backfill
        # touches ONLY conversation_messages (never the session_files cost
        # cursor), is idempotent on (source_path, byte_offset), and commits
        # per-file — so a crash leaves the flag set and the next sync re-runs
        # cleanly. It writes + commits, so it must land here, BEFORE the
        # zero-write-lock read+parse region below (and never on the rebuild
        # path, which already cleared the flag and repopulates via the normal
        # walk). A path-less/:memory: conn has no cache_meta only if the schema
        # was never applied; the try/except tolerates that.
        if not rebuild and not targeted:
            try:
                _pending = conn.execute(
                    "SELECT 1 FROM cache_meta "
                    "WHERE key='conversation_backfill_pending'"
                ).fetchone() is not None
            except sqlite3.OperationalError:
                _pending = False
            if _pending:
                backfill_conversation_messages(conn)
                conn.execute(
                    "DELETE FROM cache_meta "
                    "WHERE key='conversation_backfill_pending'"
                )
                # Browse-rail rollup: a #139 offset-0 backfill bulk-inserts
                # history into conversation_messages, so arm the durable
                # recompute flag (idempotent; covers a partial-migration state
                # where the rollup is empty but messages just landed). The
                # post-walk recompute rebuilds it and drops the flag last.
                _set_cache_meta(conn, "conversation_sessions_backfill_pending", "1")
                conn.commit()

            # #193: consume the deferred ai-title backfill. Cache migration 012 is
            # flag-only (sets ``ai_titles_backfill_pending``); the offset-0 walk
            # over all history via backfill_ai_titles (mtime-ascending,
            # last-write-wins) runs HERE under the held flock — same #139
            # contract as the message backfill above. Touches ONLY
            # conversation_ai_titles; the flag is dropped LAST so a crash mid-walk
            # re-runs cleanly. Never on the rebuild path (which already cleared
            # the flag + repopulates via the normal walk).
            try:
                _ai_pending = conn.execute(
                    "SELECT 1 FROM cache_meta WHERE key='ai_titles_backfill_pending'"
                ).fetchone() is not None
            except sqlite3.OperationalError:
                _ai_pending = False
            if _ai_pending:
                backfill_ai_titles(conn)
                conn.execute(
                    "DELETE FROM cache_meta WHERE key='ai_titles_backfill_pending'"
                )
                conn.commit()

            # Issue #164: consume the deferred conversation_messages re-ingest.
            # Cache migration 003 is flag-only — it sets
            # ``conversation_reingest_pending`` rather than clearing inline
            # (clearing in the handler would run WITHOUT this flock, racing a
            # concurrent sync, and would empty the reader on stats-only /
            # eager-migration opens or ``dashboard --no-sync``). The destructive
            # clear + id-aware offset-0 re-derive live here, UNDER the held
            # flock. Distinct from 002's backfill-without-clear: 003 is
            # clear-then-backfill, re-deriving the WHOLE index id-aware so
            # existing history pairs tool_use<->tool_result. The clear is
            # storm-free (#138); the offset-0 backfill walks every JSONL from 0;
            # the flag is dropped LAST so a crash mid-walk re-runs cleanly on the
            # next sync. Never on the rebuild path (which already wipes +
            # repopulates the index id-aware via the normal walk). #166 migration
            # 004 reuses this SAME flag (to land the spawn subagent_type + the
            # record-level toolUseResult agentId/meta on existing history): the
            # offset-0 backfill re-parses every JSONL through the current parser,
            # so those fields land here with zero new consumption code. Migration
            # 005 reuses it again to reclassify injected isMeta rows from
            # entry_type='human' to 'meta' (so the reader stops attributing skill
            # bodies / git-context to the user). Migration 006 uses a DISTINCT
            # flag ``conversation_source_tool_use_reingest_pending`` (NOT the
            # shared one) to land the message-level ``source_tool_use_id`` — the
            # shared flag also gates the kernel's 005 human-fallback, so re-arming
            # it for 006 could misclassify a genuine human prompt during the
            # pre-reingest window. #177 migration 007 uses ANOTHER distinct flag
            # ``conversation_reingest_enrichment_pending`` (for the same shared-flag
            # reason) to land the enriched data contract (structured input +
            # input_truncated, the raised result cap + full_length, stop_reason /
            # attribution_skill / attribution_plugin, and the search_aux FTS-aux
            # blob); the offset-0 re-parse through the enriched parser lands them
            # all with zero new consumption code. #177 S4 migration 009 uses yet
            # ANOTHER distinct flag ``conversation_media_reingest_pending`` to land
            # the tool_result media[] placeholders + user-content media index +
            # web_search/web_fetch captures; same offset-0 re-parse, same reason
            # for a distinct flag. Migration 014 uses ANOTHER distinct flag
            # ``conversation_queued_prompt_reingest_pending`` to land queued-while-
            # busy user prompts (queued_command attachments the parser now promotes
            # to HUMAN); same offset-0 re-parse, same distinct-flag reason. #217 S1
            # migration 017 uses ANOTHER distinct flag
            # ``conversation_reingest_nested_agent_pending`` to land the ingest-time
            # structured agent_id stamp on >16 KB nested-subagent grandchildren
            # (whose agentId: trailer was clipped past the 16 KB cap); same offset-0
            # re-parse, same distinct-flag reason. We trigger the SAME clear +
            # offset-0 backfill on ANY of these flags and clear them ALL atomically
            # here under the held flock.
            try:
                _reingest = conn.execute(
                    "SELECT 1 FROM cache_meta WHERE key IN "
                    "('conversation_reingest_pending',"
                    " 'conversation_source_tool_use_reingest_pending',"
                    " 'conversation_reingest_enrichment_pending',"
                    " 'conversation_media_reingest_pending',"
                    " 'conversation_queued_prompt_reingest_pending',"
                    " 'conversation_reingest_nested_agent_pending')"
                ).fetchone() is not None
            except sqlite3.OperationalError:
                _reingest = False
            if _reingest:
                # #179: resumable per-file reingest (was a global clear_conversation_messages
                # + offset-0 backfill that re-armed the entire ~2.5min rebuild on any
                # interrupt). The helper checkpoints a sorted-path cursor and clears the
                # three flags + cursor + gen atomically on completion. Never on the rebuild
                # path (which already wipes + repopulates id-aware via the normal walk).
                _resumable_reingest_conversation_messages(conn)
                # Browse-rail rollup: a #179 reingest DELETEs + re-inserts every
                # file's conversation_messages rows (bumping autoincrement ids and
                # potentially MIN/MAX), so arm the durable recompute flag
                # (idempotent; covers a partial-migration state). The post-walk
                # recompute rebuilds the rollup and drops the flag last.
                _set_cache_meta(conn, "conversation_sessions_backfill_pending", "1")
                conn.commit()

            # #177 S6: consume the migration-010 search-column split under the
            # SAME held flock, AFTER the reingest so any just-re-ingested rows
            # already carry fresh search_tool/search_thinking before the backfill
            # touches the tail. Cursor-resumable; the legacy triggers are blind to
            # the search_tool/search_thinking UPDATEs (they fire on text only), so
            # old search keeps working until the final swap.
            _consume_search_split(conn)

            # #188 bug 4: consume the migration-011 command-args promotion under
            # the SAME held flock, AFTER the search split so a row flipped to
            # entry_type='human' here keeps the fresh search_tool/search_thinking
            # the split just wrote (the consumer recomputes them anyway, but
            # ordering keeps the two passes independent + idempotent). Flips
            # legacy META command rows carrying a real <command-args> prompt to
            # HUMAN(text=args); the split-FTS UPDATE triggers re-index the args.
            _consume_promote_command_args(conn)

            # #217 S2 / E7: consume the migration-018 title-FTS backfill under the
            # SAME held flock. An FTS5 'rebuild' re-derives the external-content
            # title index from conversation_ai_titles (P1-7) — idempotent under
            # the 012-then-018 both-pending ordering, and a cheap clear-only
            # no-op on a no-FTS5 build (P1-6). Touches ONLY the title index, never
            # conversation_messages (P1-2).
            _consume_title_fts(conn)

            # #217 S2 / I-3: consume the migration-019 file-touches backfill under
            # the SAME held flock. Derives conversation_file_touches from existing
            # blocks_json history (cursor-resumable; idempotent via INSERT OR
            # IGNORE). Touches ONLY conversation_file_touches, never
            # conversation_messages (P1-2).
            _consume_file_touches(conn)

        if targeted:
            paths = [pathlib.Path(p) for p in only_paths if pathlib.Path(p).is_file()]
        else:
            paths = list(_iter_claude_jsonl_files())
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
        # Targeted (only_paths) sync narrows `paths` to the requested file(s),
        # so the orphan scan below — which infers "deleted from disk" from a
        # tracked path's absence in `paths` — would mistake EVERY other tracked
        # file for an orphan and nuke the walk-complete marker. Skip it entirely
        # for targeted: the live-tail fast path never prunes orphans (the full
        # background sync owns that).
        if not targeted:
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
            if targeted:
                # The targeted fast path must NEVER trigger the global
                # full-cache wipe-and-re-ingest escalation below — that would
                # turn a 1s live-tail tick into a multi-minute rebuild and drop
                # every other session's rows. Decline and defer to the next full
                # background sync, which owns the truncation escalation.
                stats.deferred_reason = "truncation"
                return stats
            eprint(
                f"[cache-sync] truncation detected on {len(truncated_paths)} "
                f"file(s) — re-ingesting all files (safe under ccusage-parity "
                f"dedup)"
            )
            conn.execute("DELETE FROM session_entries")
            # Plan 1: truncation escalates to a full re-ingest of EVERY file,
            # so conversation_messages is wiped here (parallel to the
            # session_entries full-reset) and the per-file fused walk
            # repopulates it from offset 0. Storm-free clear (#138): drop FTS
            # triggers → truncate → 'delete-all' → recreate, so conv_fts_ad
            # never fires O(rows) inside the held lock.
            clear_conversation_messages(conn)
            # #193: truncation escalates to a full offset-0 re-ingest, so wipe
            # conversation_ai_titles too (parallel to the session_entries +
            # conversation_messages full-reset). The per-file fused walk below
            # repopulates it from offset 0.
            conn.execute("DELETE FROM conversation_ai_titles")
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
            # Browse-rail rollup: truncation escalates to a full offset-0
            # re-ingest of conversation_messages, so wipe the rollup here (in the
            # same destructive txn, alongside clear_conversation_messages) and
            # arm the durable backfill flag. The post-walk recompute rebuilds it
            # from the re-ingested messages and drops the flag last (crash-safe).
            conn.execute("DELETE FROM conversation_sessions")
            _set_cache_meta(conn, "conversation_sessions_backfill_pending", "1")
            conn.commit()
            stats.files_reset_truncated += len(truncated_paths)
            # Force every file to re-ingest from offset 0: clearing the
            # `existing` map makes `prev is None` true downstream, so the
            # per-file branch takes the fresh-ingest path (start_offset=0,
            # truncated=False since we already wiped the table above —
            # avoids a redundant per-file DELETE that would be a no-op).
            existing = {}

        # Browse-rail rollup: accumulate the session_ids whose
        # conversation_messages this walk touched, so the post-walk recompute can
        # scope its DELETE+INSERT re-derive to just those sessions (steady
        # state). Pure Python —
        # updated only AFTER each per-file conn.commit() below, never inside the
        # zero-write-lock read/parse region, so it adds no DML there.
        touched_sessions: set = set()

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
                stats.files_failed += 1
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
            conv_rows: list[tuple[Any, ...]] = []
            ai_rows: list[tuple[Any, ...]] = []   # #193: ai-title upserts
            final_offset = start_offset
            try:
                with open(jp, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(start_offset)
                    # Fused single-pass walk (#138): cost rows AND conversation
                    # message rows come from ONE parse of each line. An assistant
                    # line yields both; a user line yields only a message row.
                    # This replaces the former cost walk + re-seek conversation
                    # walk over the identical span — the "identical span"
                    # invariant is now structural (a single stop point) rather
                    # than a prose-enforced ``>= final_offset`` runtime break.
                    for offset, cost, mrow, ai in _iter_sync_entries(fh, path_str):
                        if cost is not None:
                            entry, msg_id, req_id = cost
                            usage = entry.usage
                            inp = int(usage.get("input_tokens", 0) or 0)
                            out = int(usage.get("output_tokens", 0) or 0)
                            cc = int(usage.get("cache_creation_input_tokens", 0) or 0)
                            cr = int(usage.get("cache_read_input_tokens", 0) or 0)
                            # #181: `speed` is the ONLY non-token usage key any
                            # consumer reads, so materialize just that scalar and
                            # write NULL into usage_extra_json — no more
                            # serializing the deeply-nested blob the read paths
                            # used to json.loads per row.
                            speed = usage.get("speed")
                            rows.append((
                                path_str,
                                offset,
                                entry.timestamp.astimezone(dt.timezone.utc).isoformat(),
                                entry.model,
                                msg_id,
                                req_id,
                                inp, out, cc, cr,
                                None,    # usage_extra_json — bloat no longer written (#181)
                                speed,   # materialized speed column
                                entry.cost_usd,
                            ))
                        if mrow is not None:
                            conv_rows.append(_conv_row_tuple(mrow, path_str))
                        if ai is not None:
                            # #193: accumulate ai-title upserts in file order; the
                            # executemany below applies them after conv_rows.
                            ai_rows.append((ai.session_id, ai.ai_title,
                                            path_str, ai.byte_offset))
                    # ``final_offset`` is the single walk's stop — captured AFTER
                    # the loop drains (or rewinds a partial mid-write tail line).
                    # It is what session_files.last_byte_offset is written from,
                    # so it must reflect the cost cursor's position; with the
                    # fused walk there is exactly one stop point shared by the
                    # cost and conversation rows (#138 / #Plan1 Task 4
                    # cursor-consistency invariant).
                    final_offset = fh.tell()
            except OSError as exc:
                eprint(f"[cache] could not read {jp}: {exc}")
                walk_clean = False  # skipped a file without ingesting (D5a)
                stats.files_failed += 1
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
                            usage_extra_json, speed, cost_usd_raw)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                               speed = excluded.speed,
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
                               AND excluded.speed IS NOT NULL
                               AND session_entries.speed IS NULL
                            )""",
                        rows,
                    )
                    stats.rows_changed += conn.total_changes - before
                # Conversation message ingest (Plan 1). Lands in the SAME
                # per-file write transaction as session_entries so the cost
                # rows and message rows for a file commit atomically.
                # INSERT OR IGNORE on UNIQUE(source_path, byte_offset): a
                # resume-replayed line re-walked from a delta offset that
                # already landed is a silent no-op, and the same physical line
                # in two files (resume across JSONL) keeps BOTH rows. No
                # per-file DELETE here — the only conversation_messages resets
                # are the rebuild + truncation-escalation full-clears above
                # (parallel to the cost path's lifecycle).
                if conv_rows:
                    conn.executemany(_CONV_INSERT_SQL, conv_rows)
                    # #217 S2 / I-3: derive this tick's file touches, scoped to the
                    # just-ingested rows' PHYSICAL keys (cr[3]=source_path,
                    # cr[4]=byte_offset per _conv_row_tuple). Cheap (proportional to
                    # new bytes); decoupled from the INSERT OR IGNORE rowcount —
                    # _fill_file_touches reads conversation_messages by physical key,
                    # so an already-present (rowcount-0) row still gets its touches.
                    # Lands in the SAME per-file write transaction as the message
                    # rows, so they commit atomically.
                    _fill_file_touches(
                        conn, scope=[(cr[3], cr[4]) for cr in conv_rows])
                # #193: ai-title upserts for this file, in file order (last wins).
                # Committed atomically with the session_files cursor below.
                if ai_rows:
                    conn.executemany(_AI_TITLE_UPSERT_SQL, ai_rows)
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
                # Browse-rail rollup: record the session_ids this file just
                # committed so the post-walk recompute can scope its DELETE+INSERT
                # re-derive to them. cr[0] is session_id per _conv_row_tuple's
                # column order. Lands
                # AFTER the commit (pure Python; no DML, no extra write lock).
                touched_sessions.update(cr[0] for cr in conv_rows if cr[0] is not None)
            except sqlite3.DatabaseError as exc:
                eprint(f"[cache] db error on {jp}: {exc}")
                conn.rollback()
                walk_clean = False  # rolled back this file without ingesting (D5a)
                stats.files_failed += 1
                continue

            if progress is not None:
                progress(stats)

        if progress is not None:
            progress(stats)

        # Browse-rail rollup maintenance (single post-walk recompute, under the
        # still-held flock, after every per-file commit and before the
        # walk-complete marker). Keyed on the DURABLE flag, not an in-memory
        # bool: a crash between a destructive path's commit (rebuild /
        # truncation / #139 backfill / #179 reingest, each of which armed the
        # flag in its own committed txn) and this recompute leaves the flag set,
        # so the next sync full-recomputes — never strands stale rollup rows
        # (Codex gate BLOCKER 1). Flag set -> full GROUP BY over all sessions
        # (rare, ~90ms), then drop the flag LAST (drop-it-last contract). Else ->
        # scoped re-derive (DELETE+INSERT, not a SQL UPSERT) over just the
        # sessions this walk touched (steady state,
        # ~1 session/tick). Both recomputes derive COUNT/MIN/MAX from the same
        # rows the rail's old live aggregate read, so the rollup stays
        # byte-identical to that aggregate.
        if _conversation_sessions_backfill_pending(conn):
            _recompute_conversation_sessions(conn)
            conn.execute(
                "DELETE FROM cache_meta "
                "WHERE key='conversation_sessions_backfill_pending'"
            )
            conn.commit()
        elif touched_sessions:
            _recompute_conversation_sessions(conn, touched_sessions)
            conn.commit()

        # Walk-complete sentinel write (cctally-dev#93, D5a). Still inside the
        # held fcntl lock, before the finally-unlock. Only when the entire walk
        # was clean AND cache 001 was already applied at the start of this run
        # (D5b): an unclean walk or a straddle run must not vouch for cache
        # completeness. A lock-contended sync returned early above and never
        # reaches here. Presence (not the timestamp) is the gate signal; the
        # value stores the completion instant for doctor/debugging.
        if walk_clean and applied_at_start and not targeted:
            conn.execute(
                "INSERT INTO cache_meta(key, value) "
                "VALUES('claude_ingest_walk_complete', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (dt.datetime.now(dt.timezone.utc).isoformat(),),
            )
            conn.commit()
        # At-rest hardening (Plan 2, spec §5). Runs here — at the end of the
        # write transaction, while the cache.db.lock flock is still held (so a
        # concurrent writer can't be mid-checkpoint) AND after at least one
        # write has materialized the -wal/-shm sidecars. open_cache_db hardens
        # cache.db + the data dir; this finishes the job for the sidecars.
        _harden_cache_sidecars()
        return stats
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()


def backfill_conversation_messages(conn: sqlite3.Connection) -> int:
    """One-time backfill of ``conversation_messages`` for existing installs
    (Plan 1 Task 5). Walks EVERY Claude JSONL from offset 0 and inserts one
    row per user/assistant line via ``_lib_conversation.iter_message_rows``.

    Properties:
      * Per-file commits — a short write transaction per JSONL file, never one
        long transaction over the whole (potentially ~1M-line) history. The
        backfill of a huge history can't hold the cache.db write lock for
        minutes.
      * Idempotent — ``INSERT OR IGNORE`` on ``UNIQUE(source_path,
        byte_offset)``. A row already present (from a prior partial run or from
        the live ``sync_cache`` ingest) is silently skipped.
      * Crash-resumable — because each file commits independently and the
        INSERT is idempotent, a re-run after a crash re-walks every file but
        only the not-yet-committed rows actually land.
      * Cursor-safe — touches ONLY ``conversation_messages``. It never reads or
        writes ``session_files`` / ``session_entries``, so the cost delta
        cursor is untouched: a later ``sync_cache`` still resumes the cost walk
        from exactly where it left off.

    Returns the number of rows inserted. Since issue #139 the caller is
    ``sync_cache`` itself (consuming the ``conversation_backfill_pending`` flag),
    which already holds the ``cache.db.lock`` flock for the duration — the same
    serialization cache migration 001 relies on. The 002 migration handler no
    longer walks inline; it only flags the work as pending.
    """
    inserted = 0
    for jp in _iter_claude_jsonl_files():
        path_str = str(jp)
        rows: list[tuple[Any, ...]] = []
        try:
            with open(jp, "r", encoding="utf-8", errors="replace") as fh:
                for m in _iter_message_rows(fh, path_str):
                    rows.append(_conv_row_tuple(m, path_str))
        except OSError as exc:
            eprint(f"[conversation-backfill] could not read {jp}: {exc}")
            continue
        if rows:
            # cursor.rowcount after an executemany INSERT OR IGNORE is the
            # number of rows actually inserted (conflicts excluded), and —
            # unlike conn.total_changes — it is NOT inflated by the FTS
            # AFTER INSERT trigger's shadow-table writes.
            cur = conn.executemany(_CONV_INSERT_SQL, rows)
            conn.commit()  # per-file commit — no long write txn
            if cur.rowcount and cur.rowcount > 0:
                inserted += cur.rowcount
    return inserted


def backfill_ai_titles(conn: sqlite3.Connection) -> int:
    """One-time backfill of ``conversation_ai_titles`` for existing installs
    (#193). Walks EVERY Claude JSONL from offset 0 via
    ``_lib_conversation.iter_ai_titles`` and upserts.

    Files are walked MTIME-ASCENDING so that, for a session whose ai-title spans
    multiple files (a ``--resume``), the most-recently-modified file's last
    non-null title is written last (last-write-wins; see _AI_TITLE_UPSERT_SQL).
    Per-file commit; the caller (``sync_cache``, consuming the
    ``ai_titles_backfill_pending`` flag) holds the ``cache.db.lock`` flock for the
    duration. Touches ONLY ``conversation_ai_titles`` — the cost/message cursors
    are untouched. Idempotent: a re-run rewrites the same current title (the
    last-write-wins ordering is stable under the deterministic mtime walk).
    Returns rows upserted."""
    n = 0

    def _mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0  # vanished mid-walk; sorts first, the open() below skips it

    files = sorted(_iter_claude_jsonl_files(), key=_mtime)
    for jp in files:
        path_str = str(jp)
        rows: list[tuple[Any, ...]] = []
        try:
            with open(jp, "r", encoding="utf-8", errors="replace") as fh:
                for r in _lib_conversation.iter_ai_titles(fh, path_str):
                    rows.append((r.session_id, r.ai_title, path_str, r.byte_offset))
        except OSError as exc:
            eprint(f"[ai-title-backfill] could not read {jp}: {exc}")
            continue
        if rows:
            conn.executemany(_AI_TITLE_UPSERT_SQL, rows)
            n += len(rows)
            conn.commit()
    return n


_REINGEST_FLAG_KEYS = (
    "conversation_reingest_pending",
    "conversation_source_tool_use_reingest_pending",
    "conversation_reingest_enrichment_pending",
    "conversation_media_reingest_pending",   # #177 S4 (migration 009)
    "conversation_queued_prompt_reingest_pending",   # migration 014
    "conversation_reingest_nested_agent_pending",    # #217 S1 (migration 017)
)


def _reingest_parse_file(jp, path_str):
    """Parse one Claude JSONL into enriched ``conversation_messages`` row tuples
    (``_CONV_INSERT_SQL`` column order). Mirrors ``backfill_conversation_messages``'s
    inner read+flatten, factored to a module-level seam so the resumable reingest
    builds rows BEFORE any write (a parse failure does no DML) and tests can inject.
    Raises ``OSError`` if the file can't be opened/read."""
    rows = []
    with open(jp, "r", encoding="utf-8", errors="replace") as fh:
        for m in _iter_message_rows(fh, path_str):
            rows.append(_conv_row_tuple(m, path_str))
    return rows


def _resumable_reingest_conversation_messages(conn):
    """#179: resumable, lock-friendly replacement for the old global
    ``clear_conversation_messages`` + offset-0 ``backfill_conversation_messages``
    reingest, which re-armed the whole ~2.5min rebuild on any interrupt. Walks
    every Claude JSONL in deterministic sorted-path order, re-enriching one file
    per atomic transaction and checkpointing ``conversation_reingest_cursor`` so an
    interrupt resumes instead of restarting. A ``conversation_reingest_cursor_gen``
    fingerprint (the set of pending reingest flags) resets the cursor whenever the
    pending-flag set changes, so a newly-armed flag forces a fresh pass. The caller
    (``sync_cache``) already holds the cache.db flock; per-file commits bound only
    the SQLite write transaction, not the flock. Clears all _REINGEST_FLAG_KEYS +
    cursor + gen atomically on completion."""
    # 1. Generation guard: reset the cursor if the live pending-flag set differs.
    set_flags = [k for k in _REINGEST_FLAG_KEYS
                 if conn.execute("SELECT 1 FROM cache_meta WHERE key=?", (k,)).fetchone()]
    gen = ",".join(sorted(set_flags))
    gen_row = conn.execute(
        "SELECT value FROM cache_meta WHERE key='conversation_reingest_cursor_gen'"
    ).fetchone()
    if (gen_row[0] if gen_row else None) != gen:
        _set_cache_meta(conn, "conversation_reingest_cursor_gen", gen)
        conn.execute("DELETE FROM cache_meta WHERE key='conversation_reingest_cursor'")
        conn.commit()
        cursor = ""
    else:
        crow = conn.execute(
            "SELECT value FROM cache_meta WHERE key='conversation_reingest_cursor'"
        ).fetchone()
        cursor = crow[0] if crow and crow[0] is not None else ""

    # 2. Per-file resumable walk in deterministic sorted-path order.
    for jp in sorted(_iter_claude_jsonl_files(), key=str):
        path_str = str(jp)
        if path_str <= cursor:
            continue
        try:
            rows = _reingest_parse_file(jp, path_str)   # parse FIRST — no DML on failure
        except OSError as exc:
            # Read/parse failed BEFORE any conversation_messages DML — the file's
            # existing rows are untouched (preserved, not dropped). Only advance the
            # cursor; this cursor-only write needs no rollback envelope (no message
            # DML to undo, and an interrupt mid-commit just re-runs this file).
            eprint(f"[conversation-reingest] could not read {jp}: {exc}; "
                   "preserving existing rows")
            _set_cache_meta(conn, "conversation_reingest_cursor", path_str)
            conn.commit()
            continue
        try:
            # #217 S2 / I-3 (P1-4): conversation_file_touches is derived state
            # keyed by conversation_messages.id, and this per-source reingest
            # DELETEs + re-inserts the file's message rows (bumping autoincrement
            # ids). Delete the file's touches BEFORE the message delete (resolving
            # the ids while they still exist), then refill from the reinserted rows
            # AFTER — all in this one atomic transaction, so a crash leaves no stale
            # or duplicate anchors.
            conn.execute(
                "DELETE FROM conversation_file_touches WHERE message_id IN "
                "(SELECT id FROM conversation_messages WHERE source_path=?)",
                (path_str,))
            conn.execute("DELETE FROM conversation_messages WHERE source_path=?",
                         (path_str,))
            if rows:
                conn.executemany(_CONV_INSERT_SQL, rows)
                # Refill scoped to this file's just-reinserted physical keys
                # (col 3=source_path, col 4=byte_offset per _conv_row_tuple).
                _fill_file_touches(
                    conn, scope=[(r[3], r[4]) for r in rows])
            _set_cache_meta(conn, "conversation_reingest_cursor", path_str)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # 3. Completion: clear flags + cursor + gen atomically.
    conn.execute(
        "DELETE FROM cache_meta WHERE key IN "
        "('conversation_reingest_pending',"
        " 'conversation_source_tool_use_reingest_pending',"
        " 'conversation_reingest_enrichment_pending',"
        " 'conversation_media_reingest_pending',"
        " 'conversation_queued_prompt_reingest_pending',"
        " 'conversation_reingest_nested_agent_pending',"
        " 'conversation_reingest_cursor',"
        " 'conversation_reingest_cursor_gen')")
    conn.commit()


# === Browse-rail rollup (conversation_sessions) maintenance =================
# Keeps conversation_sessions — the four structural aggregates the old live
# GROUP BY produced — in lockstep with conversation_messages so
# GET /api/conversations renders a page without scanning the whole message
# table. Maintained entirely inside sync_cache under the cache.db.lock flock:
# the steady-state per-file loop is insert-only, so a scoped re-derive
# (DELETE+INSERT, not a SQL UPSERT) over the touched sessions suffices; the
# rare heavy/destructive paths (rebuild,
# truncation-escalation, #139 backfill, #179 reingest) set the durable
# ``conversation_sessions_backfill_pending`` cache_meta flag (migration 013
# arms it too) which forces one full recompute, crash-safe across a
# destructive-commit/recompute crash window. The CALLER owns the commit.

# All non-null sessions, recomputed from conversation_messages. Shared by the
# full and scoped recompute so both paths derive byte-identical aggregates.
_CONV_SESSIONS_SELECT = (
    "SELECT session_id, COUNT(*), MIN(timestamp_utc), MAX(timestamp_utc) "
    "FROM conversation_messages WHERE session_id IS NOT NULL"
)


def _conversation_sessions_backfill_pending(conn) -> bool:
    """True while the durable ``conversation_sessions_backfill_pending`` flag is
    set — the signal that the rollup needs one full GROUP BY recompute (armed by
    migration 013 and by every heavy/destructive conversation_messages path).
    Tolerates a missing cache_meta table (path-less / schema-not-applied conn) by
    degrading to False, like the sibling reingest/backfill predicates."""
    try:
        return conn.execute(
            "SELECT 1 FROM cache_meta "
            "WHERE key='conversation_sessions_backfill_pending'"
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _recompute_conversation_sessions(conn, session_ids=None) -> None:
    """Recompute the ``conversation_sessions`` browse-rail rollup from
    ``conversation_messages``. The caller holds the cache.db.lock flock and owns
    the commit (this helper never commits).

    ``session_ids is None`` -> FULL: wipe the whole rollup and rebuild it from a
    single GROUP BY over every non-null session — the rare, flag-gated path
    (rebuild / truncation / backfill / reingest / migration-013 history).

    ``session_ids={...}`` -> SCOPED: for each <=400-id chunk, DELETE those rows
    then re-INSERT the GROUP BY restricted to the chunk — the steady-state path
    keyed on the per-file loop's touched set. DELETE+INSERT (NOT
    INSERT…SELECT…ON CONFLICT, which trips SQLite's upsert-on-SELECT parse
    ambiguity) also correctly drops a session whose rows all vanished — though in
    steady state conversation_messages only gains rows, so that branch is just
    belt-and-suspenders. The chunking keeps the ``session_id IN (…)`` parameter
    list well under SQLite's variable limit.

    The recomputed COUNT/MIN/MAX are byte-identical to the rail's prior live
    aggregate over the same rows — that is the load-bearing invariant
    (assert_rollup_matches_live in the maintenance test pins it)."""
    if session_ids is None:
        conn.execute("DELETE FROM conversation_sessions")
        conn.execute(
            "INSERT INTO conversation_sessions "
            "(session_id, msg_count, started_utc, last_activity_utc) "
            + _CONV_SESSIONS_SELECT + " GROUP BY session_id"
        )
        _fill_conversation_sessions_filter_columns(conn, None)
        return
    ids = [s for s in session_ids if s is not None]
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"DELETE FROM conversation_sessions WHERE session_id IN ({placeholders})",
            chunk,
        )
        conn.execute(
            "INSERT INTO conversation_sessions "
            "(session_id, msg_count, started_utc, last_activity_utc) "
            + _CONV_SESSIONS_SELECT
            + f" AND session_id IN ({placeholders}) GROUP BY session_id",
            chunk,
        )
    _fill_conversation_sessions_filter_columns(conn, ids)


def _fill_conversation_sessions_filter_columns(conn, session_ids):
    """Fill the rollup's browse-FILTER columns (project_label / cost_usd /
    cache_rebuild_count, migration 015) for the given sessions, or ALL when
    ``session_ids is None``. The structural COUNT/MIN/MAX columns are filled by
    the INSERT in _recompute_conversation_sessions; this is the second pass that
    materializes the three filter axes so the rail's date/project/cost/rebuild
    filters are pure-SQL predicates.

    project_label + cost reuse the query kernel's batch maps (the SAME
    _project_label / _session_cost_map the rail's per-page Python path used), so
    a filtered/displayed value equals what the live rail produced. cost is
    rounded to 6dp to match list_conversations' per-row rounding.
    cache_rebuild_count is a per-session lightweight rebuild-count via the
    query kernel's single-source-of-truth helper (no full assembly — U1; the
    rule is shared with the reader path) — a whole-session property
    (recompute, never increment).

    No-op when the columns are absent (a pre-migration-015 cache.db being
    re-derived before its 015 ALTER lands), so an early/partial sync never
    raises ``no such column``. The CALLER owns the commit (this never commits)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversation_sessions)")}
    if "cache_rebuild_count" not in cols:
        return
    lq = _load_lib("_lib_conversation_query")
    if session_ids is None:
        ids = [r[0] for r in conn.execute(
            "SELECT session_id FROM conversation_sessions")]
    else:
        ids = [s for s in session_ids if s is not None]
    if not ids:
        return
    cost = lq._session_cost_map(conn, ids)
    meta = lq._session_latest_meta_map(conn, ids)
    for sid in ids:
        proj = lq._project_label(meta.get(sid, (None, None))[0])
        rebuilds = lq.session_cache_rebuild_count(conn, sid)
        conn.execute(
            "UPDATE conversation_sessions SET project_label=?, cost_usd=?, "
            "cache_rebuild_count=? WHERE session_id=?",
            (proj, round(cost.get(sid, 0.0), 6), rebuilds, sid),
        )


def _consume_search_split(conn) -> None:
    """#177 S6: flock-held consumer for ``conversation_search_split_pending``
    (set by cache migration 010). Cursor-resumable: backfills
    search_tool/search_thinking from each row's ``blocks_json`` via the SHARED
    ``_lib_conversation._derive_search_columns`` chokepoint (so the values are
    byte-identical to live ingest), checkpointing
    ``conversation_search_split_cursor`` per 500-row batch. These UPDATEs are
    INVISIBLE to the LEGACY triggers (which fire on text/search_aux only), so the
    old prose search keeps working untouched until the final swap (spec F5).

    When the cursor completes, swap the legacy two-table FTS to the consolidated
    split shape + rebuild (one short transaction), then delete the pending +
    cursor meta keys. FTS5-unavailable (``fts5_unavailable`` set): the base-column
    backfill still runs (it is FTS-independent), the vtable swap is SKIPPED, the
    flag still clears, and the rebuild-on-availability recovery path
    (_apply_cache_schema) lands the split shape later (spec F6). Interrupted at
    any point ⇒ resumes from the cursor on the next locked sync; a fresh install
    never sets the flag so this is a cheap no-op there."""
    if conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_search_split_pending'"
    ).fetchone() is None:
        return
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key='conversation_search_split_cursor'"
    ).fetchone()
    last_id = int(row[0]) if row else 0
    while True:
        batch = conn.execute(
            "SELECT id, blocks_json FROM conversation_messages "
            "WHERE id > ? ORDER BY id LIMIT 500",
            (last_id,)).fetchall()
        if not batch:
            break
        ups = []
        for rid, bj in batch:
            try:
                blocks = json.loads(bj) if bj else []
            except (TypeError, ValueError):
                blocks = []
            st, sth = _lib_conversation._derive_search_columns(blocks)
            ups.append((st, sth, rid))
            last_id = rid
        conn.executemany(
            "UPDATE conversation_messages SET search_tool=?, search_thinking=? "
            "WHERE id=?", ups)
        _cctally_db_sib._set_cache_meta(
            conn, "conversation_search_split_cursor", str(last_id))
        conn.commit()
    fts_off = conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='fts5_unavailable'"
    ).fetchone() is not None
    if not fts_off:
        _cctally_db_sib._swap_conversation_fts_to_split(conn)
    conn.execute(
        "DELETE FROM cache_meta WHERE key IN "
        "('conversation_search_split_pending','conversation_search_split_cursor')")
    conn.commit()


def _consume_title_fts(conn) -> None:
    """#217 S2 / E7: flock-held consumer for ``conversation_title_fts_backfill_pending``
    (set by cache migration 018). Populates the external-content title FTS over
    ``conversation_ai_titles`` from existing history.

    Uses the FTS5 ``'rebuild'`` command (the established consumer idiom —
    ``_apply_cache_schema``'s recovery rebuild + ``_consume_search_split`` —
    NOT blind row inserts, P1-7): ``'rebuild'`` re-derives the whole index from
    the content table and is IDEMPOTENT even if migration 012's
    ``ai_titles_backfill_pending`` ran first and already populated the index via
    the conv_title_fts_ai trigger (the 012-then-018 both-pending upgrade
    ordering) — re-running yields the same rows, no duplicates or conflict.

    FTS5-unavailable (``fts5_unavailable`` set, P1-6): there is no usable vtable
    to rebuild and a ``'rebuild'`` would error on the absent fts5 module, so just
    clear the flag — ``kind=title`` degrades to a LIKE scan over
    conversation_ai_titles. Touches ONLY the title index (never
    conversation_messages — P1-2: this is NOT a message reingest); the flag is
    dropped LAST so a crash mid-rebuild re-runs cleanly on the next sync. A fresh
    install never sets the flag, so this is a cheap no-op there."""
    if conn.execute(
        "SELECT 1 FROM cache_meta "
        "WHERE key='conversation_title_fts_backfill_pending'"
    ).fetchone() is None:
        return
    fts_off = conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='fts5_unavailable'"
    ).fetchone() is not None
    if not fts_off:
        # The title FTS may not exist YET even on an FTS5-capable build: a pre-S6
        # install whose conversation_fts is still the legacy (text) shape makes
        # _apply_cache_schema early-return at its legacy_present guard BEFORE it
        # creates conversation_title_fts. When migrations 010 + 018 are both
        # pending in that open, _consume_search_split swaps only the MESSAGE FTS
        # (never the title FTS), so a blind 'rebuild' here would raise
        # "no such table: conversation_title_fts" — fts5_unavailable is NOT set
        # (FTS5 IS available), so the fts_off guard above does not protect it.
        # Swallow that OperationalError and RETURN before the flag-clear so the
        # flag survives: the NEXT open (message FTS now split → legacy_present
        # False → _apply_cache_schema creates the title FTS) re-runs this consumer
        # and completes the backfill. Match the message-FTS path's resilience.
        try:
            conn.execute(
                "INSERT INTO conversation_title_fts(conversation_title_fts) "
                "VALUES('rebuild')")
        except sqlite3.OperationalError:
            return   # title FTS not yet created (legacy-shape pre-swap); leave
                     # the flag set, retry on the next open
    conn.execute(
        "DELETE FROM cache_meta WHERE key='conversation_title_fts_backfill_pending'")
    conn.commit()


_FILE_TOUCH_INSERT_SQL = (
    "INSERT OR IGNORE INTO conversation_file_touches"
    "(message_id, session_id, uuid, file_path, tool) VALUES(?,?,?,?,?)")


def _fill_file_touches(conn, scope=None) -> None:
    """#217 S2 / I-3: derive ``conversation_file_touches`` rows from
    ``conversation_messages.blocks_json`` for the in-scope message rows.

    ``scope`` is an iterable of ``(source_path, byte_offset)`` physical keys, or
    ``None`` for ALL rows (the backfill). We read FROM ``conversation_messages``
    (the source of truth) and resolve ``message_id`` from the row's own ``id``.

    P1-3 (load-bearing): scope by the PHYSICAL key ``(source_path, byte_offset)``,
    NEVER by ``uuid``. ``conversation_messages.uuid`` is NOT unique (only
    ``(source_path, byte_offset)`` is; the uuid index is ``(session_id, uuid)``),
    and resume/replay rows legitimately share a ``(session_id, uuid)`` — a
    ``WHERE uuid=?`` fill would touch unrelated physical rows.

    Decoupled from the message-insert rowcount ("dedup must not gate side
    effects"): a no-op INSERT OR IGNORE of an already-present message row (rowcount
    0) still has its touches derived here, because we read the row by physical key
    rather than from the insert's lastrowid/rowcount.

    Cheap at steady state: scoped to the rows ingested this tick (proportional to
    new bytes), never re-parsing the whole session per tick. ``INSERT OR IGNORE``
    on ``UNIQUE(message_id, file_path, tool)`` makes it idempotent, and a row's
    ``blocks_json`` is immutable, so accumulate-via-IGNORE needs no per-tick DELETE.
    The caller owns the commit (this helper never commits)."""
    def _emit(rows):
        for mid, sid, uuid_, bj in rows:
            if not sid:
                continue   # a touch row's session_id is NOT NULL; skip null-session rows
            try:
                blocks = json.loads(bj) if bj else []
            except (TypeError, ValueError):
                blocks = []
            for fp, tool in _lib_conversation._derive_file_touches(blocks):
                conn.execute(_FILE_TOUCH_INSERT_SQL, (mid, sid, uuid_, fp, tool))

    if scope is None:
        # Backfill: cursor-resumable 500-row batches keyed on the message rowid.
        # Resume from the stored cursor so an interrupt skips already-derived
        # batches (the fill is also idempotent via INSERT OR IGNORE, so a restart
        # from 0 would be correct but redundant).
        row = conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='conversation_file_touches_cursor'").fetchone()
        last_id = int(row[0]) if row and row[0] is not None else 0
        while True:
            batch = conn.execute(
                "SELECT id, session_id, uuid, blocks_json FROM conversation_messages "
                "WHERE id > ? ORDER BY id LIMIT 500",
                (last_id,)).fetchall()
            if not batch:
                break
            _emit(batch)
            last_id = batch[-1][0]
            _cctally_db_sib._set_cache_meta(
                conn, "conversation_file_touches_cursor", str(last_id))
            conn.commit()
        return
    for sp, off in scope:
        rows = conn.execute(
            "SELECT id, session_id, uuid, blocks_json FROM conversation_messages "
            "WHERE source_path=? AND byte_offset=?", (sp, off)).fetchall()
        _emit(rows)


def _consume_file_touches(conn) -> None:
    """#217 S2 / I-3: flock-held consumer for
    ``conversation_reingest_file_touches_pending`` (set by cache migration 019).
    Derives ``conversation_file_touches`` from ALL existing ``blocks_json`` history
    via ``_fill_file_touches(conn, scope=None)`` (cursor-resumable, 500-row
    batches), then clears the flag + cursor.

    Touches ONLY ``conversation_file_touches`` (never ``conversation_messages`` —
    P1-2: this is NOT a message reingest). The fill is idempotent (INSERT OR
    IGNORE on the UNIQUE key), so an interrupted backfill resumes cleanly on the
    next locked sync. A fresh install never sets the flag, so this is a cheap
    no-op there."""
    if conn.execute(
        "SELECT 1 FROM cache_meta "
        "WHERE key='conversation_reingest_file_touches_pending'"
    ).fetchone() is None:
        return
    _fill_file_touches(conn, scope=None)
    conn.execute(
        "DELETE FROM cache_meta WHERE key IN "
        "('conversation_reingest_file_touches_pending',"
        " 'conversation_file_touches_cursor')")
    conn.commit()


def _consume_promote_command_args(conn) -> None:
    """#188 bug 4: flock-held consumer for ``conversation_promote_command_args_pending``
    (set by cache migration 011). Cursor-resumable walk of
    ``conversation_messages WHERE entry_type='meta'``: a row whose ``blocks_json``
    is a pure slash-command marker with a NON-EMPTY ``<command-args>`` is a real
    user turn, so flip it to ``entry_type='human'`` with ``text=args`` and
    recompute ``search_tool``/``search_thinking`` via the SHARED
    ``_lib_conversation._derive_search_columns`` chokepoint (byte-identical to
    live ingest). ``/clear`` and stdout-only markers (``_extract_command_invocation``
    returns None) stay META untouched.

    The split-FTS ``AFTER UPDATE OF text, search_tool, search_thinking`` triggers
    keep the external-content index in sync, so we never hand-write FTS rows.
    FTS5-unavailable (``fts5_unavailable`` set): no triggers exist, so the
    base-column UPDATE alone is correct (the index lands later via the
    rebuild-on-availability path). Checkpoints
    ``conversation_promote_command_args_cursor`` per 500-row batch; clears both
    keys when the cursor is exhausted. Interrupted ⇒ resumes from the cursor on
    the next locked sync; a fresh install never sets the flag → cheap no-op."""
    if conn.execute(
        "SELECT 1 FROM cache_meta "
        "WHERE key='conversation_promote_command_args_pending'"
    ).fetchone() is None:
        return
    row = conn.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='conversation_promote_command_args_cursor'").fetchone()
    last_id = int(row[0]) if row else 0
    while True:
        batch = conn.execute(
            "SELECT id, blocks_json FROM conversation_messages "
            "WHERE id > ? AND entry_type='meta' ORDER BY id LIMIT 500",
            (last_id,)).fetchall()
        if not batch:
            break
        ups = []
        for rid, bj in batch:
            last_id = rid
            try:
                blocks = json.loads(bj) if bj else []
            except (TypeError, ValueError):
                blocks = []
            inv = _lib_conversation._extract_command_invocation(
                blocks, _lib_conversation._join_text_blocks(blocks))
            if inv is None:
                continue
            st, sth = _lib_conversation._derive_search_columns(blocks)
            ups.append((inv["args"], st, sth, rid))
        if ups:
            conn.executemany(
                "UPDATE conversation_messages SET entry_type='human', text=?, "
                "search_tool=?, search_thinking=? WHERE id=?", ups)
        _cctally_db_sib._set_cache_meta(
            conn, "conversation_promote_command_args_cursor", str(last_id))
        conn.commit()
    conn.execute(
        "DELETE FROM cache_meta WHERE key IN "
        "('conversation_promote_command_args_pending',"
        " 'conversation_promote_command_args_cursor')")
    conn.commit()


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
        "cache_create_tokens, cache_read_tokens, speed, "
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
        # speed is the only non-token usage key any consumer reads (#181);
        # materialized into its own column so this hot path never parses JSON.
        # `is not None` (not truthiness) so an empty-string speed still surfaces,
        # mirroring the SQL `json_extract(...) IS NOT NULL` parity.
        if row[6] is not None:
            usage["speed"] = row[6]
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
        "  se.cost_usd_raw, se.speed "
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
            # speed materialized into its own column (#181); reconstruct the
            # {"speed": …} shape _usage_entry_from_joined already merges, with
            # zero JSON parsing. `is not None` so an empty-string speed surfaces.
            usage_extra=({"speed": row[10]} if row[10] is not None else None),
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
    # Count of cached files dropped because they fall outside the CURRENT
    # $CODEX_HOME root set (issue #108 — a prior-root purge, not a delta).
    files_pruned: int = 0
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
        # Pure read (glob + is_file only); safe to run before the SELECT and
        # the per-file loop, where no cache.db write lock may be held.
        paths: list[pathlib.Path] = list(_iter_codex_jsonl_paths(roots))
        stats.files_total = len(paths)

        # Scope the cache to the CURRENT root set: drop rows ingested under a
        # prior $CODEX_HOME (issue #108). iter_codex_entries() has NO root
        # predicate — it reads every row in range — so without this, reusing
        # the same cache.db across `CODEX_HOME=/A` then `CODEX_HOME=/B` runs
        # returns A+B instead of just B. Prune every real (absolute) row
        # outside the current set, even when that set is empty (an empty
        # current root then prunes the cache to empty): the cache is fully
        # re-derivable, so honoring the override beats retaining unreachable
        # rows. Done INSIDE the lock and committed BEFORE the existing-SELECT
        # + parse loop so no cache.db write lock is held across the read-heavy
        # ingest (same invariant as the --rebuild clear above). Concurrent
        # processes with different $CODEX_HOME would prune each other; the
        # flock serializes them and that is a pathological configuration.
        if not rebuild:  # --rebuild already cleared both tables above
            current_paths = {str(p) for p in paths}
            # Only prune ABSOLUTE source_paths. _codex_home_roots() makes
            # every real root absolute (via .absolute()), so a real ingested
            # row always stores an absolute str(jp) — INCLUDING a relative
            # $CODEX_HOME like `./codexA`, which is canonicalized before the
            # glob. A relative path here is therefore — by construction — a
            # synthetic baked-cache fixture row (e.g. build-speed-fixtures.py)
            # with no on-disk JSONL to scope against; pruning it would wipe a
            # cache meant to be read as-is (issue #108).
            orphan_paths = [
                row[0]
                for row in conn.execute("SELECT path FROM codex_session_files")
                if row[0] not in current_paths and os.path.isabs(row[0])
            ]
            if orphan_paths:
                conn.executemany(
                    "DELETE FROM codex_session_entries WHERE source_path = ?",
                    [(p,) for p in orphan_paths],
                )
                conn.executemany(
                    "DELETE FROM codex_session_files WHERE path = ?",
                    [(p,) for p in orphan_paths],
                )
                conn.commit()
                stats.files_pruned = len(orphan_paths)

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
    *,
    skip_sync: bool = False,
) -> list[CodexEntry]:
    """Cache-first Codex entry fetch with transparent fallback.

    Every Codex-reading command must use this rather than touching
    open_cache_db directly.

    ``skip_sync=True`` bypasses the ``sync_codex_cache`` ingest pass and serves
    whatever is already cached — for a second read in the same process whose
    range is a SUBSET of a range already fetched (the cache is already warm), so
    a redundant full JSONL walk is wasted work (mirrors ``get_entries``'
    ``skip_sync``).
    """
    try:
        conn = open_cache_db()
    except (sqlite3.DatabaseError, OSError) as exc:
        eprint(f"[cache] unavailable ({exc}); falling back to direct JSONL parse")
        return _collect_codex_entries_direct(range_start, range_end)
    if skip_sync:
        return iter_codex_entries(conn, range_start, range_end)
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


def _sum_codex_cost_for_range(
    start: dt.datetime,
    end: dt.datetime,
    *,
    speed: str = "auto",
    skip_sync: bool = False,
) -> float:
    """Sum USD Codex cost of all `codex_session_entries` in ``[start, end)``.

    The Codex analog of Claude's ``_sum_cost_for_range`` (bin/cctally), used by
    `cctally budget`'s Codex-vendor path (calendar-period + Codex budgets
    feature, spec §4). Reads the **cache DB** via ``get_codex_entries`` (which
    opens ``cache.db``, runs the Codex sync, and carries the contention /
    direct-parse fallback) — NEVER the budget's stats ``conn``, which has no
    Codex tables.

    Spend is computed per entry via the SAME ``_calculate_codex_entry_cost``
    primitive the ``codex-*`` reports use (LiteLLM token semantics; unknown
    model → ``gpt-5`` fallback), so a Codex budget and ``codex-weekly`` agree to
    the cent. A lean sum — no per-entry sample collection (budgets don't need
    ``_compute_codex_cost_stats``' samples list) — but routed through the same
    cost primitive so there is no second pricing copy.

    ``speed="auto"`` resolves to the SAME effective tier the ``codex-*`` reports
    use under the current config (``_resolve_codex_speed`` reads the active
    ``$CODEX_HOME``/``config.toml`` — fast multiplies cost at calc time), so the
    figure matches what ``codex-weekly`` shows on this machine right now.

    ``get_codex_entries`` filters on ``timestamp_utc <= end``; the budget window
    is half-open ``[start, end)`` so an entry exactly at ``end`` is excluded
    here (mirrors the kernel's half-open elapsed math). Empty cache / no entries
    → ``0.0``.

    ``skip_sync=True`` serves the already-warm cache without a fresh ingest —
    for a second sum in the same process over a sub-range of one already fetched
    (e.g. the recent-24h window after the full-period sum).
    """
    c = _cctally()
    eff_speed = c._resolve_codex_speed(speed)
    total = 0.0
    for entry in c.get_codex_entries(start, end, skip_sync=skip_sync):
        if entry.timestamp >= end:
            continue
        total += c._calculate_codex_entry_cost(
            entry.model,
            entry.input_tokens,
            entry.cached_input_tokens,
            entry.output_tokens,
            entry.reasoning_output_tokens,
            speed=eff_speed,
        )
    return total


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


def _harden_cache_sidecars() -> None:
    """Best-effort 0600 on cache.db + its -wal/-shm sidecars (Plan 2, spec §5).

    The -wal/-shm sidecars are created on the first WRITE (not on connect), so
    this runs at the END of the sync_cache write transaction — under the held
    cache.db.lock flock, where they exist — NOT in open_cache_db (where the
    sidecars are absent → a silent no-op that would leave a 0644 WAL). All
    chmod is best-effort: swallow OSError, log, continue.
    """
    base = str(_cctally_core.CACHE_DB_PATH)
    for path in (base, base + "-wal", base + "-shm"):
        try:
            if os.path.exists(path):
                os.chmod(path, 0o600)
        except OSError as exc:
            eprint(f"[cache] could not chmod {path} 0600 ({exc}); continuing")


# === Region 6: open_cache_db (was bin/cctally:9040-9155) ===


def open_cache_db() -> sqlite3.Connection:
    """Open (or create) the session-entry cache DB.

    Enables WAL mode so queries can run concurrently with an in-progress
    ingest. On sqlite3.DatabaseError (corruption) the file is unlinked and
    recreated — the cache is fully re-derivable from JSONL, so this is safe.
    """
    c = _cctally()
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    # cache.db holds plaintext conversation prose at rest (Plan 2, spec §5).
    # Harden the data dir to 0700 so the WAL window between connect and the
    # first write (which materializes the -wal/-shm sidecars, hardened in
    # sync_cache) is not world-readable. Best-effort: swallow OSError + continue.
    try:
        os.chmod(_cctally_core.APP_DIR, 0o700)
    except OSError as exc:
        eprint(f"[cache] could not chmod data dir 0700 ({exc}); continuing")
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

    # Best-effort 0600 on cache.db itself (the 0700 dir above backstops the
    # sidecars until the first write hardens them in sync_cache).
    try:
        os.chmod(_cctally_core.CACHE_DB_PATH, 0o600)
    except OSError as exc:
        eprint(f"[cache] could not chmod cache.db 0600 ({exc}); continuing")

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
        recover_version_ahead=True,
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
