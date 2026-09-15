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
import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

import _cctally_core

UTC = dt.timezone.utc

# cache_meta throttle key (framework-untracked KV — NO schema migration, F7).
_RETENTION_LAST_PRUNE_KEY = "conversation_retention_last_prune_at"
_RETENTION_THROTTLE_SECONDS = 24 * 60 * 60

# --------------------------------------------------------------------------
# #780 — bounded, resumable reclaim
# --------------------------------------------------------------------------

#: Durable reclaim state (framework-untracked KV, same class as the throttle
#: key above — a value shape, not a schema change).
RECLAIM_PENDING_KEY = "conversation_retention_reclaim_pending"

#: One reclaim pass gets this much wall clock. The pass runs under the
#: maintenance flock and BOTH provider flocks, so an unbounded run holds all
#: three: the previous implementation drove the freelist to completion in
#: 4096-page chunks with no budget at all, and on a multi-GiB store that is
#: minutes of held locks against a live reader surface.
#:
#: THE DEADLINE IS CHECKED BETWEEN CHUNKS, so a pass can exceed it by at most
#: one chunk, and that bound is stated rather than hidden because
#: `PRAGMA incremental_vacuum(n)` cannot be preempted. Chunk size is the only
#: control, and the cost of a chunk is dominated by CACHE STATE, not by `n`.
#:
#: Measured on a copy-on-write clone of the production store, cold: 1 page
#: 0.006 s, 8 pages 0.001 s, 64 pages 0.009 s, 512 pages 7.754 s. An earlier
#: version of this comment read that series as per-page cost rising with chunk
#: size and concluded that "no estimator fixes this". The Tranche 3 review
#: disproved it and the correction matters, because the false version tells
#: the next maintainer not to bother measuring. Two facts settle it. Warm
#: passes on the SAME store sustain about 2,300 pages a second at 1,024 and
#: 2,048-page chunks, so a large chunk is not intrinsically expensive per page.
#: And the 8-page chunk being SIX TIMES cheaper than the 1-page chunk is not a
#: size effect at all — it is the second reading of a cold ramp warming up.
#: The 512-page chunk is the fourth reading of that ramp, and it is expensive
#: because it is the first one to reach a genuinely cold region of a multi-GiB
#: file, not because it asked for 512 pages.
#:
#: So the real hazard is that the ramp doubles into cold territory before it
#: has measured a slow chunk there — an estimator built from warm readings
#: cannot see the first-touch cost of pages nobody has read yet. The growth cap
#: below is the control that matters for exactly that reason, and a plausible
#: refinement, not taken here because it is unmeasured, is to require two or
#: three consecutive chunks at the current size before permitting a doubling,
#: so a size increase always rests on more than one observation.
#:
#: Observed worst case on that store: 9.9 s for the first, cold pass, then
#: 2.05-2.11 s for every warm pass. That is the overshoot this design accepts,
#: and it is bounded and reported (`deadline_hit`) rather than unbounded: the
#: implementation it replaces held the maintenance flock and both provider
#: flocks for the whole drain, which on the same store was part of a 25-minute
#: rebuild.
RECLAIM_DEADLINE_SECONDS = 2.0

#: Chunk sizing. Start small so the FIRST chunk cannot overshoot the deadline
#: on a cold multi-GiB file, then adapt upward from measured throughput —
#: DOUBLING at most, and from the SLOWEST rate this pass has seen rather than
#: the most recent one. Neither is a cure — see the cold-cache first-touch cost
#: recorded on the deadline above — but together they bound the growth to one
#: doubling per chunk, so the pass takes many measured steps and the single
#: chunk that can overshoot is at most twice the last chunk that fit inside the
#: budget.
RECLAIM_INITIAL_PAGES = 64
RECLAIM_MAX_PAGES = 2048
RECLAIM_CHUNK_GROWTH_FACTOR = 2

