"""Fail-safe dashboard ingest frontier (issue #680).

The provider hooks append the path named by their trusted event payload.  A
dashboard that has completed one ordinary full sync can then distinguish an
append target from a structurally unchanged tree without globbing or statting
every JSONL file.  Directory identity covers add/delete/rename; database and
schema identity cover replacement/migration.  Missing, malformed, truncated,
or otherwise ambiguous evidence always returns ``full``.

Every one of those guards needs a writer to move something.  A provider whose
hooks never run therefore holds its certificate while its sources grow, so a
certificate also expires purely on elapsed time after
``FRONTIER_CERTIFICATE_MAX_AGE_SECONDS``.

Identity is not continuity, though.  The journal is rotated by replacement and
an inode number is reusable, so #769 S6 gives the ledger a durable generation
and a globally monotonic, gap-free sequence: every ticket carries both, each
consumer records the ``(epoch, sequence)`` it acknowledged, and seven named
discontinuities force an exhaustive walk instead of a targeted one.  Six are
properties of one consumer's own slice — a changed generation, an
acknowledgement past the tail, a missing, duplicated or regressed sequence,
and a pre-protocol ticket — and the seventh is the cross-consumer one: two
consumers whose acknowledgements name different generations cannot tell which
tickets the other already consumed, so neither may plan a targeted pass.

The activity journal is private runtime state.  Diagnostic surfaces publish
only the bounded ``mode``/``reason`` enums and counts, never its paths.
"""
from __future__ import annotations

import json
import fcntl
import os
import secrets
import sqlite3
import threading
import pathlib
import shlex
import time
from dataclasses import dataclass
from typing import NamedTuple
from _lib_retained_size import retained_size_bytes

#: The default for ``plan_provider``'s ``configuration_generation``, meaning
#: "derive it from the guard paths". An explicit ``None`` is a different
#: statement — the caller looked and found no configuration evidence — and the
#: two must stay distinguishable, so the default cannot itself be ``None``.
_DERIVE_CONFIGURATION_GENERATION = object()

_MARKER_NAME = "dashboard-ingest-activity.jsonl"
_MARKER_LOCK_NAME = "dashboard-ingest-activity.lock"
_LEDGER_STATE_NAME = "dashboard-ingest-activity.state.json"
_MARKER_ROTATE_BYTES = 4 * 1024 * 1024
_PROVIDERS = frozenset({"claude", "codex"})
FRONTIER_MAX_BYTES = 16 * 1024 * 1024
# Upper bound on how long one certificate may stand without an exhaustive
# walk.  Every other guard compares evidence some writer must produce -- a hook
# ticket, a directory mtime, a schema bump -- and appending to an
# already-tracked file moves none of them.  Elapsed time is the only evidence
# that accrues without anyone's cooperation, so this bound is what makes the
# worst-case staleness finite when a provider's hooks are absent, disabled or
# untrusted.  One walk per interval per provider is the whole cost.
#
# 120s measured against the operator's production store (2,854 Codex rollouts,
# 197k accounting entries), where one accounting-only walk costs 2.1-23.1s with
# a ~7s median.  That is roughly 6% of one core, against ~12% at 60s and the
# ~44% the unconditional per-tick walk this replaced was measured at.  A live
# tail advances its own conversation's accounting directly, so this bound is
# the safety net for surfaces nobody is watching, not the interactive path.
FRONTIER_CERTIFICATE_MAX_AGE_SECONDS = 120.0
CODEX_FULL_WALK_COMPLETE_KEY = "dashboard_codex_full_walk_complete"
_COMPLETE_KEYS = {
    "claude": "claude_ingest_walk_complete",
    "codex": CODEX_FULL_WALK_COMPLETE_KEY,
}
_PENDING_META_KEYS = (
    "conversation_backfill_pending",
    "conversation_sessions_backfill_pending",
    "codex_replay_from_zero_pending",
    "codex_replay_from_zero_blocked",
    "codex_replay_from_zero_deferred",
    "cache_creation_split_rewalk_pending",
)

# Transcript-store work which targeted ingest is not entitled to skip.  This
# is deliberately broader than the accounting frontier's pending set: these
# flags are consumed only by a full conversations pass, and a certificate made
# while one is present would strand that work forever.
_CONVERSATION_PENDING_META_KEYS = (
    "conversation_rebuild_claude_pending",
    "conversation_rebuild_codex_pending",
    "conversation_backfill_pending",
    "ai_titles_backfill_pending",
    "conversation_reingest_pending",
    "conversation_source_tool_use_reingest_pending",
    "conversation_reingest_enrichment_pending",
    "conversation_media_reingest_pending",
    "conversation_search_split_pending",
    "conversation_promote_command_args_pending",
    "conversation_sessions_backfill_pending",
    "conversation_queued_prompt_reingest_pending",
    "conversation_reingest_nested_agent_pending",
    "conversation_title_fts_backfill_pending",
    "conversation_reingest_file_touches_pending",
    "conversation_background_mcp_reingest_pending",
    "codex_conversation_replay_from_zero_pending",
    "codex_find_projection_backfill_pending",
)


def _now() -> float:
    """Monotonic seconds used to age certificates; never a wall clock.

    Certificates are process-local, so a monotonic source keeps the bound
    correct across system clock steps, suspend/resume and timezone changes.

    This is NOT the clock the recency window is measured on. See
    ``_recency_floor_iso``, which has to read the wall clock because the
    evidence it compares against is a wall-clock timestamp somebody else
    wrote.
    """
    return time.monotonic()


