"""#313 P3: conversation-transcript retention prune kernel.

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
(F12).

The FTS5 indexes over ``conversation_messages`` (``conversation_fts``) and
``conversation_ai_titles`` (``conversation_title_fts``) are external-content and
maintained by AFTER-DELETE triggers, which are logically correct on subset
deletes. Whole groups are deleted so those triggers keep the index consistent.
The kernel normally runs inside the caller's open transaction. The orchestrator
supplies a post-group boundary that commits each whole conversation separately
while retaining every flock for the full pass (#315), bounding WAL growth.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

import _cctally_core

UTC = dt.timezone.utc

# cache_meta throttle key (framework-untracked KV — NO schema migration, F7).
_RETENTION_LAST_PRUNE_KEY = "conversation_retention_last_prune_at"
_RETENTION_THROTTLE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class PruneStats:
    """Counts from one prune pass."""

    claude_sessions: int = 0
    claude_messages: int = 0
    codex_conversations: int = 0
    codex_events: int = 0

    @property
    def total_rows(self) -> int:
        return self.claude_messages + self.codex_events


def _cutoff_iso(cutoff_utc: dt.datetime) -> str:
    """Whole-second UTC ``...Z`` boundary for lex comparison against the stored
    ``...Z`` timestamps.

    Second-granular: the sub-second mixed-precision edge at the exact cutoff
    second is immaterial for a multi-day retention window — any message wrongly classified
    there is re-derivable from JSONL and the boundary self-corrects on the next
    (daily) prune.
    """
    return cutoff_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def prune_conversation_transcripts(
    conn: sqlite3.Connection,
    *,
    cutoff_utc: dt.datetime,
    after_group: "Callable[[], None] | None" = None,
) -> PruneStats:
    """Prune every transcript group whose latest activity is before ``cutoff_utc``.

    ``after_group`` runs only after every table and FTS posting owned by one
    session/conversation has been deleted. Direct kernel callers leave it unset
    and retain their caller-managed transaction; the orchestrator uses it for
    #315's whole-conversation intermediate commits.
    """
    cutoff = _cutoff_iso(cutoff_utc)
    claude_sessions, claude_messages = _prune_claude(
        conn, cutoff, after_group=after_group
    )
    codex_conversations, codex_events = _prune_codex(
        conn, cutoff, after_group=after_group
    )
    return PruneStats(
        claude_sessions=claude_sessions,
        claude_messages=claude_messages,
        codex_conversations=codex_conversations,
        codex_events=codex_events,
    )


def _prunable_groups(
    conn: sqlite3.Connection, table: str, key_col: str, cutoff: str
) -> list[str]:
    """Return group keys (``key_col`` values) with a dated row all before the
    cutoff. MAX(timestamp_utc) IS NOT NULL excludes all-NULL-timestamp groups
    (conservative — never pruned in isolation, F12). MAX(...) < cutoff is
    equivalent to NOT EXISTS a row at/after the cutoff (NULLs are ignored by
    MAX and never satisfy ``>= cutoff``)."""
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
    """F12: for rows with a NULL identity column, group by source_path so they
    are pruned as a bounded unit rather than orphaned unbounded."""
    sql = (
        f"SELECT source_path FROM {table} "
        f"WHERE {key_col} IS NULL "
        f"GROUP BY source_path "
        f"HAVING MAX(timestamp_utc) IS NOT NULL AND MAX(timestamp_utc) < ?"
    )
    return [row[0] for row in conn.execute(sql, (cutoff,))]


def _prune_claude(
    conn: sqlite3.Connection,
    cutoff: str,
    *,
    after_group: "Callable[[], None] | None" = None,
) -> tuple[int, int]:
    sessions = 0
    messages = 0
    for session_id in _prunable_groups(
        conn, "conversation_messages", "session_id", cutoff
    ):
        conn.execute(
            "DELETE FROM conversation_file_touches WHERE session_id = ?",
            (session_id,),
        )
        cur = conn.execute(
            "DELETE FROM conversation_messages WHERE session_id = ?",
            (session_id,),
        )
        messages += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.execute(
            "DELETE FROM conversation_ai_titles WHERE session_id = ?",
            (session_id,),
        )
        conn.execute(
            "DELETE FROM conversation_sessions WHERE session_id = ?",
            (session_id,),
        )
        sessions += 1
        if after_group is not None:
            after_group()
    for source_path in _prunable_null_identity_paths(
        conn, "conversation_messages", "session_id", cutoff
    ):
        # NULL-session messages carry no session_id-keyed titles/rollup and
        # conversation_file_touches requires a NOT NULL session_id, so delete
        # any stray touches by message_id, then the messages themselves.
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM conversation_messages "
                "WHERE session_id IS NULL AND source_path = ?",
                (source_path,),
            )
        ]
        if ids:
            conn.executemany(
                "DELETE FROM conversation_file_touches WHERE message_id = ?",
                [(i,) for i in ids],
            )
        cur = conn.execute(
            "DELETE FROM conversation_messages "
            "WHERE session_id IS NULL AND source_path = ?",
            (source_path,),
        )
        messages += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        sessions += 1
        if after_group is not None:
            after_group()
    return sessions, messages


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


def _reclaim_incremental_vacuum(conn: sqlite3.Connection) -> None:
    """Drive zero-column incremental-vacuum rows to completion portably."""
    remaining = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    if remaining <= 0:
        return
    chunk_pages = 4096
    max_passes = (remaining + chunk_pages - 1) // chunk_pages + 1
    for _ in range(max_passes):
        requested = min(remaining, chunk_pages)
        # executescript() routes through sqlite3_exec(), which steps zero-column
        # pragma rows through SQLITE_DONE on Python/SQLite combinations where a
        # Cursor.fetchall() can stop after the first row (public Linux 3.11).
        conn.executescript(f"PRAGMA incremental_vacuum({requested});")
        after = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        if after <= 0:
            return
        if after >= remaining:
            return
        remaining = after


def _maybe_prune_conversation_retention(
    conn: sqlite3.Connection,
    *,
    now_utc: dt.datetime,
    retention_days: int,
    force: bool = False,
) -> "PruneStats | None":
    """Throttled, flock-serialized transcript retention prune (F7 + F9).

    Returns the :class:`PruneStats` when a prune ran, or ``None`` when it was
    skipped (retention disabled, throttled within 24h, or a lock contended).

    Concurrency (F7): a dedicated non-blocking MAINTENANCE flock serializes prune
    attempts across processes (a second dashboard skips cleanly). Under it, the
    two provider flocks are taken in a FIXED order (Claude then Codex),
    non-blocking, so a rebuild/reingest mid-flight makes the prune skip this
    cycle rather than race between candidate selection and deletion. The prune of
    each whole session/conversation runs in its own ``BEGIN IMMEDIATE``
    transaction (#315), while every flock remains held for the full pass. The
    throttle stamp is committed only after both provider phases succeed. A
    failure rolls back the active group but preserves completed groups, writes no
    stamp, and therefore retries the remainder next cycle.

    ``conn`` must hold no provider flock and no open transaction (the caller
    guarantees this — the dashboard opens a dedicated cache connection; the
    from-zero-replay callers invoke it after releasing their sync flock, F9).
    ``retention_days <= 0`` disables retention (keep forever).
    """
    if retention_days is None or retention_days <= 0:
        return None
    core = _cctally_core
    try:
        core.APP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    maint_fh = open(core.CACHE_LOCK_MAINTENANCE_PATH, "w")
    try:
        try:
            fcntl.flock(maint_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return None  # another prune holds it; skip cleanly, do NOT stamp
        if not force and not _retention_due(conn, now_utc):
            return None
        claude_fh = open(core.CACHE_LOCK_PATH, "w")
        try:
            try:
                fcntl.flock(claude_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return None  # a Claude sync is mid-flight; retry next cycle
            codex_fh = open(core.CACHE_LOCK_CODEX_PATH, "w")
            try:
                try:
                    fcntl.flock(codex_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    return None  # a Codex sync is mid-flight; retry next cycle
                cutoff = now_utc - dt.timedelta(days=int(retention_days))
                conn.execute("BEGIN IMMEDIATE")
                try:
                    def commit_group() -> None:
                        conn.commit()
                        conn.execute("BEGIN IMMEDIATE")

                    stats = prune_conversation_transcripts(
                        conn,
                        cutoff_utc=cutoff,
                        after_group=commit_group,
                    )
                    _stamp_retention_prune(conn, now_utc)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                # Return the freed pages to the OS. On an INCREMENTAL auto-vacuum
                # cache.db (the default for freshly-created DBs, #313 P3) this
                # shrinks the file on disk instead of leaving a growing freelist,
                # so the transcript prune reclaims space automatically without a
                # manual `cctally db vacuum`; on a legacy auto_vacuum=NONE cache it
                # is a harmless no-op (those still reclaim via `db vacuum` or a
                # `cache-sync --rebuild`). Runs OUTSIDE the committed transaction,
                # still under the maintenance + provider flocks (no concurrent
                # writer), and best-effort — a reclaim error must never fail the
                # already-durable prune.
                if stats.total_rows > 0:
                    try:
                        # Use the sqlite3_exec path and verify progress between
                        # bounded chunks. This clears the freelist and drops
                        # page_count; the physical file shrinks on the next
                        # `wal_checkpoint(TRUNCATE)` the sync loop forces (#297).
                        _reclaim_incremental_vacuum(conn)
                    except sqlite3.Error:
                        pass
                return stats
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


def _delete_codex_conversation_derived(
    conn: sqlite3.Connection, conversation_key: str
) -> None:
    """#294 S6: drop one pruned conversation's normalized derived rows in the same
    transaction as its physical events.

    #313 prunes at WHOLE-conversation granularity — ``_prune_codex`` deletes every
    ``codex_conversation_events`` row for the key, so every event across every file
    of the conversation is gone. The S6 rollup ownership rule (§3.2) is therefore
    the "or-delete" branch: with zero surviving normalized rows the rollup is
    deleted outright, no recompute needed. The normalized-message DELETE is a
    PARTIAL delete over the whole corpus (§3.4), so it rides the per-row FTS
    AFTER-DELETE trigger — surviving conversations keep their postings — and NEVER
    the full-clear ``'delete-all'``; when ``codex_fts_unavailable`` is set the
    triggers are absent and these are plain base deletes. File touches carry an
    explicit ``conversation_key`` so they scope exactly. Deleting the derived rows
    is part of pruning the SAME conversation, so it does not change the reported
    ``codex_conversations`` / ``codex_events`` counts (those stay physical).
    """
    conn.execute(
        "DELETE FROM codex_conversation_file_touches WHERE conversation_key = ?",
        (conversation_key,),
    )
    # Rides the codex_conv_fts_ad per-row trigger (partial delete, §3.4).
    conn.execute(
        "DELETE FROM codex_conversation_messages WHERE conversation_key = ?",
        (conversation_key,),
    )
    conn.execute(
        "DELETE FROM codex_conversation_rollups WHERE conversation_key = ?",
        (conversation_key,),
    )


def _prune_codex(
    conn: sqlite3.Connection,
    cutoff: str,
    *,
    after_group: "Callable[[], None] | None" = None,
) -> tuple[int, int]:
    conversations = 0
    events = 0
    for conversation_key in _prunable_groups(
        conn, "codex_conversation_events", "conversation_key", cutoff
    ):
        # #294 S6: purge the derived normalized rows for this conversation in the
        # SAME transaction as the events (§3.2 or-delete + §3.4 partial delete).
        _delete_codex_conversation_derived(conn, conversation_key)
        cur = conn.execute(
            "DELETE FROM codex_conversation_events WHERE conversation_key = ?",
            (conversation_key,),
        )
        events += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conversations += 1
        if after_group is not None:
            after_group()
    for source_path in _prunable_null_identity_paths(
        conn, "codex_conversation_events", "conversation_key", cutoff
    ):
        # NULL-conversation_key events never gained thread identity, so S6 never
        # normalized them (§4.1) — there are no derived rows to clean up here.
        cur = conn.execute(
            "DELETE FROM codex_conversation_events "
            "WHERE conversation_key IS NULL AND source_path = ?",
            (source_path,),
        )
        events += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conversations += 1
        if after_group is not None:
            after_group()
    return conversations, events