#: Backlog bounds. "Unbounded growth is not accepted" needs numbers, so these
#: are them, and each has a distinct consequence.
#:   * above ESCALATION, reclaim stops waiting for the daily throttle and
#:     continues on the next cycle;
#:   * below MIN_PAGES_PER_SECOND, the pass is recorded as making no progress
#:     and stays pending — it is never declared complete;
#:   * above CEILING, doctor raises a FAIL and a new rebuild is refused,
#:     because a rebuild adds staging churn to a store already failing to drain.
#:
#: THE CEILING IS SIZED FROM A MEASUREMENT, not chosen. One full rebuild of a
#: copy-on-write clone of the 9.2 GB production store left 1,398,528 free pages
#: — 5.33 GiB, 37% of the file — because reclaim is now budgeted and the
#: remainder is deferred. A ceiling below that would FAIL doctor and refuse the
#: next rebuild after every ordinary production-scale rebuild, which is a false
#: alarm rather than an enforcement mechanism. 16 GiB is three times the
#: measured single-rebuild leftover.
#:
#: The escalation threshold is what actually drains it. Measured steady rate on
#: the same clone: about 2,300 pages a second, so roughly 4,600 pages a 2 s
#: pass, so about 300 passes for that backlog. At the daily throttle that is
#: 300 days; at the escalated per-cycle cadence it is under an hour. That
#: ratio is the reason escalation exists.
RECLAIM_ESCALATION_BYTES = 256 * 1024 * 1024
RECLAIM_MIN_PAGES_PER_SECOND = 1.0
RECLAIM_CEILING_BYTES = 16 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class ReclaimOutcome:
    """What one bounded reclaim pass observed. Never a claim about the file."""

    freelist_before: int = 0
    freelist_after: int = 0
    page_size: int = 0
    pages_reclaimed: int = 0
    duration_s: float = 0.0
    deadline_hit: bool = False

    @property
    def unreclaimed_bytes(self) -> int:
        return max(0, self.freelist_after) * max(0, self.page_size)

    @property
    def made_progress(self) -> bool:
        if self.pages_reclaimed <= 0:
            return False
        if self.duration_s <= 0:
            return True
        return (self.pages_reclaimed / self.duration_s) >= (
            RECLAIM_MIN_PAGES_PER_SECOND)

    @property
    def freelist_drained(self) -> bool:
        return self.freelist_after <= 0


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


def _run_incremental_vacuum_chunk(conn: sqlite3.Connection, pages: int) -> int:
    """Reclaim at most ``pages`` freelist pages; return the freelist count after.

    The one seam every chunk goes through, so a test can replace it to hold the
    real SQLite write lock at a barrier and observe what a concurrent reader
    does. ``executescript()`` routes through ``sqlite3_exec()``, which steps
    zero-column pragma rows through ``SQLITE_DONE`` on Python/SQLite
    combinations where ``Cursor.fetchall()`` can stop after the first row
    (public Linux 3.11).
    """
    conn.executescript(f"PRAGMA incremental_vacuum({int(pages)});")
    return int(conn.execute("PRAGMA freelist_count").fetchone()[0])