def activity_marker_path(app_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(app_dir) / _MARKER_NAME


def ledger_state_path(app_dir: pathlib.Path) -> pathlib.Path:
    """The durable ledger generation and counter beside the activity file.

    The tickets themselves cannot hold this state. Rotation replaces the file
    with one containing only the latest payload, so a counter recoverable only
    from the file's contents would restart at every rotation, and a consumer
    with no new tickets could not learn the current generation at all. Keeping
    it in a small sidecar written under the SAME writer lock gives both
    consumers one fact to compare against, at the cost of one tiny read per
    plan.
    """
    return pathlib.Path(app_dir) / _LEDGER_STATE_NAME


def _mint_ledger_epoch() -> str:
    """A generation token that can never collide with a previous one.

    An incrementing integer would be wrong here. The counter it would have to
    increment lives in the very file whose loss is one of the conditions this
    token exists to detect, so a fresh ledger would restart at the same epoch
    a consumer already acknowledged and the discontinuity would be invisible.
    """
    return secrets.token_hex(16)


class LedgerState(NamedTuple):
    """The ledger's durable generation, counter and last Codex configuration.

    A tuple rather than a record because both existing readers index it, and
    the third field is optional evidence rather than part of the continuity
    contract: it names the Codex hook configuration the MOST RECENT Codex
    ticket was written under, which is what makes "observed to have executed
    for this configuration generation" a claim with a source.
    """

    epoch: str
    sequence: int
    configuration_generation: "str | None" = None


def read_ledger_state(app_dir: pathlib.Path):
    """The ledger's state, or ``None`` when unknown.

    Absent, unreadable and malformed all report ``None``, which no
    acknowledged epoch matches, so every one of them forces recovery rather
    than being mistaken for continuity.
    """
    try:
        raw = ledger_state_path(app_dir).read_bytes()
    except OSError:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    epoch = value.get("epoch")
    sequence = value.get("sequence")
    if not isinstance(epoch, str) or not epoch:
        return None
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        return None
    if sequence < 0:
        return None
    generation = value.get("configuration_generation")
    if generation is not None and not isinstance(generation, str):
        generation = None
    return LedgerState(epoch, sequence, generation or None)


def _write_ledger_state(
    app_dir: pathlib.Path, epoch: str, sequence: int,
    configuration_generation: "str | None" = None,
) -> None:
    """Publish the ledger generation atomically, under the caller's lock."""
    target = ledger_state_path(app_dir)
    staging = target.with_name(target.name + ".next")
    body = {"epoch": epoch, "sequence": int(sequence)}
    if configuration_generation:
        body["configuration_generation"] = str(configuration_generation)
    payload = json.dumps(
        body, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(staging, target)


def classify_ledger_continuity(
    *, ack_epoch, ack_sequence: int, epoch, tail_sequence: int, sequences,
) -> "str | None":
    """Name the discontinuity in one consumed ledger slice, or return ``None``.

    SIX of the seven discontinuities are decided here, and all six are
    properties of one consumer's own slice. The seventh is cross-consumer and
    cannot be: two consumers whose acknowledgements name different generations
    are irreconcilable however self-consistent each one's slice is, and
    :func:`reconcile_acknowledgements` is what decides that one.

    Pure. ``sequences`` is the sequence number of every ticket the consumer is
    about to consume, in file order, with ``None`` standing for a ticket
    written before this protocol existed.

    The order of the checks is the order of decreasing scope: a generation
    that moved invalidates every sequence comparison inside it, and an
    acknowledgement past the ledger's own tail means the consumer's memory
    describes a ledger that no longer exists.
    """
    if epoch != ack_epoch:
        # `None == None` is the one continuous case here: a store where no
        # ticket has ever been written has no ledger, and the consumer
        # acknowledged exactly that. Any pre-protocol tickets it does hold are
        # caught below as legacy records rather than mistaken for continuity.
        return "ledger_epoch_changed"
    if ack_sequence > tail_sequence:
        return "ledger_cursor_beyond_tail"
    previous = int(ack_sequence)
    for raw in sequences:
        if raw is None:
            return "ledger_legacy_record"
        current = int(raw)
        if current == previous:
            return "ledger_sequence_duplicated"
        if current < previous:
            return "ledger_sequence_regressed"
        if current > previous + 1:
            return "ledger_sequence_gap"
        previous = current
    return None


class ObservedFile(NamedTuple):
    """One restat of a tracked rollout: what is at that pathname right now."""

    size: int
    device: int
    inode: int


def source_identity_replaced(
    committed_device, committed_inode, observed_device, observed_inode,
) -> bool:
    """Does a DIFFERENT file now occupy the pathname this cursor describes?

    The single point of truth for that verdict. `classify_recent_active_path`
    below and both Codex walks (`_cctally_cache.sync_codex_cache` and
    `sync_codex_conversations`) call it, because three copies of one
    comparison is exactly the shape in which one of them silently stops
    matching the others.

    THE INODE DECIDES; THE DEVICE ONLY CORROBORATES. `st_dev` is assigned when
    a volume is mounted rather than when a file is created, so a `$CODEX_HOME`
    on an external or network volume presents every retained rollout at a new
    device number after an ordinary remount, with no file changed at all.
    Deciding replacement on the device would therefore classify the WHOLE
    estate as replaced at once: every cursor resets to byte zero, every
    incarnation bumps, no stored account range survives the bump, and every
    rollout takes a fresh attribution from whichever account is logged in now.
    That is #416's re-derive-attribution-per-sync failure, reached by a
    filesystem event that changed no file. The device stays stored and stays
    available as corroborating evidence and as a diagnostic; it is never a
    decision input on its own.

    NO EVIDENCE degrades to False, which returns the caller to its size
    comparison — precisely the pre-#769 behaviour. It covers a missing INODE
    on either side, which is what every row written before #769 S6 carries and
    what a fresh column keeps until its path is next ingested, and it covers a
    stored value that is not an integer. The second case matters because a
    raise here escapes the per-file loop and takes every LATER file in the
    estate with it, so an unreadable identity must degrade rather than fail.

    NO-EVIDENCE IS THE INODE ALONE, because the DECISION is the inode alone.
    An earlier form also required both device columns, so a row carrying a
    real inode beside a NULL device answered "no evidence" and returned the
    caller to the size comparison, which lets a same-size replacement pass as
    unchanged. Both write sites stamp the pair from one `stat`, so no row in
    the shipped tree splits them; the guard is written against the decision
    rather than against the current shape of the rows.
    """
    if committed_inode is None or observed_inode is None:
        return False
    try:
        return int(committed_inode) != int(observed_inode)
    except (TypeError, ValueError):
        return False


def classify_recent_active_path(
    *, committed_size, committed_offset, committed_complete,
    committed_device, committed_inode, observed,
) -> str:
    """Classify one recently-active rollout against its committed cursor.

    Pure. Returns one of ``unchanged``, ``append``, ``resume``, ``replaced``,
    ``cursor_gap`` or ``vanished``.

    IDENTITY OUTRANKS SIZE. A larger file at a different inode is a
    replacement, not an append: its first byte is not the continuation of the
    committed offset, so resuming there would attribute bytes from one file to
    the cursor of another. `source_identity_replaced` above owns that verdict
    and states why the inode alone decides it.

    ``resume`` is what makes a budget-stopped generation recoverable. Its
    committed size is the whole observed size, so no size comparison can see
    the unread suffix; only the completion flag can.
    """
    if observed is None:
        return "vanished"
    if source_identity_replaced(
        committed_device, committed_inode, observed.device, observed.inode,
    ):
        return "replaced"
    floor = max(int(committed_size or 0), int(committed_offset or 0))
    if int(observed.size) < floor:
        return "cursor_gap"
    if int(observed.size) > int(committed_size or 0):
        return "append"
    if not committed_complete:
        return "resume"
    return "unchanged"


#: How long a Codex rollout stays in the recently-active set after cctally
#: last committed a read of it. Deliberately the SAME number as the
#: certificate age bound rather than a second knob, so that under a steady
#: clock the recency window ends exactly when the exhaustive backstop falls
#: due and no path is unwatched in between.
#:
#: THE TWO ARE MEASURED ON DIFFERENT CLOCKS, and an earlier version of this
#: comment claimed they "introduce no new time bound at all", which is false.
#: The certificate ages on ``time.monotonic()``; the recency window is a
#: comparison against ``codex_session_files.last_ingested_at``, which is a
#: wall-clock ISO timestamp the ingester wrote, so it can only be measured on
#: the wall clock. A forward clock step or a suspend and resume therefore
#: empties the recency set while the certificate is still young, and for the
#: remainder of that certificate's life a mid-turn append is invisible again.
#: That is a loss of freshness, not of correctness — it degrades to exactly
#: the pre-#724 behaviour, which the expiry backstop then answers — and a
#: BACKWARD step is safe in the other direction, because it only widens the
#: window and costs extra stats. The ``ingest_complete = 0`` leg of the
#: predicate is clock-independent, so pending work is never dropped by either.
FRONTIER_RECENT_ACTIVITY_SECONDS = FRONTIER_CERTIFICATE_MAX_AGE_SECONDS


def _recency_floor_iso(now=None) -> str:
    """The lower bound of the recency window, in the writers' own spelling.

    ``codex_session_files.last_ingested_at`` is written as
    ``datetime.now(timezone.utc).isoformat()`` at COMMIT time, which is
    cctally's own observation of when it read the file. Building the bound the
    same way keeps the comparison a plain string comparison over one format.

    WALL CLOCK, unavoidably. The stored value is a wall-clock timestamp
    written by a process that may no longer exist, so there is no monotonic
    reading of it to compare against. The certificate's own age bound uses
    ``_now()`` instead, and ``FRONTIER_RECENT_ACTIVITY_SECONDS`` records what
    the divergence between the two costs.
    """
    import datetime as _dt  # noqa: PLC0415 — stdlib, one call per plan

    moment = now or _dt.datetime.now(_dt.timezone.utc)
    return (
        moment - _dt.timedelta(seconds=FRONTIER_RECENT_ACTIVITY_SECONDS)
    ).isoformat()


#: Host parameters per `IN (...)` query. SQLite's compiled default limit is
#: 999 on the builds this ships against; 400 is the figure
#: `_load_codex_session_files_rows` already chunks at, restated rather than
#: imported so the two modules stay independent.
_CURSOR_QUERY_CHUNK = 400


def select_recently_active_codex_paths(
    conn, *, table: str = "codex_session_files", extra_paths=(),
) -> dict:
    """The bounded set of Codex rollouts one plan is entitled to restat.

    Returns ``{path: (size_bytes, last_byte_offset, ingest_complete,
    device_id, inode)}`` for the union of three sources: rollouts cctally
    itself committed a read of inside the recency window, paths an interrupted
    or budget-stopped generation left incomplete, and any ``extra_paths`` the
    caller names — the valid Codex tickets in the slice being consumed.

    RECENCY IS CCTALLY'S OWN COMMIT TIME. Not the rollout's event timestamps,
    which the observed process writes; not the file's mtime, which any writer
    moves; and not the time of a verification that found nothing changed,
    which would keep a quiet file in this set for the life of the process
    simply because it kept being looked at. Only a committed read advances
    ``last_ingested_at``, and only a committed read renews recency.

    ONE QUERY OVER ONE SMALL TABLE. Both cursor tables hold one row per
    retained rollout — about 2,854 on the operator's store — and this
    predicate is evaluated once per plan. It deliberately never touches
    ``codex_session_entries``, which holds ~197,000 rows and whose cost is the
    thing the whole frontier exists to avoid.

    ``table`` selects the consumer's own cursor table. Only
    ``codex_session_files`` carries ``ingest_complete``: the transcript
    ingester has no budgeted mid-file stop, so it has no incomplete state to
    resume and every one of its rows reports complete.
    """
    if table not in _CODEX_CURSOR_TABLES:
        raise ValueError("unknown codex cursor table")
    has_completion = _CODEX_CURSOR_TABLES[table]
    completion = "ingest_complete" if has_completion else "1"
    columns = (
        f"path, size_bytes, last_byte_offset, {completion}, device_id, inode")
    predicate = "last_ingested_at >= ?"
    if has_completion:
        predicate += " OR ingest_complete = 0"
    floor = _recency_floor_iso()
    selected: dict = {}

    def _absorb(rows):
        for path, size, offset, complete, device, inode in rows:
            if not path:
                continue
            selected[str(path)] = (
                int(size or 0), int(offset or 0), bool(complete),
                None if device is None else int(device),
                None if inode is None else int(inode),
            )

    _absorb(conn.execute(
        f"SELECT {columns} FROM {table} WHERE {predicate}", (floor,)))
    wanted = {str(item) for item in extra_paths if item}
    missing = sorted(wanted - set(selected))
    # CHUNKED, at the same 400 `_load_codex_session_files_rows` uses. One
    # `IN (...)` over the whole missing set raises `OperationalError` past
    # SQLite's host-parameter limit, and the caller catches that as
    # `codex_cursor_unavailable` — a reason that describes store STATE, so a
    # code defect would have been reported as an unmigrated store forever.
    for start in range(0, len(missing), _CURSOR_QUERY_CHUNK):
        chunk = missing[start:start + _CURSOR_QUERY_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        _absorb(conn.execute(
            f"SELECT {columns} FROM {table} WHERE path IN ({placeholders})",
            chunk))
    return selected


#: Deterministic work counters, by cursor table. `retained_file_visits` counts
#: the stats the RESTAT performs — one per member of the bounded recently-active
#: set — which is the quantity #786 bounds and the one an unbounded restat
#: would blow up. It is not the total number of stats a plan performs:
#: `_target_has_cursor_gap` and `_target_in_roots` each stat a ticketed path
#: as well and are deliberately not counted here, so on a ticketed append the
#: same path is statted at least twice against one recorded visit. The counter
#: is exact for the case it exists to protect — an idle caught-up tick names
#: no path at all, so it stats nothing and records nothing — and every extra
#: stat it omits is proportional to ticketed activity rather than to the
#: retained estate. Plain integers under the coordinator's own lock discipline
#: is enough: they are read by the benchmark harness and by nothing that makes
#: a decision.
_FRONTIER_COUNTERS: "dict[str, int]" = {}
_FRONTIER_COUNTERS_LOCK = threading.Lock()


def frontier_counters() -> dict:
    """A snapshot of the deterministic frontier work counters."""
    with _FRONTIER_COUNTERS_LOCK:
        return dict(_FRONTIER_COUNTERS)


def reset_frontier_counters() -> None:
    with _FRONTIER_COUNTERS_LOCK:
        _FRONTIER_COUNTERS.clear()


def _bump_counter(name: str, amount: int = 1) -> None:
    with _FRONTIER_COUNTERS_LOCK:
        _FRONTIER_COUNTERS[name] = _FRONTIER_COUNTERS.get(name, 0) + amount


def _observe_file(path: str, table: str) -> "ObservedFile | None":
    _bump_counter(f"retained_file_visits.{table}")
    try:
        st = pathlib.Path(path).stat()
    except OSError:
        return None
    return ObservedFile(st.st_size, st.st_dev, st.st_ino)


#: The two Codex cursor tables the restat may read, and whether each carries
#: an ``ingest_complete`` column. The accounting ingester has a budgeted
#: mid-file stop and therefore an incomplete state; the transcript ingester
#: does not.
_CODEX_CURSOR_TABLES = {
    "codex_session_files": True,
    "codex_conversation_source_files": False,
}


def _ingest_source_table(provider: str) -> str:
    """The ACCOUNTING ingester's cursor table for one provider.

    The twin of ``_conversation_source_table`` below, which answers the same
    question for the transcript ingester. Both exist so that no planner spells
    a table name out at its own call site: the accounting planner did, and one
    literal beside one derived sibling is exactly the shape in which the two
    stop matching.

    Only the Codex answer is ever handed to ``plan_recent_active_restat``.
    ``session_files`` is deliberately absent from ``_CODEX_CURSOR_TABLES``
    above, so reaching the restat with the Claude answer raises rather than
    reading a Codex table under a Claude plan. That refusal is a backstop, not
    the mechanism: ``_restat_and_return`` gates the restat on
    ``provider == "codex"`` and is what keeps the Claude answer away from it.
    """
    if provider == "claude":
        return "session_files"
    if provider == "codex":
        return "codex_session_files"
    raise ValueError("unknown provider")


def plan_recent_active_restat(
    conn, *, table: str = "codex_session_files", extra_paths=(),
):
    """Restat the bounded recent set and return ``(targets, escalation)``.

    ``targets`` are the paths whose suffix this tick should ingest;
    ``escalation`` is a refusal reason when one of them cannot be handled by
    targeted work at all, in which case the caller must walk exhaustively.

    An empty recent set performs ZERO stats, which is the whole shape of an
    idle caught-up tick on the production store.
    """
    try:
        candidates = select_recently_active_codex_paths(
            conn, table=table, extra_paths=extra_paths)
    except sqlite3.DatabaseError:
        # A store whose cursor columns this binary expects are absent is store
        # STATE, not a code defect: an opener that has not yet run the
        # migration, or a fixture standing in for the table. Escalating is the
        # conservative answer, and it must be an answer rather than a raise,
        # because the planner's own `except` clause does not cover
        # `sqlite3.DatabaseError` and an escape here would end the tick.
        return frozenset(), "codex_cursor_unavailable"
    targets: set[str] = set()
    for path, (size, offset, complete, device, inode) in sorted(
        candidates.items()
    ):
        verdict = classify_recent_active_path(
            committed_size=size,
            committed_offset=offset,
            committed_complete=complete,
            committed_device=device,
            committed_inode=inode,
            observed=_observe_file(path, table),
        )
        if verdict in {"append", "resume"}:
            targets.add(path)
        elif verdict == "replaced":
            return frozenset(), "source_replaced"
        elif verdict == "cursor_gap":
            return frozenset(), "cursor_gap"
        elif verdict == "vanished":
            return frozenset(), "filesystem_changed"
    return frozenset(targets), None


def _ledger_slice_decision(owner, provider: str, state, records):
    """The ledger verdict for one consumed slice, for EITHER consumer.

    Returns ``(reason, ledger_epoch, ledger_sequence)``. ``reason`` names one
    of the seven discontinuities, or is ``None`` when the slice is continuous
    and reconcilable, in which case the other two values are what a clean plan
    acknowledges.

    Single-sourced because the ledger is ONE file and the discontinuities are
    properties of it, not of whoever is reading it. The two consumers ran
    byte-identical copies of this block, comment text included, and a copy is
    exactly the shape in which one of them silently stops matching the other.
    """
    ledger = read_ledger_state(owner.app_dir)
    reason = classify_ledger_continuity(
        ack_epoch=state.ack_epoch,
        ack_sequence=state.ack_sequence,
        epoch=None if ledger is None else ledger.epoch,
        tail_sequence=0 if ledger is None else ledger.sequence,
        sequences=tuple(record[3] for record in records),
    )
    if reason is not None:
        return reason, None, 0
    # GUARDED, three lines after the same value was read defensively above.
    # `ledger` is `None` exactly when no ledger has ever been written AND this
    # consumer acknowledged that, which the classifier reports as continuous.
    # Production does not reach it, because `capture_cutoff` materializes the
    # sidecar before any seed; a caller that builds its own `MarkerCutoff`
    # does, and a bare `ledger[0]` there raises a `TypeError` that the
    # planner's `except (OSError, ValueError)` would not catch.
    ledger_epoch = None if ledger is None else ledger.epoch
    ledger_sequence = records[-1][3] if records else state.ack_sequence
    # The seventh discontinuity. Reached only when this consumer's own slice
    # is self-consistent, which is the whole reason it needs a separate check:
    # each consumer compares its acknowledgement against the SAME live ledger,
    # so a peer that has not planned since a rotation is the one state in
    # which two acknowledgements can name different generations while every
    # other guard is satisfied.
    mine = (
        None if not state.ack_epoch
        else (state.ack_epoch, state.ack_sequence)
    )
    for peer in owner._generations.peer_ledger_acknowledgements(
        provider, owner.CONSUMER,
    ):
        conflict = reconcile_acknowledgements(mine, peer)
        if conflict is not None:
            return conflict, None, 0
    return None, ledger_epoch, ledger_sequence


def _configuration_generation_reason(
    owner, provider: str, records, guard_paths, configuration_generation,
):
    """Escalate when a ticket's configuration is not the one on disk now.

    A ticket written under a configuration that has since changed is not
    evidence about the configuration on disk now: the handler that wrote it
    may no longer be the managed one, so naming its path as the only target
    would rest a precise claim on a stale observation. The ``guard_paths``
    mtime comparison in each planner is the coarse form of the same check;
    this one compares the bytes. It is a no-op when the caller supplies no
    configuration evidence AND no ticket carries any, because Claude tickets
    and the benchmarks legitimately carry none.
    """
    ticket_generations = {
        record[4] for record in records if record[0] == provider}
    stamped_generations = {
        item for item in ticket_generations if item is not None}
    current_generation = _resolve_configuration_generation(
        provider, guard_paths, configuration_generation)
    if current_generation is None and not stamped_generations:
        return None
    if all(
        classify_codex_execution_observed(
            observed_generation=stamped,
            current_generation=current_generation,
        )
        for stamped in ticket_generations
    ):
        return None
    if current_generation is not None and not codex_execution_observed(
        owner.app_dir, current_generation,
    ):
        # #769 S6: the divergence diagnostic. The hook derives its digest from
        # `_codex_lifecycle_roots()` and this module recomputes one from the
        # guard paths; they agree today because both funnel through
        # `codex_hook_roots(_codex_home_roots())`. A dashboard running under a
        # different `$CODEX_HOME` than the hooks would break that, every Codex
        # ticket would escalate here forever, and #724's fix would silently
        # stop applying. This counter is what names that state: it rises on
        # every plan while the ledger's own retained generation — written by
        # whichever hook last ran — never matches the one recomputed here.
        _bump_counter(f"configuration_unobserved.{provider}")
    return "configuration_generation_changed"


def _ticket_target_paths(provider: str, records, roots):
    """The ticketed paths this plan may target, or a refusal reason."""
    provider_paths = tuple(
        path for kind, path, _e, _s, _c in records if kind == provider)
    if any(not path for path in provider_paths):
        return None, "ambiguous_activity"
    paths = frozenset(provider_paths)
    if any(not _target_in_roots(path, roots) for path in paths):
        return None, "target_outside_scope"
    return paths, None


def _restat_and_return(
    provider: str, conn, *, cursor_table: str, paths, marker_end: int,
    ledger_epoch, ledger_sequence: int,
):
    """#724's bounded restat, then the caught-up or targeted plan it implies.

    A mid-turn append lands before the post-turn hook writes anything, so it
    moves no ticket, no directory mtime and no cursor, and the certificate's
    age bound would otherwise be the only evidence that ever finds it.
    Restatting the paths recent activity NAMES finds it on the next ordinary
    tick at a cost proportional to that activity, and an idle caught-up estate
    names nothing, so it stats nothing.

    Scoped to Codex: the measured blind spot and the cursor-table evidence
    this reads are Codex-specific, and Claude's behaviour is deliberately
    unchanged this session. ``cursor_table`` is the CALLER's own table — the
    two consumers hold independent offsets into the same files, so a path
    caught up in one store can be behind in the other.
    """
    restat_paths = frozenset()
    if provider == "codex":
        restat_paths, escalation = plan_recent_active_restat(
            conn, table=cursor_table, extra_paths=paths)
        if escalation is not None:
            return FrontierPlan(provider, "full", reason=escalation)
    combined = paths | restat_paths
    if not combined:
        return FrontierPlan(
            provider, "caught_up", marker_end=marker_end, reason="unchanged",
            ledger_epoch=ledger_epoch, ledger_sequence=ledger_sequence,
        )
    return FrontierPlan(
        provider,
        "targeted",
        paths=combined,
        marker_end=marker_end,
        # A path the restat found is not a path a ticket named, and a
        # diagnostic that called both "activity" would hide exactly the
        # discovery this session added.
        reason="activity" if paths else "recent_activity",
        ledger_epoch=ledger_epoch,
        ledger_sequence=ledger_sequence,
    )


def _plan_accounting(owner, provider, conn, **kwargs):
    return DashboardIngestFrontier._plan_uncounted(
        owner, provider, conn, **kwargs)


def _plan_conversation(owner, provider, conn, **kwargs):
    return ConversationSyncFrontier._plan_uncounted(
        owner, provider, conn, **kwargs)


def _counted_plan(owner, body, provider, conn, **kwargs):
    """Record one plan's deterministic shape, by provider, consumer and mode.

    The counters exist so the benchmark harness can assert the SHAPE of a tick
    rather than only its wall clock: an idle caught-up tick that quietly starts
    visiting every retained rollout is a regression no timing threshold would
    reliably catch on a fast machine.
    """
    plan = body(owner, provider, conn, **kwargs)
    consumer = owner.CONSUMER
    _bump_counter(f"plans.{consumer}.{provider}.{plan.mode}")
    if plan.mode == "full":
        _bump_counter(f"full_walks.{consumer}.{provider}")
    if plan.paths:
        _bump_counter(
            f"targeted_paths.{consumer}.{provider}", len(plan.paths))
    return plan


def _resolve_configuration_generation(provider: str, guard_paths, supplied):
    """The Codex configuration digest for one plan, or ``None``.

    The frontier recomputes it from the current files rather than being told,
    which keeps this protocol inside the two modules that own it: the guard
    set the planner already holds names the hook roots, and
    ``_lib_codex_hooks`` owns both directions of that relationship. A caller
    may still pass a value explicitly, which the tests and any future consumer
    with a cheaper source can do.

    Claude has no Codex hook configuration, and this protocol is scoped to
    Codex, so Claude always resolves to ``None`` and the check that reads it
    becomes a no-op.
    """
    if supplied is not _DERIVE_CONFIGURATION_GENERATION:
        return supplied
    if provider != "codex":
        return None
    try:
        import _lib_codex_hooks  # noqa: PLC0415 — optional, resolved per plan

        return _lib_codex_hooks.codex_configuration_generation_for_guard_paths(
            guard_paths)
    except Exception:
        # An unreadable configuration is evidence of nothing, and `None` never
        # matches a stamped generation, so a stamped ticket still forces the
        # exhaustive walk rather than being trusted by default.
        return None


def classify_codex_execution_observed(
    *, observed_generation, current_generation,
) -> bool:
    """Whether a Codex hook is observed to have run for THIS configuration.

    Pure. ``observed_generation`` is the configuration digest the most recent
    Codex ticket was written under; ``current_generation`` is the digest
    recomputed from the files on disk now.

    This is a strictly stronger claim than
    ``CodexHookObservation.observed_enabled``, which says only that the
    current slots classify as installed and enabled, and which is deliberately
    NOT relabelled: a root can be installed, enabled and trusted and still
    never have fired a hook. Both ``None`` cases are false — a configuration
    that cannot be read proves nothing, and a root that has written no ticket
    has produced no execution evidence at all.
    """
    if not observed_generation or not current_generation:
        return False
    return observed_generation == current_generation


def codex_execution_observed(app_dir, current_generation) -> bool:
    """The stored form of :func:`classify_codex_execution_observed`."""
    state = read_ledger_state(pathlib.Path(app_dir))
    return classify_codex_execution_observed(
        observed_generation=None if state is None else (
            state.configuration_generation),
        current_generation=current_generation,
    )


def reconcile_acknowledgements(first, second) -> "str | None":
    """Whether two consumers' acknowledgements describe one ledger.

    Pure. Each argument is an ``(epoch, sequence)`` pair or ``None`` for a
    consumer that has never acknowledged anything. Two consumers legitimately
    sit at different sequences within one generation, because they plan and
    commit independently. Two DIFFERENT generations are the irreconcilable
    case: neither consumer can tell which tickets the other one already
    consumed, so only an exhaustive walk restores a shared frontier.
    """
    if first is None or second is None:
        return None
    if first[0] != second[0]:
        return "ledger_acknowledgements_irreconcilable"
    return None


def invalidate_activity_marker(app_dir: pathlib.Path) -> bool:
    """Remove the activity certificate after a writer cannot append a ticket.

    Absence is an intentionally fail-closed marker state: every live frontier
    reader falls back to its ordinary full provider pass.  Serialize removal
    with writers and cutoff capture so a later successful ticket cannot be
    unlinked by an earlier failed hook.
    """
    marker = activity_marker_path(pathlib.Path(app_dir))
    lock_path = marker.with_name(_MARKER_LOCK_NAME)
    lock_fd = None
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        return True
    except OSError:
        # Even when the lock itself is unavailable, an atomic unlink is safe:
        # a concurrent writer either recreates the pathname with its ticket or
        # finishes on the unlinked inode and leaves the pathname absent.  Both
        # outcomes are conservative for the next reader.
        try:
            marker.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def record_activity(
    app_dir: pathlib.Path, provider: str, source_path: str,
    *, configuration_generation: "str | None" = None,
) -> bool:
    """Append one hook-owned activity ticket before background work starts.

    Every ticket carries the ledger generation it was written under and the
    next durable sequence in that ledger. Both are assigned here, under the
    writer flock, so two concurrent hooks can never mint the same sequence.

    The sidecar is published BEFORE the ticket, and the ordering is
    deliberate. A crash between the two leaves a sequence number that no
    ticket ever claims, which the next consumer reads as a gap and answers
    with an exhaustive walk. The opposite order would leave two tickets
    sharing one sequence, which is the same recovery at the cost of a
    duplicate the ledger can never explain.
    """
    if provider not in _PROVIDERS:
        return False
    marker = activity_marker_path(pathlib.Path(app_dir))
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        lock_path = marker.with_name(_MARKER_LOCK_NAME)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                rotate = marker.stat().st_size >= _MARKER_ROTATE_BYTES
                present = True
            except OSError:
                rotate = False
                present = False
            state = read_ledger_state(pathlib.Path(app_dir))
            if state is None:
                # No usable ledger state. Minting a fresh generation is what
                # stops a restarted counter from looking like a continuation
                # of the one every consumer already acknowledged.
                state = LedgerState(_mint_ledger_epoch(), 0)
                retained_generation = None
            else:
                retained_generation = state.configuration_generation
            epoch, sequence = state.epoch, state.sequence
            if rotate or not present:
                # Both are replacements of the pathname, and a replacement is
                # exactly what a consumer holding a byte cursor cannot follow.
                epoch = _mint_ledger_epoch()
            sequence += 1
            # A writer that supplies no configuration evidence must not erase
            # the evidence another writer left. One ledger serves both
            # providers, so a Claude ticket would otherwise wipe the only
            # durable record that the Codex hook ever ran.
            generation = configuration_generation or retained_generation
            _write_ledger_state(
                pathlib.Path(app_dir), epoch, sequence, generation)
            body = {
                "provider": provider,
                "path": str(source_path or ""),
                "epoch": epoch,
                "seq": sequence,
            }
            if configuration_generation:
                body["cfg"] = str(configuration_generation)
            payload = json.dumps(
                body,
                ensure_ascii=True, separators=(",", ":"), sort_keys=True,
            ).encode("utf-8") + b"\n"
            if rotate:
                replacement = marker.with_name(marker.name + ".next")
                fd = os.open(
                    replacement, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    _write_all(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(replacement, marker)
            else:
                fd = os.open(
                    marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    _write_all(fd, payload)
                finally:
                    os.close(fd)
            try:
                os.chmod(marker, 0o600)
                os.chmod(lock_path, 0o600)
                os.chmod(ledger_state_path(pathlib.Path(app_dir)), 0o600)
            except OSError:
                pass
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        return True
    except OSError:
        return False


def _write_all(fd: int, payload: bytes) -> None:
    """Write one complete marker record or fail closed.

    Regular-file ``os.write`` calls may legally report a short count. The
    hook's marker lock serializes the loop, so completing the remaining bytes
    preserves one newline-delimited ticket without weakening append ordering.
    """
    pending = memoryview(payload)
    while pending:
        try:
            written = os.write(fd, pending)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("activity marker write made no progress")
        pending = pending[written:]


@dataclass(frozen=True)
class FrontierPlan:
    provider: str
    mode: str
    paths: frozenset[str] = frozenset()
    marker_end: int = 0
    reason: str = "none"
    # The ledger generation this plan consumed and the last sequence in the
    # slice it consumed. Committing a non-full plan advances the consumer's
    # acknowledgement to exactly these, so the NEXT plan can prove it saw
    # every ticket in between.
    ledger_epoch: "str | None" = None
    ledger_sequence: int = 0


@dataclass(frozen=True)
class MarkerCutoff:
    identity: tuple[int, int]
    end: int
    # The ledger generation and sequence as at this boundary, read under the
    # SAME writer lock that produced `end`. Reading them afterwards would race
    # a ticket into the gap: the walk would leave that ticket pending on the
    # byte cursor while the consumer had already acknowledged its sequence,
    # and the next plan would report a duplicate that never happened.
    ledger_epoch: "str | None" = None
    ledger_sequence: int = 0


@dataclass(frozen=True)
class _MarkerCutoffFailure:
    reason: str = "capture_failed"


_MARKER_CUTOFF_FAILURE = _MarkerCutoffFailure()


@dataclass
class _ProviderState:
    marker_identity: tuple[int, int]
    marker_offset: int
    db_identity: tuple[int, int]
    schema_identity: tuple[int, int]
    pending_identity: tuple[str, ...]
    directory_identity: dict[str, tuple[int, int, int, int]]
    guard_identity: dict[str, tuple[int, int, int, int]]
    # Monotonic timestamp of the exhaustive walk this certificate rests on.
    # The default is deliberately far in the past so a state built without one
    # is already expired rather than trusted forever.
    seeded_at: float = 0.0
    # The ledger generation and the last sequence this consumer acknowledged.
    # `None` means no generation has ever been acknowledged, which no live
    # generation matches, so an unstamped state recovers rather than trusting.
    ack_epoch: "str | None" = None
    ack_sequence: int = 0
    # The shared provider evidence generation this certificate rests on, and
    # the normalized configured root set it was minted over.
    evidence_generation: "str | None" = None
    root_membership: tuple[str, ...] = ()


def _frontier_memory_stats(owner):
    estimated = retained_size_bytes(
        owner._states, stop_after=FRONTIER_MAX_BYTES)
    return {
        "estimatedBytes": estimated,
        "maxBytes": FRONTIER_MAX_BYTES,
        "entryCount": len(owner._states),
        "maxEntries": len(_PROVIDERS),
        "fallbackCount": int(owner._fallback_count),
    }


def _admit_frontier_state(owner, provider: str, state: _ProviderState) -> bool:
    candidate = dict(owner._states)
    candidate[provider] = state
    if retained_size_bytes(candidate, stop_after=FRONTIER_MAX_BYTES) > (
            FRONTIER_MAX_BYTES):
        owner._states.pop(provider, None)
        owner._fallback_count += 1
        owner.last_seed_failure[provider] = "memory_budget"
        return False
    owner._states[provider] = state
    return True


def _issue_generation_for(owner, provider: str, conn) -> "str | None":
    """Join or mint this provider's evidence generation, or fail closed.

    Returns ``None`` when this consumer's store contradicts another
    consumer's about which exhaustive walk it rests on. That is the
    cross-restart disagreement: the in-process generation is gone, the two
    stores' stamps are all that remain, and two different non-empty answers
    cannot both be true.

    THE REFUSAL IS ONE-SHOT PER CONSUMER PER PROCESS, and it is not "self-
    correcting on the next walk" — commit ``2143bdfb8`` quoted that earlier
    wording as untrue of any walk, and this caller kept it. The refusal
    returns ``None``, so this seed FAILS and stamps nothing; the correction
    comes from the SECOND seed, which ``observe_stamp`` no longer refuses and
    which joins the live generation and writes it into this store. See
    ``FrontierGenerations.observe_stamp`` for why refusing more than once was
    an outage rather than a safeguard.
    """
    stamped = _read_generation_stamp(conn, provider)
    if not owner._generations.observe_stamp(
        provider, owner.CONSUMER, stamped
    ):
        return None
    generation = owner._generations.issue(provider, owner.CONSUMER)
    _write_generation_stamp(conn, provider, generation)
    return generation


class FrontierGenerations:
    """One provider evidence generation, shared by every registered consumer.

    #769 S6, #716 Task A. The two frontiers each held their own certificate
    state and their own byte cursor into one shared ticket ledger, and nothing
    tied them together: both could report themselves caught up while resting
    on different bodies of evidence, and neither could say whether the other
    had consumed the walk it was trusting.

    A generation is minted by the first exhaustive walk that has no live
    generation to join. A later walk by another consumer JOINS it rather than
    replacing it, which is what makes "both consumers are on one generation" a
    statement with content. It RETIRES once every registered consumer has
    acknowledged it, and only then does the next walk mint a new one.

    REGISTRATION IS BY PARTICIPATION, and that is the explicit form rather
    than an assumed one. A consumer registers the first time it actually seeds
    or plans a provider, so a process that never runs a transcript pass — the
    standalone TUI builds only an accounting frontier — has no conversation
    consumer to wait for and its generations retire normally. Registering at
    CONSTRUCTION would be wrong for a specific reason:
    `ConversationSyncFrontier.capture_cutoff` constructs a throwaway
    `DashboardIngestFrontier` purely to reuse one method, and that object
    would otherwise register an accounting consumer that never acknowledges
    anything and hold every generation open forever.

    Every method takes the lock. Two dashboard threads reach one of these.
    """

    def __init__(self, app_dir):
        self.app_dir = pathlib.Path(app_dir)
        self._lock = threading.RLock()
        self._registered: dict[str, set[str]] = {}
        self._live: dict[str, str] = {}
        self._acknowledged: dict[str, dict[str, str]] = {}
        self._observed: dict[str, dict[str, "str | None"]] = {}
        # The `(epoch, sequence)` each consumer last acknowledged into the
        # ticket ledger. Separate from `_acknowledged`, which tracks the
        # EVIDENCE generation: two consumers may legitimately sit at different
        # sequences inside one ledger generation, and the reconciliation below
        # judges only the generation.
        self._ledger_acks: dict[str, dict[str, tuple]] = {}

    def _register_locked(self, provider: str, consumer: str) -> None:
        """Registration is by PARTICIPATION — see this class's docstring.

        Private, and called from every method a consumer reaches, because
        there is no separate moment at which a consumer announces itself: the
        first plan, seed or acknowledgement IS the announcement, and a public
        `register` nobody called was the alternative this replaced.
        """
        self._registered.setdefault(provider, set()).add(consumer)

    def record_ledger_acknowledgement(
        self, provider: str, consumer: str, epoch, sequence: int,
    ) -> None:
        """Publish where this consumer's byte cursor stands in the ledger."""
        if not epoch:
            return
        with self._lock:
            self._register_locked(provider, consumer)
            self._ledger_acks.setdefault(provider, {})[consumer] = (
                str(epoch), int(sequence))

    def peer_ledger_acknowledgements(self, provider: str, consumer: str):
        """Every OTHER consumer's ledger acknowledgement, as a tuple."""
        with self._lock:
            return tuple(
                value for other, value in
                self._ledger_acks.get(provider, {}).items()
                if other != consumer
            )

    def observe_stamp(self, provider: str, consumer: str, stamp) -> bool:
        """Record what this consumer's store claimed when first examined.

        Returns False ONCE per consumer when that claim contradicts another
        consumer's. This is the cross-restart check: the in-process generation
        is gone, so the two stores' own stamps are the only remaining
        statement about which exhaustive walk each of them rests on, and two
        different non-empty answers cannot both be true.

        REGISTERS THE CONSUMER, like every other method a consumer reaches.
        Without that, a refused consumer is not registered, so its peer can
        commit a clean plan, become the only registered consumer, retire the
        generation on its own, and leave the second seed minting a FRESH
        generation instead of joining the live one — which stamps this store
        with a third value and leaves the two stores still disagreeing. The
        cost of registering is the one the participation deviation in the
        session spec already records: a consumer that refuses and then never
        seeds again holds the live generation open for the life of the
        process. Retirement is NOT purely diagnostic — ``_retired_locked`` is
        what ``issue`` consults to choose between minting a fresh generation
        and rejoining the live one, while the public ``generation_retired``
        accessor is read only by tests. A generation held open therefore means
        later walks rejoin it and stamp both stores with the SAME value, which
        is agreement rather than divergence, and this path writes nothing to
        ``_ledger_acks``, so it cannot reach the seventh discontinuity. The
        repair it buys is what makes the two stores converge, so the trade is
        deliberate.

        FIRST OBSERVATION WINS, and it is never overwritten. What this dict
        holds is each consumer's PRE-PROCESS claim — what its store said
        before this process touched it — and NOT a live mirror of disk. The
        distinction is load-bearing rather than pedantic: this process writes
        a generation into the store of every consumer that seeds
        successfully, so refreshing the record afterwards would let the value
        THIS process just wrote contradict a peer that had agreed with the
        estate all along, and force a spurious exhaustive walk. With more than
        two consumers the retained pre-process claim can outlive the
        disagreement it described, and a consumer whose first observation
        lands after another consumer's refusal was lifted may then refuse once
        against it. The one-shot key is ``(provider, consumer)``, so that
        bound is ONE extra exhaustive walk per consumer and the worst case
        over N consumers is N of them, not one. It is the direction to be
        wrong in, and production has exactly two consumers.

        ONE-SHOT, and that bound is the whole difference between a safe check
        and an outage. Two stores holding different stamps is not a rare
        corruption: a generation retires once every registered consumer
        acknowledges it, the next exhaustive walk mints a fresh one, and only
        the store of the consumer that walked is stamped with it — so two
        consumers whose walks land in different rounds leave two different
        stamps behind as an ordinary outcome. Measured on the sealed #786
        current corpus after one ordinary dashboard run, both providers'
        stamps differed. A refusal that never lifted therefore held the
        refusing consumer on a full plan on EVERY tick, and #769 S6's own
        measurement caught the estate walking all 3,012 retained rollouts once
        every five seconds. Refusing once costs one exhaustive walk; that walk
        stamps this consumer's store, which is what makes the two agree again
        and what the recorded reasoning always claimed happened.

        An absent stamp is not a claim. Every store predating this protocol is
        in that state, and so is a fresh install.
        """
        with self._lock:
            self._register_locked(provider, consumer)
            seen = self._observed.setdefault(provider, {})
            first_observation = consumer not in seen
            contradicted = stamp is not None and any(
                other != consumer and value is not None and value != stamp
                for other, value in seen.items()
            )
            seen.setdefault(consumer, stamp)
            return not (contradicted and first_observation)

    def issue(self, provider: str, consumer: str) -> str:
        """The generation this consumer's exhaustive walk belongs to."""
        with self._lock:
            self._register_locked(provider, consumer)
            live = self._live.get(provider)
            if live is None or self._retired_locked(provider, live):
                live = secrets.token_hex(16)
                self._live[provider] = live
                self._acknowledged[provider] = {}
            return live

    def acknowledge(self, provider: str, consumer: str, generation) -> None:
        if not generation:
            return
        with self._lock:
            self._register_locked(provider, consumer)
            self._acknowledged.setdefault(provider, {})[consumer] = generation

    def drop(self, provider: str, consumer: str) -> None:
        """Deregister one consumer, e.g. after `drop_provider` discards it.

        A consumer that has abandoned its certificate can no longer
        acknowledge anything, so leaving it registered would hold this
        generation and every later one open for the life of the process.
        """
        with self._lock:
            self._registered.get(provider, set()).discard(consumer)
            self._acknowledged.get(provider, {}).pop(consumer, None)
            # The ledger acknowledgement goes with it. A consumer that has
            # abandoned its certificate is no longer describing any ledger
            # position, so reconciling a live consumer against the cursor it
            # left behind would force exhaustive recovery over a claim nobody
            # is making any more.
            self._ledger_acks.get(provider, {}).pop(consumer, None)

    def retired(self, provider: str, generation) -> bool:
        with self._lock:
            return self._retired_locked(provider, generation)

    def _retired_locked(self, provider: str, generation) -> bool:
        if not generation:
            return True
        acknowledged = self._acknowledged.get(provider, {})
        registered = self._registered.get(provider, set())
        if not registered:
            return True
        return all(
            acknowledged.get(consumer) == generation
            for consumer in registered
        )


#: One coordinator per data directory, because the generation is a property of
#: the estate rather than of any object that happens to be looking at it. Two
#: frontiers in one process must reach the same one, and they are constructed
#: independently in `_cctally_dashboard` and `_cctally_tui`, so the shared
#: object cannot be passed between them without changing both callers.
_FRONTIER_GENERATIONS: "dict[str, FrontierGenerations]" = {}
_FRONTIER_GENERATIONS_LOCK = threading.Lock()


def frontier_generations(app_dir) -> FrontierGenerations:
    key = str(pathlib.Path(app_dir))
    with _FRONTIER_GENERATIONS_LOCK:
        existing = _FRONTIER_GENERATIONS.get(key)
        if existing is None:
            existing = FrontierGenerations(app_dir)
            _FRONTIER_GENERATIONS[key] = existing
        return existing


# NOTE (#769 S6 T1 review R9): there is deliberately NO
# `reset_frontier_generations`. One shipped in T1 as a production test hook and
# was removed here, because #769 S6's constraints forbid adding one and the
# `reset_*` family cited to justify it was created by this same session: as of
# T1 the only commit introducing that sibling helper was 6d479343a, inside T1
# itself. (Search for the definition rather than quoting the literal search
# string, which would make this comment a second hit and falsify the claim.)
# `reset_frontier_counters` stays because `bin/cctally-bench` calls it;
# this one had no caller outside `tests/`. Isolation is now the test estate's
# own job: `tests/conftest.py` substitutes `_FRONTIER_GENERATIONS` for the
# duration of every test through `monkeypatch`, which restores the real
# registry afterwards and is the mechanism `tests/_pytest_isolation_plugin.py`
# already reasons about for a module global.


def frontier_generation_meta_key(provider: str) -> str:
    """The `cache_meta` key each store stamps its acknowledged generation in."""
    return f"dashboard_frontier_generation_{provider}"


def _read_generation_stamp(conn, provider: str) -> "str | None":
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=? LIMIT 1",
            (frontier_generation_meta_key(provider),),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def _write_generation_stamp(conn, provider: str, generation: str) -> None:
    """Best effort. A store that cannot be stamped simply carries no claim.

    Refusing the seed instead would turn a read-only or contended connection
    into a permanent full-walk loop, which is a worse outcome than losing one
    cross-restart consistency check.
    """
    try:
        conn.execute(
            "INSERT INTO cache_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (frontier_generation_meta_key(provider), generation),
        )
        conn.commit()
    except sqlite3.DatabaseError:
        pass


def normalized_root_membership(roots) -> tuple[str, ...]:
    """The configured root SET, as certificate identity.

    Sorted and de-duplicated, so reordering `$CODEX_HOME` is not a change
    while adding or removing an entry is. Without this a configured root could
    be added or removed with no filesystem change anywhere and an old
    certificate would still look caught up, because plan-time roots were used
    only to validate ticket targets.
    """
    return tuple(sorted({
        str(pathlib.Path(root).absolute()) for root in roots
    }))


def _stat_identity(path: pathlib.Path) -> tuple[int, int, int, int]:
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)


def _stat_identity_or_missing(path: pathlib.Path) -> tuple[int, int, int, int]:
    """Represent an already-absent directory as a stable observed state."""
    try:
        return _stat_identity(path)
    except OSError:
        return (0, 0, 0, 0)


def _database_path(conn) -> pathlib.Path:
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main" and path:
            return pathlib.Path(path)
    raise OSError("database path unavailable")


def _database_identity(conn):
    st = _database_path(conn).stat()
    return st.st_dev, st.st_ino


def _schema_identity(conn) -> tuple[int, int]:
    schema = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    user = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return schema, user


def _pending_identity(conn) -> tuple[str, ...]:
    placeholders = ",".join("?" for _ in _PENDING_META_KEYS)
    rows = conn.execute(
        f"SELECT key FROM cache_meta WHERE key IN ({placeholders}) ORDER BY key",
        _PENDING_META_KEYS,
    )
    return tuple(str(row[0]) for row in rows)


def _provider_complete(conn, provider: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM cache_meta WHERE key=? LIMIT 1",
        (_COMPLETE_KEYS[provider],),
    ).fetchone()
    return row is not None


def _source_paths(conn, provider: str) -> tuple[pathlib.Path, ...]:
    table = _ingest_source_table(provider)
    return tuple(
        pathlib.Path(str(row[0]))
        for row in conn.execute(f"SELECT path FROM {table}")
        if row[0] and os.path.isabs(str(row[0]))
    )


def _directory_paths(conn, provider: str, roots) -> tuple[pathlib.Path, ...]:
    normalized_roots = tuple(pathlib.Path(root).absolute() for root in roots)
    directories = set(normalized_roots)
    for source in _source_paths(conn, provider):
        parent = source.parent
        for root in normalized_roots:
            try:
                parent.relative_to(root)
            except ValueError:
                continue
            cur = parent
            while True:
                directories.add(cur)
                if cur == root:
                    break
                cur = cur.parent
            break
    return tuple(sorted(directories, key=str))


def _directory_identity(conn, provider: str, roots):
    return {
        str(path): _stat_identity_or_missing(path)
        for path in _directory_paths(conn, provider, roots)
    }


def _restat_directory_identity(saved):
    return {
        raw: _stat_identity_or_missing(pathlib.Path(raw))
        for raw in saved
    }


def _guard_identity(paths):
    result = {}
    for raw in paths:
        path = pathlib.Path(raw)
        try:
            result[str(path)] = _stat_identity(path)
        except OSError:
            result[str(path)] = (0, 0, 0, 0)
    return result


def _read_marker(marker: pathlib.Path, offset: int):
    with marker.open("rb") as fh:
        st = os.fstat(fh.fileno())
        identity = (st.st_dev, st.st_ino)
        if st.st_size < offset:
            # The consumer's byte cursor points past the end of the ledger, so
            # its memory describes a file this one is not.
            raise ValueError("ledger_cursor_beyond_tail")
        fh.seek(offset)
        raw = fh.read()
        end = fh.tell()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("partial")
    records = _decode_marker_records(raw)
    return identity, end, records


def _decode_marker_records(raw: bytes):
    """Decode a ledger slice into ``(provider, path, epoch, sequence, cfg)``.

    A record written before the continuity protocol carries neither an epoch
    nor a sequence, and it decodes to ``(provider, path, None, None)`` rather
    than raising. That distinction matters: an upgraded install still holds
    pre-protocol tickets ahead of its cursor, and treating them as malformed
    would refuse to seed a certificate at all instead of forcing one recovery
    walk that then consumes them.
    """
    records = []
    for line in raw.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("malformed") from None
        if not isinstance(value, dict) or value.get("provider") not in _PROVIDERS:
            raise ValueError("malformed")
        path = value.get("path")
        if not isinstance(path, str):
            raise ValueError("malformed")
        epoch = value.get("epoch")
        sequence = value.get("seq")
        if epoch is not None and not isinstance(epoch, str):
            raise ValueError("malformed")
        if sequence is not None and (
            not isinstance(sequence, int) or isinstance(sequence, bool)
        ):
            raise ValueError("malformed")
        if (epoch is None) != (sequence is None):
            # Half a stamp is not a legacy record; it is a record whose
            # continuity claim cannot be evaluated at all.
            raise ValueError("malformed")
        generation = value.get("cfg")
        if generation is not None and not isinstance(generation, str):
            raise ValueError("malformed")
        records.append((value["provider"], path, epoch, sequence, generation))
    return records


def _validate_marker_prefix(marker: pathlib.Path, cutoff: "MarkerCutoff") -> None:
    """Validate all evidence that a successful full walk proposes to consume."""
    with marker.open("rb") as fh:
        st = os.fstat(fh.fileno())
        if (st.st_dev, st.st_ino) != cutoff.identity or st.st_size < cutoff.end:
            raise ValueError("activity_marker_changed")
        raw = fh.read(cutoff.end)
    if len(raw) != cutoff.end:
        raise ValueError("activity_marker_changed")
    if raw and not raw.endswith(b"\n"):
        raise ValueError("partial")
    _decode_marker_records(raw)


def _target_has_cursor_gap(conn, provider: str, source_path: str) -> bool:
    table = "session_files" if provider == "claude" else "codex_session_files"
    row = conn.execute(
        f"SELECT size_bytes, last_byte_offset FROM {table} WHERE path=?",
        (source_path,),
    ).fetchone()
    if row is None:
        return False  # a genuinely new file is a valid targeted append
    try:
        actual_size = pathlib.Path(source_path).stat().st_size
    except OSError:
        return True
    return actual_size < max(int(row[0] or 0), int(row[1] or 0))


def _target_in_roots(source_path: str, roots) -> bool:
    path = pathlib.Path(source_path)
    if not path.is_absolute() or path.suffix != ".jsonl":
        return False
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    if not resolved.is_file():
        return False
    for root in roots:
        try:
            resolved.relative_to(pathlib.Path(root).resolve(strict=True))
        except (OSError, ValueError):
            continue
        return True
    return False


def is_dashboard_activity_claude_hook_handler(handler: object) -> bool:
    """Require the exact executable handler shape that writes tickets."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return bool(
        len(tokens) == 2
        and pathlib.Path(tokens[0]).is_absolute()
        and pathlib.Path(tokens[0]).name in {"cctally", "cctally-npm-shim.js"}
        and tokens[1] == "hook-tick"
    )


def provider_sync_certifiable(mode: str, stats: object) -> bool:
    """Whether one provider result can advance or mint a certificate."""
    if mode == "caught_up":
        return True
    if stats is None:
        return False
    common_clean = not any((
        getattr(stats, "lock_contended", False),
        getattr(stats, "files_failed", 0),
        getattr(stats, "files_deferred_torn", 0),
        getattr(stats, "deferred_reason", None),
        getattr(stats, "prune_refused", False),
        getattr(stats, "budget_exhausted", False),
        getattr(stats, "maintenance_failed", False),
    ))
    if not common_clean:
        return False
    if mode == "targeted":
        return bool(getattr(stats, "targeted_clean", True))
    if mode == "full":
        return bool(getattr(stats, "full_walk_complete", False))
    return False


class DashboardIngestFrontier:
    """One process-local certificate per provider, rebuilt after full sync."""

    #: This frontier's name in the shared generation coordinator.
    CONSUMER = "accounting"

    def __init__(self, app_dir: pathlib.Path):
        self.app_dir = pathlib.Path(app_dir)
        self._states: dict[str, _ProviderState] = {}
        self.last_seed_failure: dict[str, str] = {}
        self._fallback_count = 0
        self._generations = frontier_generations(self.app_dir)

    def _issue_generation(self, provider: str, conn) -> "str | None":
        return _issue_generation_for(self, provider, conn)

    def evidence_generation(self, provider: str) -> "str | None":
        state = self._states.get(provider)
        return None if state is None else state.evidence_generation

    def generation_retired(self, provider: str, generation) -> bool:
        return self._generations.retired(provider, generation)

    def memory_stats(self):
        return _frontier_memory_stats(self)

    def capture_cutoff(self) -> "MarkerCutoff | _MarkerCutoffFailure":
        """Capture a writer-serialized boundary before a full walk starts.

        Materializing an absent marker under the same flock used by hook
        writers gives the first full walk a real zero-byte cutoff. A ticket
        appended after this method returns therefore remains pending instead
        of being swallowed by finalization.
        """
        marker = activity_marker_path(self.app_dir)
        lock_path = marker.with_name(_MARKER_LOCK_NAME)
        lock_fd = None
        marker_fd = None
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            marker_fd = os.open(marker, os.O_RDONLY | os.O_CREAT, 0o600)
            st = os.fstat(marker_fd)
            # Materialize the ledger generation here for the same reason the
            # marker itself is materialized here: the first full walk needs a
            # real zero-sequence boundary to acknowledge. Without one the
            # consumer would acknowledge "no ledger", the first hook ticket
            # would mint a generation, and that first ticket would present as
            # a replacement rather than as the append it is.
            ledger = read_ledger_state(self.app_dir)
            if ledger is None:
                # A `LedgerState`, not a bare pair. Every other reader of this
                # value reaches it by attribute, and a 2-tuple standing in for
                # a 3-field record is exactly the kind of substitution that
                # works until somebody reads the third field.
                ledger = LedgerState(_mint_ledger_epoch(), 0)
                _write_ledger_state(self.app_dir, ledger.epoch, ledger.sequence)
        except OSError:
            return _MARKER_CUTOFF_FAILURE
        finally:
            if marker_fd is not None:
                os.close(marker_fd)
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        return MarkerCutoff(
            (st.st_dev, st.st_ino), st.st_size,
            ledger.epoch, ledger.sequence)

    def seed_provider(
        self, provider: str, conn, *, roots, guard_paths=(), trusted=True,
        cutoff: "MarkerCutoff | _MarkerCutoffFailure | None" = None,
    ) -> bool:
        if provider not in _PROVIDERS:
            raise ValueError("unknown provider")
        if not trusted:
            self.last_seed_failure[provider] = "untrusted_hook_configuration"
            self._states.pop(provider, None)
            return False
        marker = activity_marker_path(self.app_dir)
        try:
            boundary = cutoff if cutoff is not None else self.capture_cutoff()
            if not isinstance(boundary, MarkerCutoff):
                self.last_seed_failure[provider] = "cutoff_capture_failed"
                self._states.pop(provider, None)
                return False
            st = marker.stat()
            marker_identity = (st.st_dev, st.st_ino)
            if marker_identity != boundary.identity or st.st_size < boundary.end:
                self.last_seed_failure[provider] = "activity_marker_changed"
                self._states.pop(provider, None)
                return False
            _validate_marker_prefix(marker, boundary)
            pending_identity = _pending_identity(conn)
            if pending_identity:
                self.last_seed_failure[provider] = "maintenance_pending"
                self._states.pop(provider, None)
                return False
            if not _provider_complete(conn, provider):
                self.last_seed_failure[provider] = "incomplete_store"
                self._states.pop(provider, None)
                return False
            generation = self._issue_generation(provider, conn)
            if generation is None:
                self.last_seed_failure[provider] = "generation_disagreement"
                self._states.pop(provider, None)
                return False
            candidate = _ProviderState(
                marker_identity=marker_identity,
                marker_offset=boundary.end,
                db_identity=_database_identity(conn),
                schema_identity=_schema_identity(conn),
                pending_identity=pending_identity,
                directory_identity=_directory_identity(conn, provider, roots),
                guard_identity=_guard_identity(guard_paths),
                seeded_at=_now(),
                # The exhaustive walk consumed everything up to the cutoff, so
                # the ledger position AT the cutoff is what it acknowledges.
                ack_epoch=boundary.ledger_epoch,
                ack_sequence=boundary.ledger_sequence,
                evidence_generation=generation,
                root_membership=normalized_root_membership(roots),
            )
            if not _admit_frontier_state(self, provider, candidate):
                return False
            # Published so the OTHER consumer can reconcile against it. A
            # successful exhaustive walk consumed everything up to the cutoff,
            # so the cutoff's ledger position IS this consumer's new
            # acknowledgement, and recording it only at commit time would
            # leave a freshly reseeded consumer invisible to its peer.
            self._generations.record_ledger_acknowledgement(
                provider, self.CONSUMER,
                boundary.ledger_epoch, boundary.ledger_sequence)
            self.last_seed_failure[provider] = ""
            return True
        except (OSError, ValueError) as exc:
            self.last_seed_failure[provider] = f"{type(exc).__name__}:{exc}"
            self._states.pop(provider, None)
            return False

    def plan_provider(
        self, provider: str, conn, *, roots, guard_paths=(),
        configuration_generation=_DERIVE_CONFIGURATION_GENERATION,
    ) -> FrontierPlan:
        return _counted_plan(self, _plan_accounting, provider, conn,
                             roots=roots, guard_paths=guard_paths,
                             configuration_generation=configuration_generation)

    def _plan_uncounted(
        self, provider: str, conn, *, roots, guard_paths=(),
        configuration_generation=_DERIVE_CONFIGURATION_GENERATION,
    ) -> FrontierPlan:
        state = self._states.get(provider)
        if state is None:
            return FrontierPlan(provider, "full", reason="unseeded")
        # Checked before every other guard: an expired certificate needs a full
        # walk whatever the remaining evidence says, and answering here keeps
        # the expiry itself free of database and filesystem work.
        if _now() - state.seeded_at >= FRONTIER_CERTIFICATE_MAX_AGE_SECONDS:
            return FrontierPlan(provider, "full", reason="certificate_expired")
        marker = activity_marker_path(self.app_dir)
        try:
            if _database_identity(conn) != state.db_identity:
                raise ValueError("database_replaced")
            if _schema_identity(conn) != state.schema_identity:
                raise ValueError("schema_changed")
            if not _provider_complete(conn, provider):
                raise ValueError("incomplete_store")
            if _pending_identity(conn) != state.pending_identity:
                raise ValueError("maintenance_changed")
            if _guard_identity(guard_paths) != state.guard_identity:
                raise ValueError("hook_config_changed")
            # The configured root SET, before any filesystem comparison. A
            # root added or removed with no filesystem change anywhere moves
            # none of the directory identities below, because those describe
            # only the roots the certificate was minted over.
            if normalized_root_membership(roots) != state.root_membership:
                raise ValueError("root_membership_changed")
            if _restat_directory_identity(
                state.directory_identity
            ) != state.directory_identity:
                raise ValueError("filesystem_changed")
            marker_identity, marker_end, records = _read_marker(
                marker, state.marker_offset)
            if marker_identity != state.marker_identity:
                raise ValueError("marker_replaced")
        except (OSError, ValueError) as exc:
            reason = str(exc) if str(exc) else "ambiguous"
            return FrontierPlan(provider, "full", reason=reason)
        reason, ledger_epoch, ledger_sequence = _ledger_slice_decision(
            self, provider, state, records)
        if reason is not None:
            return FrontierPlan(provider, "full", reason=reason)
        reason = _configuration_generation_reason(
            self, provider, records, guard_paths, configuration_generation)
        if reason is not None:
            return FrontierPlan(
                provider, "full", marker_end=marker_end, reason=reason)
        paths, reason = _ticket_target_paths(provider, records, roots)
        if reason is not None:
            return FrontierPlan(
                provider, "full", marker_end=marker_end, reason=reason)
        if any(_target_has_cursor_gap(conn, provider, path) for path in paths):
            return FrontierPlan(provider, "full", reason="cursor_gap")
        return _restat_and_return(
            provider, conn, cursor_table=_ingest_source_table(provider),
            paths=paths, marker_end=marker_end, ledger_epoch=ledger_epoch,
            ledger_sequence=ledger_sequence,
        )

    def commit_provider(
        self, plan: FrontierPlan, conn, *, roots, guard_paths=(), trusted=True,
        cutoff: "MarkerCutoff | _MarkerCutoffFailure | None" = None,
    ) -> None:
        """Advance a clean caught-up/targeted plan; full plans are reseeded."""
        if plan.mode == "full":
            self.seed_provider(
                plan.provider, conn, roots=roots, guard_paths=guard_paths,
                trusted=trusted, cutoff=cutoff,
            )
            return
        state = self._states.get(plan.provider)
        if state is None:
            return
        # The acknowledgement moves with the BYTE cursor, not with this
        # provider's own tickets: one ledger serves both providers, so a
        # Claude ticket the Codex consumer skipped over is still a sequence
        # the Codex consumer has now passed. Leaving it unacknowledged would
        # make the next plan read a hole that is not a hole.
        if plan.ledger_epoch is not None:
            state.ack_epoch = plan.ledger_epoch
            state.ack_sequence = plan.ledger_sequence
            self._generations.record_ledger_acknowledgement(
                plan.provider, self.CONSUMER,
                plan.ledger_epoch, plan.ledger_sequence)
        # Committing a clean plan is this consumer's acknowledgement that it
        # has consumed the evidence generation its certificate rests on.
        self._generations.acknowledge(
            plan.provider, self.CONSUMER, state.evidence_generation)
        if plan.mode == "caught_up":
            # The plan already compared every guard. Advancing an empty marker
            # slice must remain O(1); recomputing the estate here would erase
            # the fast-negative immediately after taking it.
            state.marker_offset = plan.marker_end
            return
        # Targeted ingest may have changed schema-neutral cache guard values;
        # refresh every cheap guard from the committed connection.
        state.marker_offset = plan.marker_end
        state.db_identity = _database_identity(conn)
        state.schema_identity = _schema_identity(conn)
        state.pending_identity = _pending_identity(conn)
        state.directory_identity = _directory_identity(
            conn, plan.provider, roots)
        state.guard_identity = _guard_identity(guard_paths)
        _admit_frontier_state(self, plan.provider, state)


def _conversation_pending_identity(conn) -> tuple[str, ...]:
    placeholders = ",".join("?" for _ in _CONVERSATION_PENDING_META_KEYS)
    rows = conn.execute(
        f"SELECT key FROM cache_meta WHERE key IN ({placeholders}) ORDER BY key",
        _CONVERSATION_PENDING_META_KEYS,
    )
    return tuple(str(row[0]) for row in rows)


def _conversation_source_table(provider: str) -> str:
    if provider == "claude":
        return "conversation_source_files"
    if provider == "codex":
        return "codex_conversation_source_files"
    raise ValueError("unknown provider")


def _conversation_source_paths(conn, provider: str) -> tuple[pathlib.Path, ...]:
    table = _conversation_source_table(provider)
    return tuple(
        pathlib.Path(str(row[0]))
        for row in conn.execute(f"SELECT path FROM {table}")
        if row[0] and os.path.isabs(str(row[0]))
    )


def _conversation_directory_paths(conn, provider: str, roots):
    normalized_roots = tuple(pathlib.Path(root).absolute() for root in roots)
    directories = set(normalized_roots)
    for source in _conversation_source_paths(conn, provider):
        parent = source.parent
        for root in normalized_roots:
            try:
                parent.relative_to(root)
            except ValueError:
                continue
            cur = parent
            while True:
                directories.add(cur)
                if cur == root:
                    break
                cur = cur.parent
            break
    return tuple(sorted(directories, key=str))


def _conversation_directory_identity(conn, provider: str, roots):
    return {
        str(path): _stat_identity_or_missing(path)
        for path in _conversation_directory_paths(conn, provider, roots)
    }


def _conversation_target_risk(conn, provider: str, source_path: str) -> str | None:
    """Classify a ticketed path that cannot use ordinary append ingest."""
    table = _conversation_source_table(provider)
    row = conn.execute(
        f"SELECT size_bytes,mtime_ns,last_byte_offset FROM {table} WHERE path=?",
        (source_path,),
    ).fetchone()
    if row is None:
        return None
    try:
        stat = pathlib.Path(source_path).stat()
    except OSError:
        return "cursor_gap"
    stored_size = int(row[0] or 0)
    if stat.st_size < max(stored_size, int(row[2] or 0)):
        return "cursor_gap"
    if stat.st_size == stored_size and stat.st_mtime_ns != int(row[1] or 0):
        return "source_replaced"
    return None


def conversation_sync_certifiable(
    mode: str, stats: object, *, expected_paths: int = 0,
) -> bool:
    """Whether one transcript result may advance its process certificate."""
    if mode == "caught_up":
        return True
    if stats is None:
        return False
    if any((
        getattr(stats, "lock_contended", False),
        getattr(stats, "files_failed", 0),
        getattr(stats, "files_deferred_torn", 0),
        getattr(stats, "deferred_reason", None),
        getattr(stats, "prune_refused", False),
        getattr(stats, "budget_exhausted", False),
        getattr(stats, "maintenance_failed", False),
    )):
        return False
    total = int(getattr(stats, "files_total", 0) or 0)
    completed = (
        int(getattr(stats, "files_processed", 0) or 0)
        + int(getattr(stats, "files_skipped_unchanged", 0) or 0)
    )
    if mode == "targeted":
        # A source deleted after planning is filtered before the sync sees it;
        # do not consume that ticket as an empty successful target set.
        return total == expected_paths and completed == total
    if mode == "full":
        # There is no durable conversation-walk sentinel.  The per-call census
        # is therefore the proof that the exhaustive discovery actually drained.
        return completed == total
    return False


class ConversationSyncFrontier:
    """Independent hook-journal certificates for `conversations.db`.

    The activity file is shared evidence, not a consumable queue.  This class
    owns its byte cursor independently of :class:`DashboardIngestFrontier` and
    guards the transcript store's own schema/cursor tables.
    """

    #: This frontier's name in the shared generation coordinator.
    CONSUMER = "conversation"

    def __init__(self, app_dir: pathlib.Path):
        self.app_dir = pathlib.Path(app_dir)
        self._states: dict[str, _ProviderState] = {}
        self.last_seed_failure: dict[str, str] = {}
        self._fallback_count = 0
        self._generations = frontier_generations(self.app_dir)

    def _issue_generation(self, provider: str, conn) -> "str | None":
        return _issue_generation_for(self, provider, conn)

    def evidence_generation(self, provider: str) -> "str | None":
        state = self._states.get(provider)
        return None if state is None else state.evidence_generation

    def generation_retired(self, provider: str, generation) -> bool:
        return self._generations.retired(provider, generation)

    def memory_stats(self):
        return _frontier_memory_stats(self)

    def capture_cutoff(self) -> "MarkerCutoff | _MarkerCutoffFailure":
        # Same writer-serialized boundary as the accounting frontier; keeping
        # the implementation single-sourced prevents the two readers assigning
        # different meaning to the first ticket in a newly-created marker.
        return DashboardIngestFrontier(self.app_dir).capture_cutoff()

    def drop_provider(self, provider: str) -> bool:
        """Discard one provider's certificate, returning whether one existed.

        For work that mutates the transcript store OUTSIDE a sync pass, where
        no stats object reaches `provider_sync_certifiable` (#729). The orphan
        prune is that case: its deletions commit on their own, and when the
        rollup re-derive that should follow them is refused, a certificate
        minted before the prune would let the next pass skip exactly the work
        the refusal left undone.
        """
        if provider not in _PROVIDERS:
            raise ValueError("unknown provider")
        # Deregister before discarding the certificate. A consumer with no
        # certificate cannot acknowledge anything, so leaving it registered
        # would hold this generation and every later one open for the life of
        # the process — and the accounting frontier would then never see one
        # retire, however cleanly it committed.
        self._generations.drop(provider, self.CONSUMER)
        return self._states.pop(provider, None) is not None

    def seed_provider(
        self, provider: str, conn, *, roots, guard_paths=(), trusted=True,
        cutoff: "MarkerCutoff | _MarkerCutoffFailure | None" = None,
    ) -> bool:
        if provider not in _PROVIDERS:
            raise ValueError("unknown provider")
        if not trusted:
            self.last_seed_failure[provider] = "untrusted_hook_configuration"
            self._states.pop(provider, None)
            return False
        marker = activity_marker_path(self.app_dir)
        try:
            boundary = cutoff if cutoff is not None else self.capture_cutoff()
            if not isinstance(boundary, MarkerCutoff):
                raise ValueError("cutoff_capture_failed")
            st = marker.stat()
            marker_identity = (st.st_dev, st.st_ino)
            if marker_identity != boundary.identity or st.st_size < boundary.end:
                raise ValueError("activity_marker_changed")
            _validate_marker_prefix(marker, boundary)
            pending = _conversation_pending_identity(conn)
            if pending:
                raise ValueError("maintenance_pending")
            generation = self._issue_generation(provider, conn)
            if generation is None:
                # Reported verbatim rather than through the enclosing
                # `except`, whose handler prefixes the exception type: this is
                # a named refusal, not an unexpected error.
                self.last_seed_failure[provider] = "generation_disagreement"
                self._states.pop(provider, None)
                return False
            candidate = _ProviderState(
                marker_identity=marker_identity,
                marker_offset=boundary.end,
                db_identity=_database_identity(conn),
                schema_identity=_schema_identity(conn),
                pending_identity=pending,
                directory_identity=_conversation_directory_identity(
                    conn, provider, roots),
                guard_identity=_guard_identity(guard_paths),
                seeded_at=_now(),
                ack_epoch=boundary.ledger_epoch,
                ack_sequence=boundary.ledger_sequence,
                evidence_generation=generation,
                root_membership=normalized_root_membership(roots),
            )
            if not _admit_frontier_state(self, provider, candidate):
                return False
            # Published so the OTHER consumer can reconcile against it. A
            # successful exhaustive walk consumed everything up to the cutoff,
            # so the cutoff's ledger position IS this consumer's new
            # acknowledgement, and recording it only at commit time would
            # leave a freshly reseeded consumer invisible to its peer.
            self._generations.record_ledger_acknowledgement(
                provider, self.CONSUMER,
                boundary.ledger_epoch, boundary.ledger_sequence)
            self.last_seed_failure[provider] = ""
            return True
        except (OSError, ValueError) as exc:
            self.last_seed_failure[provider] = f"{type(exc).__name__}:{exc}"
            self._states.pop(provider, None)
            return False

    def plan_provider(
        self, provider: str, conn, *, roots, guard_paths=(),
        configuration_generation=_DERIVE_CONFIGURATION_GENERATION,
    ):
        return _counted_plan(self, _plan_conversation, provider, conn,
                             roots=roots, guard_paths=guard_paths,
                             configuration_generation=configuration_generation)

    def _plan_uncounted(
        self, provider: str, conn, *, roots, guard_paths=(),
        configuration_generation=_DERIVE_CONFIGURATION_GENERATION,
    ):
        state = self._states.get(provider)
        if state is None:
            return FrontierPlan(provider, "full", reason="unseeded")
        # Checked before every other guard: an expired certificate needs a full
        # walk whatever the remaining evidence says, and answering here keeps
        # the expiry itself free of database and filesystem work.
        if _now() - state.seeded_at >= FRONTIER_CERTIFICATE_MAX_AGE_SECONDS:
            return FrontierPlan(provider, "full", reason="certificate_expired")
        marker = activity_marker_path(self.app_dir)
        try:
            if _database_identity(conn) != state.db_identity:
                raise ValueError("database_replaced")
            if _schema_identity(conn) != state.schema_identity:
                raise ValueError("schema_changed")
            if _conversation_pending_identity(conn) != state.pending_identity:
                raise ValueError("maintenance_changed")
            if _guard_identity(guard_paths) != state.guard_identity:
                raise ValueError("hook_config_changed")
            # The configured root SET, before any filesystem comparison. A
            # root added or removed with no filesystem change anywhere moves
            # none of the directory identities below, because those describe
            # only the roots the certificate was minted over.
            if normalized_root_membership(roots) != state.root_membership:
                raise ValueError("root_membership_changed")
            if _restat_directory_identity(
                state.directory_identity
            ) != state.directory_identity:
                raise ValueError("filesystem_changed")
            marker_identity, marker_end, records = _read_marker(
                marker, state.marker_offset)
            if marker_identity != state.marker_identity:
                raise ValueError("marker_replaced")
        except (OSError, ValueError) as exc:
            return FrontierPlan(
                provider, "full", reason=str(exc) or "ambiguous")
        reason, ledger_epoch, ledger_sequence = _ledger_slice_decision(
            self, provider, state, records)
        if reason is not None:
            return FrontierPlan(provider, "full", reason=reason)
        reason = _configuration_generation_reason(
            self, provider, records, guard_paths, configuration_generation)
        if reason is not None:
            return FrontierPlan(
                provider, "full", marker_end=marker_end, reason=reason)
        paths, reason = _ticket_target_paths(provider, records, roots)
        if reason is not None:
            return FrontierPlan(
                provider, "full", marker_end=marker_end, reason=reason)
        target_risks = {
            _conversation_target_risk(conn, provider, path)
            for path in paths
        }
        if "source_replaced" in target_risks:
            return FrontierPlan(provider, "full", reason="source_replaced")
        if "cursor_gap" in target_risks:
            return FrontierPlan(provider, "full", reason="cursor_gap")
        return _restat_and_return(
            provider, conn, cursor_table=_conversation_source_table(provider),
            paths=paths, marker_end=marker_end, ledger_epoch=ledger_epoch,
            ledger_sequence=ledger_sequence,
        )

    def commit_provider(
        self, plan: FrontierPlan, conn, *, roots, guard_paths=(), trusted=True,
        cutoff: "MarkerCutoff | _MarkerCutoffFailure | None" = None,
    ) -> None:
        if plan.mode == "full":
            self.seed_provider(
                plan.provider, conn, roots=roots, guard_paths=guard_paths,
                trusted=trusted, cutoff=cutoff)
            return
        state = self._states.get(plan.provider)
        if state is None:
            return
        if plan.ledger_epoch is not None:
            state.ack_epoch = plan.ledger_epoch
            state.ack_sequence = plan.ledger_sequence
            self._generations.record_ledger_acknowledgement(
                plan.provider, self.CONSUMER,
                plan.ledger_epoch, plan.ledger_sequence)
        self._generations.acknowledge(
            plan.provider, self.CONSUMER, state.evidence_generation)
        state.marker_offset = plan.marker_end
        if plan.mode == "caught_up":
            return
        state.db_identity = _database_identity(conn)
        state.schema_identity = _schema_identity(conn)
        state.pending_identity = _conversation_pending_identity(conn)
        state.directory_identity = _conversation_directory_identity(
            conn, plan.provider, roots)
        state.guard_identity = _guard_identity(guard_paths)
        _admit_frontier_state(self, plan.provider, state)