def _reclaim_incremental_vacuum(
    conn: sqlite3.Connection,
    *,
    deadline_seconds: float = RECLAIM_DEADLINE_SECONDS,
    clock: "Callable[[], float]" = time.monotonic,
) -> ReclaimOutcome:
    """Reclaim freelist pages under a monotonic wall-clock budget (#780).

    The pass runs under the maintenance flock and both provider flocks, so its
    cost is a cost every reader and both ingesters pay. It therefore stops at
    the deadline and leaves the remainder for the next pass rather than driving
    the freelist to zero however long that takes.

    Chunks start small and adapt to measured throughput, so a slow disk cannot
    overshoot the budget on the first chunk and a fast one is not held to 64
    pages a chunk for the whole pass. The freelist is re-read after every
    chunk, which is also how a chunk that reclaimed nothing is detected.
    """
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    before = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    started = clock()
    if before <= 0:
        return ReclaimOutcome(
            freelist_before=before, freelist_after=before, page_size=page_size)
    remaining = before
    pages = RECLAIM_INITIAL_PAGES
    # The SLOWEST rate observed in THIS pass, not the most recent one. A cold
    # store's per-page cost is about twenty times its warm cost, so sizing the
    # next chunk from the chunk just measured is optimistic exactly when it
    # matters: measured on a clone of the production store, that estimator ran
    # a first pass to 7.9 s against a 2.0 s budget. Taking the minimum makes
    # one slow chunk hold the rest of the pass conservative, and the estimate
    # resets each pass, so a warm pass still ramps.
    slowest_rate = None
    deadline_hit = False
    while remaining > 0:
        elapsed = clock() - started
        if elapsed >= deadline_seconds:
            deadline_hit = True
            break
        requested = min(remaining, pages)
        chunk_started = clock()
        after = _run_incremental_vacuum_chunk(conn, requested)
        chunk_elapsed = max(clock() - chunk_started, 0.0)
        if after >= remaining:
            # The chunk reclaimed nothing. Continuing would spin against a
            # store that is not releasing pages.
            remaining = after
            break
        if chunk_elapsed > 0:
            # Aim each chunk at a quarter of the remaining budget, so the pass
            # takes several measured steps rather than one guess — and never
            # more than DOUBLE the chunk just measured, so a cold first chunk
            # cannot extrapolate the pass past its deadline.
            rate = (remaining - after) / chunk_elapsed
            slowest_rate = (
                rate if slowest_rate is None else min(slowest_rate, rate))
            budget_left = max(deadline_seconds - (clock() - started), 0.0)
            pages = int(max(
                RECLAIM_INITIAL_PAGES,
                min(RECLAIM_MAX_PAGES,
                    slowest_rate * budget_left / 4,
                    requested * RECLAIM_CHUNK_GROWTH_FACTOR),
            ))
        remaining = after
    return ReclaimOutcome(
        freelist_before=before,
        freelist_after=remaining,
        page_size=page_size,
        pages_reclaimed=max(0, before - remaining),
        duration_s=max(clock() - started, 0.0),
        deadline_hit=deadline_hit,
    )


def _resolve_main_db_path(conn: sqlite3.Connection):
    """The file behind this connection's ``main`` schema.

    Read off the connection rather than assumed, because the orchestrator is
    called with a conversations connection by the dashboard and with a cache
    connection by the from-zero-replay callers; measuring the wrong family's
    size would report a reclaim against a file the pass never touched. Falls
    back to the conversations path when the connection reports no file, which
    is the in-memory case.
    """
    try:
        for _seq, name, file_name in conn.execute("PRAGMA database_list"):
            if name == "main" and file_name:
                return file_name
    except sqlite3.Error:
        pass
    return _cctally_core.CONVERSATIONS_DB_PATH


def _db_family_bytes(path) -> int:
    """Database plus WAL plus shm, in bytes. Missing members count as zero."""
    total = 0
    base = str(path)
    for member in (base, base + "-wal", base + "-shm"):
        try:
            total += os.path.getsize(member)
        except OSError:
            continue
    return total


def _checkpoint_truncate(conn: sqlite3.Connection) -> "tuple[int, int, int] | None":
    """`wal_checkpoint(TRUNCATE)`, returning its `(busy, log, checkpointed)` row.

    Completion is PHYSICAL, not a freelist reading. `incremental_vacuum` moves
    pages off the freelist and drops `page_count`, but the bytes stay in the
    WAL until it is truncated, so clearing the pending state at
    `freelist_count == 0` would declare success while the space is still
    occupied. Returns None when the checkpoint could not run at all.
    """
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return (int(row[0]), int(row[1]), int(row[2]))


def read_reclaim_pending(conn: sqlite3.Connection) -> "dict | None":
    """The durable reclaim backlog record, or None when nothing is pending."""
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
        state = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _write_reclaim_pending(conn: sqlite3.Connection, state: "dict | None") -> None:
    if state is None:
        conn.execute("DELETE FROM cache_meta WHERE key=?", (RECLAIM_PENDING_KEY,))
        return
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (RECLAIM_PENDING_KEY, json.dumps(state, sort_keys=True)),
    )


def reclaim_backlog_bytes(state: "dict | None") -> int:
    """Unreclaimed bytes a pending record reports, or 0 when nothing is."""
    if not state:
        return 0
    try:
        return max(0, int(state.get("unreclaimed_bytes") or 0))
    except (TypeError, ValueError):
        return 0


def reclaim_backlog_escalated(state: "dict | None") -> bool:
    """Whether the backlog is large enough to stop waiting a whole day."""
    return reclaim_backlog_bytes(state) >= RECLAIM_ESCALATION_BYTES


def reclaim_backlog_over_ceiling(state: "dict | None") -> bool:
    """Whether the backlog has reached the hard ceiling: doctor FAIL, and a new
    rebuild is refused because it would add staging churn to a store that is
    already failing to drain."""
    return reclaim_backlog_bytes(state) >= RECLAIM_CEILING_BYTES


def run_reclaim_pass(
    conn: sqlite3.Connection,
    *,
    now_utc: dt.datetime,
    db_path=None,
    deadline_seconds: float = RECLAIM_DEADLINE_SECONDS,
    clock: "Callable[[], float]" = time.monotonic,
) -> "dict":
    """One bounded reclaim + checkpoint pass, with durable pending state.

    Returns a phase record: ``{reclaim: {...}, checkpoint: {...}, pending: ...}``.
    The pending record is retained until a MEASURED reduction in the database
    family's size, or the checkpoint's own result, shows the space was really
    returned. A pass that made no progress stays pending and is reported; it is
    never declared complete.
    """
    path = _resolve_main_db_path(conn) if db_path is None else db_path
    family_before = _db_family_bytes(path)
    outcome = _reclaim_incremental_vacuum(
        conn, deadline_seconds=deadline_seconds, clock=clock)
    checkpoint = _checkpoint_truncate(conn)
    family_after = _db_family_bytes(path)
    returned = max(0, family_before - family_after)
    complete = (
        outcome.freelist_drained
        and checkpoint is not None
        and checkpoint[0] == 0
        and checkpoint[1] == 0
    )
    state = None
    if not complete:
        state = {
            "freelist_count": outcome.freelist_after,
            "unreclaimed_bytes": outcome.unreclaimed_bytes,
            "attempted_at": now_utc.astimezone(UTC).isoformat(),
            "made_progress": bool(outcome.made_progress),
            "deadline_hit": bool(outcome.deadline_hit),
        }
    # Write and commit ONLY when the record actually changed. A pass over a
    # store with no backlog would otherwise issue a no-op DELETE and a commit
    # on every cycle, which is both pointless and visible to callers counting
    # transaction boundaries.
    if state != read_reclaim_pending(conn):
        _write_reclaim_pending(conn, state)
        conn.commit()
    return {
        "reclaim": {
            "pages_reclaimed": outcome.pages_reclaimed,
            "freelist_before": outcome.freelist_before,
            "freelist_after": outcome.freelist_after,
            "duration_s": outcome.duration_s,
            "deadline_hit": outcome.deadline_hit,
            "made_progress": outcome.made_progress,
        },
        "checkpoint": {
            "result": checkpoint,
            "bytes_returned": returned,
            "family_bytes": family_after,
        },
        "pending": state,
        "complete": complete,
    }


def _maybe_prune_conversation_retention(
    conn: sqlite3.Connection,
    *,
    now_utc: dt.datetime,
    retention_days: int,
    force: bool = False,
    record_phase: "Callable[[str, dict], None] | None" = None,
) -> "PruneStats | None":
    """Throttled, flock-serialized transcript retention prune (F7 + F9).

    Returns the :class:`PruneStats` when a prune ran, or ``None`` when it was
    skipped (retention disabled, throttled within 24h, or a lock contended).

    Concurrency (F7): a dedicated non-blocking MAINTENANCE flock serializes prune
    attempts across processes (a second dashboard skips cleanly). It is claimed
    EXCLUSIVE and then downgraded to SHARED for the pass proper, so a long prune
    cannot starve the fail-closed panel readers that sample it
    ``LOCK_SH | LOCK_NB``; a rival ``LOCK_EX | LOCK_NB`` claim still fails
    against the held SHARED, so the serialization is unchanged. Under it, the
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
    maint_fh = open(core.CONVERSATIONS_LOCK_MAINTENANCE_PATH, "w")
    try:
        try:
            fcntl.flock(maint_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return None  # another prune holds it; skip cleanly, do NOT stamp
        # The daily throttle governs DELETION. A reclaim backlog above the
        # escalation threshold still continues on the next cycle, because a
        # doctor warning once a day is a report, not an enforcement mechanism.
        reclaim_only = False
        if not force and not _retention_due(conn, now_utc):
            if not reclaim_backlog_escalated(read_reclaim_pending(conn)):
                return None
            reclaim_only = True
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
                # Downgrade the maintenance flock to SHARED for the pass proper.
                # The EXCLUSIVE acquire above is what wins the race; holding it
                # exclusive for the whole pass additionally locked out the
                # fail-CLOSED panel readers, which take this flock
                # `LOCK_SH | LOCK_NB` and blank their column on any contention
                # (`read_session_titles_bounded` -> every Claude session title in
                # the Recent Sessions card, for the minutes a large prune runs).
                # A prune is a writer, not a family replacement: concurrent
                # writes are already excluded by the two provider flocks, and
                # the replacement paths readers guard against take this flock
                # EXCLUSIVE, which a held SHARED still blocks. Rival prunes and
                # `db vacuum` claim `LOCK_EX | LOCK_NB`, which also still fails.
                # MUST stay after both provider flocks: a flock conversion is
                # not atomic, so a rival can slip into the downgrade window —
                # the provider flocks it then fails to claim are what make that
                # harmless.
                try:
                    fcntl.flock(maint_fh, fcntl.LOCK_SH)
                except OSError:
                    pass  # keep the exclusive hold; correctness is unchanged
                if reclaim_only:
                    # Deletion is throttled; the backlog is not. Nothing here
                    # stamps the throttle, so the ordinary daily deletion still
                    # happens on its own schedule.
                    try:
                        phases = run_reclaim_pass(conn, now_utc=now_utc)
                    except sqlite3.Error:
                        phases = None
                    if phases is not None and record_phase is not None:
                        record_phase("reclaim", phases["reclaim"])
                        record_phase("checkpoint", phases["checkpoint"])
                    return None
                cutoff = now_utc - dt.timedelta(days=int(retention_days))
                conn.execute("BEGIN IMMEDIATE")
                try:
                    def commit_group() -> None:
                        conn.commit()
                        conn.execute("BEGIN IMMEDIATE")

                    delete_started = time.monotonic()
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
                if record_phase is not None:
                    record_phase("delete", {
                        "duration_s": max(
                            time.monotonic() - delete_started, 0.0),
                        "claude_messages": stats.claude_messages,
                        "codex_events": stats.codex_events,
                    })
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
                # Deletion is complete and durable at this point, and only
                # deletion completion is stamped. Reclaim is a SEPARATE phase
                # with its own budget and its own durable pending state, so a
                # pass that runs out of budget continues on the next cycle
                # instead of holding three flocks until the freelist drains.
                #
                # A pass with no new deletion still continues an outstanding
                # backlog: the previous implementation reclaimed only when the
                # prune had deleted something, so a store that fell behind had
                # no way to catch up.
                pending_before = read_reclaim_pending(conn)
                if stats.total_rows > 0 or pending_before is not None:
                    try:
                        phases = run_reclaim_pass(conn, now_utc=now_utc)
                    except sqlite3.Error:
                        phases = None
                    if phases is not None and record_phase is not None:
                        record_phase("reclaim", phases["reclaim"])
                        record_phase("checkpoint", phases["checkpoint"])
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
