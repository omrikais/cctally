"""Durable selector state — the pure kernel (#496 S5b Stage 1, spec §3).

`_lib_journal.resolve_effective_events` accumulates six things over the record
stream — per-evt candidates, batch markers, batch actions, protocol resolutions,
tainted batches with their distinct violations, and a per-fingerprint
`violation_available_after` minimum — and returns only a summary. Every live
tick that meets a correction record therefore re-derives them by reading the
whole journal prefix. This module turns those six into durable rows and merges a
delta into them, which is what lets a validated generation continue the fold
instead of restarting it.

Two rules govern what is stored, and both are load-bearing:

**No per-candidate table.** Same-revision containment needs only, per event id,
the winning revision, the lowest-sequence winner and the set of distinct content
hashes observed at that revision. `journal_effective_events` is already keyed
that way, so two added columns complete it.

**Action cores are retained while a batch is `begin_only` OR `tainted`, and
dropped only on `completed`.** An early taint does not end a batch's record
stream: later actions and a commit can still establish a further violation such
as `manifest_actions_hash_mismatch`, whose derivation hashes every first-seen
action core. Dropping cores at taint would leave that underivable. Dropping on
`completed` is safe in both directions — a duplicate that matches changes
nothing, and one that conflicts is detected from the retained whole-record
digest, which taints the batch and forces a rebuild that re-derives from the
journal.

This module imports nothing outside the stdlib and `_lib_journal`, and in
particular never imports `_cctally_journal`, so it is unit-testable without a
journal on disk — the same rule `bin/_lib_journal_router.py` follows.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace

import _lib_journal as _jl


#: Bumped whenever the durable row shapes or the merge semantics change. A
#: mismatch is one of the states that falls back to full selection silently.
SELECTOR_VERSION = 1

#: What `_lib_journal.decode_line` returns for a line that is not a record. A
#: malformed line produces NO decoded entry, so it consumes no sequence number.
MALFORMED = None

_BEGIN_ONLY = "begin_only"
_COMPLETED = "completed"
_TAINTED = "tainted"


class IncrementalSelectionUnavailable(Exception):
    """The delta contains something an incremental merge may not decide alone.

    Two things reach it:

    - a `journal_protocol_resolution` op, because acknowledging a violation
      requires an exact length-framed raw-prefix SHA-256 and no semantic summary
      can reconstruct that hash, so the caller must fall back to full selection
      rather than accept a claimed one; and
    - a batch whose durable status is `completed` gaining a marker phase or an
      action sequence the durable rows do not hold. Its action cores were
      dropped at completion, so its verdict can only be carried forward, and
      carrying it forward over a record that a full pass would have folded into
      the verdict is exactly how the two paths diverge.
    """


# --------------------------------------------------------------------------
# row shapes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SelectorStateRow:
    """`journal_selector_state` — one row."""

    next_sequence: int
    selector_version: int = SELECTOR_VERSION
    covered_segment: "str | None" = None
    covered_offset: "int | None" = None
    #: Written at PUBLICATION, not at scratch construction: the publication
    #: stamp does not exist while the scratch is being built, so a row populated
    #: then cannot carry the identity it will publish under.
    generation_record_path: "str | None" = None
    generation_stamped_at_utc: "str | None" = None
    #: `cutover_seen` distinguishes "no cutover op exists" from "the op exists
    #: and recorded no account". A plain NULL cannot carry both answers.
    cutover_seen: bool = False
    cutover_account_key: "str | None" = None


@dataclass(frozen=True)
class SelectorBatchRow:
    """`journal_selector_batches` — one row per correction batch."""

    batch_id: str
    status: str
    action_count: "int | None" = None
    action_set_hash: "str | None" = None
    begin_segment: "str | None" = None
    begin_offset: "int | None" = None
    earliest_commit_segment: "str | None" = None
    earliest_commit_offset: "int | None" = None


@dataclass(frozen=True)
class SelectorBatchRecordRow:
    """`journal_selector_batch_records` — one row per marker and per action."""

    batch_id: str
    kind: str          # "marker" | "action"
    key: str           # the phase, or the action sequence rendered as text
    record_digest: str
    sequence: int
    identity_digest: "str | None" = None
    action_core_json: "str | None" = None


@dataclass(frozen=True)
class SelectorEffectiveRow:
    """`journal_effective_events` — one row per event id."""

    event_id: str
    rev: int
    status: str
    content_hash: str
    batch_id: "str | None"
    event_json: "str | None"
    winning_sequence: int
    conflict_hashes_json: "str | None" = None


@dataclass(frozen=True)
class SelectorViolationRow:
    """`journal_protocol_violations` — one row per distinct violation."""

    fingerprint: str
    batch_id: str
    kind: str
    violation_json: str
    available_after: "int | None" = None


@dataclass(frozen=True)
class SelectorRows:
    """One generation's durable selector state, as comparable value objects."""

    state: SelectorStateRow
    batches: tuple = ()
    batch_records: tuple = ()
    effective: tuple = ()
    violations: tuple = ()


@dataclass(frozen=True)
class TaintTransition:
    """A batch whose durable status was `completed` and is now `tainted`.

    `causal_sequence` is the sequence of the FIRST record that established the
    taint — never the batch's earliest commit. A rebuild bounded at the commit
    excludes the tainting record, faithfully reproduces the completed
    correction, and meets the same taint on the next tick, which is a livelock
    rather than a recovery (spec §3.7).
    """

    batch_id: str
    causal_sequence: int


# --------------------------------------------------------------------------
# the decoded-entry counting primitive
# --------------------------------------------------------------------------

def decoded_entry_count(decode_results) -> int:
    """How many `decoded` entries a stream of PHYSICAL LINES produces.

    ``decode_results`` is one element per physical journal line — exactly what
    `_lib_journal.decode_line` returned for it — so ``None`` here means the line
    FAILED TO DECODE. That is the opposite of the `None` in the rebuild's
    `decoded` list, which is a placeholder for a valid non-retained record; the
    two conventions are distinct and this primitive takes the first.

    Mirrors the rebuild read loop's branching exactly: a decoded record produces
    ONE entry whether or not its type is retained, because the loop appends an
    explicit `None` PLACEHOLDER for a valid non-retained record; a line that
    failed to decode produces none, because the loop skips and counts it
    separately.

    That distinction is not bookkeeping. `resolve_effective_events` numbers
    candidates with `enumerate(records)`, three of the seven structural
    violation kinds put that number inside `ProtocolViolation.evidence`, and the
    fingerprint hashes it. The fingerprint is durable — it lands in
    `journal_protocol_violations` and is referenced by name from a
    `journal_protocol_resolution` op — so renumbering would make a previously
    acknowledged violation unresolvable and raise on every later rebuild. An
    elided segment therefore contributes exactly this count in its stead.
    """
    return sum(1 for result in decode_results if isinstance(result, dict))


# --------------------------------------------------------------------------
# full selection -> durable rows
# --------------------------------------------------------------------------

def _batch_status(fold, batch_id: str) -> str:
    if batch_id in fold.tainted_batches:
        return _TAINTED
    if batch_id in fold.completed:
        return _COMPLETED
    return _BEGIN_ONLY


def _coordinate(coordinates, sequence):
    if not coordinates or sequence is None:
        return (None, None)
    found = coordinates.get(sequence)
    return (None, None) if found is None else (found[0], int(found[1]))


def _canonical_json(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=False)


def _batch_rows(fold, coordinates):
    batches = []
    records = []
    for batch_id in sorted(set(fold.markers) | set(fold.actions)):
        status = _batch_status(fold, batch_id)
        markers = fold.markers.get(batch_id, {})
        begin = markers.get("begin")
        commit = markers.get("commit")
        begin_segment, begin_offset = _coordinate(
            coordinates, begin[3] if begin else None)
        commit_segment, commit_offset = _coordinate(
            coordinates, commit[3] if commit else None)
        batches.append(
            SelectorBatchRow(
                batch_id=batch_id,
                status=status,
                action_count=begin[0]["action_count"] if begin else None,
                action_set_hash=begin[0]["actions_hash"] if begin else None,
                begin_segment=begin_segment,
                begin_offset=begin_offset,
                earliest_commit_segment=commit_segment,
                earliest_commit_offset=commit_offset,
            )
        )
        for phase in sorted(markers):
            _core, digest, identity_digest, sequence = markers[phase]
            records.append(
                SelectorBatchRecordRow(
                    batch_id=batch_id,
                    kind="marker",
                    key=phase,
                    record_digest=digest,
                    sequence=sequence,
                    identity_digest=identity_digest,
                )
            )
        for seq in sorted(fold.actions.get(batch_id, {})):
            normalized, digest, sequence = fold.actions[batch_id][seq]
            records.append(
                SelectorBatchRecordRow(
                    batch_id=batch_id,
                    kind="action",
                    key=str(seq),
                    record_digest=digest,
                    sequence=sequence,
                    action_core_json=(
                        None
                        if status == _COMPLETED or normalized is None
                        else _canonical_json(
                            _jl._correction_action_core(normalized))
                    ),
                )
            )
    # Canonical order `(batch_id, kind, key)` — the same order a durable read
    # returns, so a stored generation and an in-memory derivation compare equal
    # without either side re-sorting. `key` is text on both sides, so SQLite's
    # BINARY collation and Python's string ordering agree.
    records.sort(key=lambda row: (row.batch_id, row.kind, row.key))
    return tuple(batches), tuple(records)


def _effective_rows(selection):
    conflicts = {
        conflict.event_id: list(conflict.content_hashes)
        for conflict in selection.conflicts
    }
    rows = []
    for event_id, selected in selection.by_id.items():
        event_json = None
        if selected.record is not None:
            event_json = (
                _jl.encode_line(selected.record).decode("utf-8").rstrip("\n")
            )
        hashes = conflicts.get(event_id)
        rows.append(
            SelectorEffectiveRow(
                event_id=event_id,
                rev=selected.rev,
                status=selected.status,
                content_hash=selected.content_hash,
                batch_id=selected.batch_id,
                event_json=event_json,
                winning_sequence=selected.sequence,
                conflict_hashes_json=(
                    None if hashes is None else _canonical_json(hashes)
                ),
            )
        )
    rows.sort(key=lambda row: row.event_id)
    return tuple(rows)


def violation_rows(selection, fold):
    """Durable violation rows for ONE full selection.

    Public because the live path's full-prefix fallback writes the same rows the
    rebuild does. Deriving them twice, in two places, is how the two ended up
    disagreeing on `available_after` and on `ensure_ascii`.
    """
    rows = []
    for violation in (
        *selection.protocol_violations,
        *selection.acknowledged_protocol_violations,
    ):
        rows.append(
            SelectorViolationRow(
                fingerprint=violation.fingerprint,
                batch_id=violation.batch_id,
                kind=violation.kind,
                violation_json=_canonical_json(violation.to_dict()),
                available_after=fold.violation_available_after.get(
                    violation.fingerprint),
            )
        )
    rows.sort(key=lambda row: (row.batch_id, row.kind, row.fingerprint))
    return tuple(rows)


def rows_from_selection(
    selection,
    *,
    accumulators,
    next_sequence: int,
    coordinates=None,
    covered=None,
    cutover_seen: bool = False,
    cutover_account_key: "str | None" = None,
) -> SelectorRows:
    """Durable rows for ONE full selection.

    ``accumulators`` is the out-dict `resolve_effective_events` populated, which
    is where the six accumulators live; `EffectiveSelection` deliberately did
    not grow a field for them, so every existing caller stays byte-unaffected.

    ``next_sequence`` is the pass's TOTAL decoded-entry count — the sequence the
    next stream must start at.

    ``coordinates`` maps a sequence number to its ``(segment, end_offset)``.
    Only correction-batch MARKER sequences are ever looked up, so a caller need
    populate no more than those; a missing entry leaves the coordinate columns
    NULL rather than guessing one.
    """
    fold = accumulators["fold"]
    batches, batch_records = _batch_rows(fold, coordinates)
    covered_segment, covered_offset = (
        (None, None) if covered is None else (covered[0], int(covered[1]))
    )
    return SelectorRows(
        state=SelectorStateRow(
            next_sequence=next_sequence,
            selector_version=SELECTOR_VERSION,
            covered_segment=covered_segment,
            covered_offset=covered_offset,
            cutover_seen=bool(cutover_seen),
            cutover_account_key=cutover_account_key,
        ),
        batches=batches,
        batch_records=batch_records,
        effective=_effective_rows(selection),
        violations=violation_rows(selection, fold),
    )


# --------------------------------------------------------------------------
# durable rows -> a seeded fold
# --------------------------------------------------------------------------

def comparable(rows: "SelectorRows | None"):
    """``rows`` with the generation identity cleared, for validation.

    The identity is written at PUBLICATION, so a durable generation carries it
    and a fresh derivation from the journal cannot. Comparing it would make
    every validation of a published index fail. Everything else — the covered
    prefix, `next_sequence`, `selector_version`, the cutover pair, and all four
    row groups — IS compared.
    """
    if rows is None:
        return None
    return replace(
        rows,
        state=replace(
            rows.state,
            generation_record_path=None,
            generation_stamped_at_utc=None,
        ),
    )


def _seed_fold(rows: SelectorRows):
    """Reconstruct the accumulator state the durable rows describe.

    A marker's normalized core is rebuilt from the batch row's `action_count`
    and `action_set_hash`, which is exactly the subset phase 2 reads, plus the
    stored identity digest — and identity-digest equality implies core equality,
    because the identity digest covers the whole record minus its phase.
    """
    fold = _jl.SelectorFold()
    by_batch = {row.batch_id: row for row in rows.batches}
    for row in rows.batch_records:
        batch = by_batch.get(row.batch_id)
        if row.kind == "marker":
            core = {
                "action_count": None if batch is None else batch.action_count,
                "actions_hash": None if batch is None else batch.action_set_hash,
            }
            fold.markers.setdefault(row.batch_id, {})[row.key] = (
                core, row.record_digest, row.identity_digest, row.sequence,
            )
        else:
            normalized = None
            if row.action_core_json is not None:
                # `_correction_action_core` deliberately excludes `v`, `t`,
                # `batch` and `seq`, which `_validate_correction_record` does
                # produce and which `_candidate_from_correction` and phase 2
                # read. They are restored around the stored core rather than
                # widened into it, so the stored JSON stays the canonical core
                # the manifest hash is computed over while the reconstructed
                # record is field-for-field what a fresh validation returns.
                normalized = {
                    "v": _jl.LINE_VERSION,
                    "t": "correction",
                    **json.loads(row.action_core_json),
                    "batch": row.batch_id,
                    "seq": int(row.key),
                }
            fold.actions.setdefault(row.batch_id, {})[int(row.key)] = (
                normalized, row.record_digest, row.sequence,
            )
    for row in rows.batches:
        if row.status == _TAINTED:
            fold.tainted_batches.add(row.batch_id)
        elif row.status == _COMPLETED:
            fold.completed.add(row.batch_id)
    # `fold.violations` is deliberately NOT seeded. Phase 2 never reads it, and
    # `_merged_violation_rows` decides each stored row against the re-resolved
    # verdict instead: it keeps a phase-1 row (not re-derivable from the first
    # record this seeds) and an acknowledged one (whose richer JSON a
    # re-derivation would not reproduce), and drops a phase-2 row the
    # re-resolution withdrew.
    return fold


def _event_from_row(row) -> "object":
    return _jl.EffectiveEvent(
        event_id=row.event_id,
        rev=row.rev,
        status=row.status,
        content_hash=row.content_hash,
        batch_id=row.batch_id,
        record=(
            None if row.event_json is None else json.loads(row.event_json)
        ),
        sequence=row.winning_sequence,
    )


def _merge_candidates(rows: SelectorRows, candidates):
    """Fold new candidates into the durable winners, reproducing #374 exactly.

    Only the WINNING revision is seeded, and that is sufficient: a candidate
    below the winning revision can never take the winner, and its same-revision
    group is filtered out by the revision-scoping rule anyway.

    Returns winners for the event ids the DELTA NAMES, not for every durable
    row. That scoping is what keeps the live path affordable: this runs inside
    an ingest tick, and materializing every stored winner would parse each
    row's retained record — at most 34,644 JSON documents on the maintainer's
    journal, an upper bound rather than a count, because a row whose
    `event_json` is NULL parses nothing — on a tick that names two or three of
    them. An untouched winner
    cannot change, so its row passes through verbatim (see
    `_merged_effective_rows`).
    """
    prior_rows = {row.event_id: row for row in rows.effective}
    winners: dict = {}
    conflicts: dict = {}

    def seeded(event_id):
        """The durable winner for ``event_id``, materialized on first use."""
        if event_id in winners:
            return winners[event_id]
        row = prior_rows.get(event_id)
        if row is None:
            return None
        if row.conflict_hashes_json is not None:
            conflicts.setdefault(
                event_id, set(json.loads(row.conflict_hashes_json)))
        return _event_from_row(row)

    for candidate in sorted(candidates, key=lambda item: item.sequence):
        prior = seeded(candidate.event_id)
        if prior is None or candidate.rev > prior.rev:
            winners[candidate.event_id] = candidate
            conflicts.pop(candidate.event_id, None)
            continue
        if candidate.rev < prior.rev:
            winners.setdefault(candidate.event_id, prior)
            continue
        if (
            prior.content_hash == candidate.content_hash
            and prior.status == candidate.status
        ):
            if prior.sequence is None:
                # A durable row with no winning sequence came from the LIVE emit
                # path, which writes the six legacy columns for an evt it
                # journals past the cycle's own high-water. The next cycle reads
                # that line and folds it here at a known sequence, so adopting
                # the candidate replaces an unknown with the number a full
                # derivation would compute. Without it the row stays sequenceless
                # forever and `stats_index_matches_journal_prefix` can never
                # agree again, which is the shape of the defect this session was
                # asked to close rather than relocate.
                winners[candidate.event_id] = candidate
            else:
                winners.setdefault(candidate.event_id, prior)
            continue
        if (
            _jl._is_legacy_quota_arming_state(prior)
            and _jl._is_legacy_quota_arming_state(candidate)
        ):
            # Legacy qaa carve-out: last-wins AND silent. A pre-#372 arming
            # record deliberately reuses its natural id as a state stream, so
            # successive lines are not a conflict.
            winners[candidate.event_id] = candidate
            continue
        winners.setdefault(candidate.event_id, prior)
        conflicts.setdefault(candidate.event_id, set()).update(
            {prior.content_hash, candidate.content_hash}
        )
    return winners, conflicts


def _merged_effective_rows(rows: SelectorRows, winners, conflicts):
    """Durable winners advanced by the delta.

    A row the delta did not name passes through VERBATIM. It cannot have
    changed — nothing else in the fold reaches it — and re-encoding it would
    re-serialize every retained record on every tick.
    """
    merged = [row for row in rows.effective if row.event_id not in winners]
    for event_id, selected in winners.items():
        event_json = None
        if selected.record is not None:
            event_json = (
                _jl.encode_line(selected.record).decode("utf-8").rstrip("\n")
            )
        hashes = conflicts.get(event_id)
        merged.append(
            SelectorEffectiveRow(
                event_id=event_id,
                rev=selected.rev,
                status=selected.status,
                content_hash=selected.content_hash,
                batch_id=selected.batch_id,
                event_json=event_json,
                winning_sequence=selected.sequence,
                conflict_hashes_json=(
                    None if not hashes else _canonical_json(sorted(hashes))
                ),
            )
        )
    merged.sort(key=lambda row: row.event_id)
    return tuple(merged)


#: The two violation kinds phase 1 establishes, from a DUPLICATE record whose
#: digest differs from the one already accumulated.
#:
#: They are separated from the other five because they behave differently under
#: re-resolution. A phase-1 violation is MONOTONE: the duplicate is durably in
#: the journal, so every later full derivation reproduces it — but an
#: incremental pass cannot, because `_seed_fold` restores only the FIRST record
#: at each phase and action sequence, which is what the durable rows store. The
#: five phase-2 kinds are the opposite: `resolve_batches` re-derives all of them
#: from the accumulated batch state on every pass, and a later record can
#: WITHDRAW one — an incomplete action set completed by a late action stops
#: producing `manifest_action_sequence_mismatch`.
PHASE_ONE_VIOLATION_KINDS = frozenset(
    {"marker_conflict", "action_sequence_conflict"}
)


def _is_acknowledged(row: SelectorViolationRow) -> bool:
    """Whether ``row``'s stored JSON is an acknowledged violation's richer shape.

    `AcknowledgedProtocolViolation.to_dict` adds `auditId`, `journalHighWater`
    and `journalPrefixHash` to the plain violation dict, and an incremental fold
    never carries a resolution — `merge_delta` refuses a delta that contains one
    — so a re-derivation here can never reproduce that shape. Such a row is
    therefore never withdrawn.

    Retaining it is conservative only about THIS path's own state. It is not
    harmless in general: when a later record genuinely withdraws an acknowledged
    phase-2 violation, a full derivation raises `JournalProtocolError` at
    `bin/_lib_journal.py:1105` for an acknowledgement that resolves nothing, so
    every later rebuild is wedged while the incremental path keeps serving the
    stale row. That is pre-existing — a full derivation reaches the same state
    without this function — and production holds zero
    `journal_protocol_resolution` ops, so nothing here can reach it today.
    """
    try:
        return "auditId" in json.loads(row.violation_json)
    except (TypeError, ValueError):
        return True


def _with_earliest_available_after(stored, derived):
    """``stored`` carrying the earlier of the two rows' ``available_after``."""
    if derived is None or derived.available_after is None:
        return stored
    if stored.available_after is None:
        return replace(stored, available_after=derived.available_after)
    if derived.available_after >= stored.available_after:
        return stored
    return replace(stored, available_after=derived.available_after)


def _merged_violation_rows(rows: SelectorRows, fold, resolved_batches):
    """Durable violation rows advanced by the delta, WITHDRAWALS included.

    ``resolved_batches`` names the batches phase 2 actually re-resolved in this
    delta. For exactly those, `fold.violations` is the complete phase-2 verdict a
    full derivation would produce, so a stored phase-2 row the re-resolution did
    not reproduce has been withdrawn and must be dropped rather than unioned
    forward. Unioning left a withdrawn `manifest_action_sequence_mismatch`
    durable after the missing action arrived, which made `doctor`'s
    `journal.protocol` leg FAIL and print a `db journal-repair --violation
    <fingerprint>` command naming a fingerprint no fresh derivation reproduces.

    Two classes of stored row are never withdrawn. A **phase-1** row is monotone
    and not re-derivable from the seeded fold (`PHASE_ONE_VIOLATION_KINDS`), and
    an **acknowledged** row carries operator audit an incremental pass cannot
    reconstruct.

    A batch outside ``resolved_batches`` is untouched for the same reason its
    other rows are: it received no record this delta could fold.

    **The stored row wins for a fingerprint both produce, except on
    ``available_after``.** Stored-wins is required, because an acknowledged row's
    richer JSON is exactly what a re-derivation cannot reproduce. But
    `available_after` is not one of the fingerprint's inputs, so the two rows can
    legitimately disagree on it, and it is a MINIMUM over sightings
    (`SelectorFold.taint`) rather than a last-write value. Taking the pointwise
    minimum is therefore the correct merge in both directions: it can never
    regress an earlier boundary a longer derivation established, and it adopts a
    value for a stored row that a four-column fallback left NULL.
    """
    derived = {}
    for (batch_id, kind, fingerprint), violation in fold.violations.items():
        derived[fingerprint] = SelectorViolationRow(
            fingerprint=fingerprint,
            batch_id=batch_id,
            kind=kind,
            violation_json=_canonical_json(violation.to_dict()),
            available_after=fold.violation_available_after.get(fingerprint),
        )
    merged: dict = {}
    for row in rows.violations:
        if (
            row.batch_id in resolved_batches
            and row.fingerprint not in derived
            and row.kind not in PHASE_ONE_VIOLATION_KINDS
            and not _is_acknowledged(row)
        ):
            continue
        merged[row.fingerprint] = _with_earliest_available_after(
            row, derived.get(row.fingerprint))
    for fingerprint, row in derived.items():
        merged.setdefault(fingerprint, row)
    ordered = sorted(
        merged.values(), key=lambda row: (row.batch_id, row.kind, row.fingerprint)
    )
    return tuple(ordered)


#: Record types `_lib_journal._fold_one` actually consumes. Everything else
#: leaves the fold untouched and only consumes a sequence number.
FOLD_RECORD_TYPES = frozenset({"evt", "correction", "correction_batch"})


def delta_batch_scope(records) -> set:
    """Every correction batch a delta of ``records`` can reach.

    A `correction_batch` names its batch through `id` and a `correction` through
    `batch`. A batch the delta does not name receives no new record, so phase 1
    cannot taint it and phase 2 cannot change its verdict, and its durable rows
    stand untouched — which is what lets the caller read `journal_selector_
    batches` and `journal_selector_batch_records` scoped to this set instead of
    materializing all 64,248 batch-record rows a production journal holds on
    every tick.
    """
    scope = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        kind = record.get("t")
        if kind == "correction_batch":
            batch_id = record.get("id")
        elif kind == "correction":
            batch_id = record.get("batch")
        else:
            continue
        if isinstance(batch_id, str):
            scope.add(batch_id)
    return scope


def delta_event_scope(records, batch_records=()) -> set:
    """Every event id `_merge_candidates` can look a durable winner up for.

    Three sources, and all three are necessary:

    - an `evt` record is a candidate for its own `id`;
    - a `correction` action replaces the event named by its `id`;
    - an action of a `begin_only` or `tainted` batch that arrived in an EARLIER
      generation names an id no delta record mentions. That is the split-cycle
      case, and it is exactly why those batches retain their action cores.

    A `completed` batch is skipped by phase 2 and produces no candidate, so its
    dropped cores cannot widen the scope and their absence is not a gap.
    """
    scope = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("t") in ("evt", "correction"):
            event_id = record.get("id")
            if isinstance(event_id, str):
                scope.add(event_id)
    for row in batch_records:
        if row.kind != "action" or row.action_core_json is None:
            continue
        event_id = json.loads(row.action_core_json).get("id")
        if isinstance(event_id, str):
            scope.add(event_id)
    return scope


def advance_counter(
    rows: SelectorRows,
    *,
    consumed: int,
    covered=None,
) -> SelectorRows:
    """Advance ONLY the prefix counters, reusing every row object.

    For a delta the fold does not consume — observations and ordinary ops — the
    merge is the identity on all four row groups, and running it anyway would
    make an ordinary status-line tick pay for the whole durable generation. The
    row tuples are returned by reference, which is also what lets the glue's
    delta writer skip those groups outright rather than diff them.

    The cutover pair carries forward and cannot be set here. An incremental pass
    may never adopt a cutover operation the durable prefix has not folded: a full
    derivation applies the legacy account stamp to every legacy Claude line in
    that prefix, changing those events' `content_hash` and `event_json`, and this
    path normalizes only the delta. The caller falls back instead.
    """
    return SelectorRows(
        state=replace(
            rows.state,
            next_sequence=consumed,
            selector_version=SELECTOR_VERSION,
            covered_segment=(
                rows.state.covered_segment if covered is None else covered[0]),
            covered_offset=(
                rows.state.covered_offset if covered is None
                else int(covered[1])),
        ),
        batches=rows.batches,
        batch_records=rows.batch_records,
        effective=rows.effective,
        violations=rows.violations,
    )


def merge_delta(
    rows: SelectorRows,
    new_records,
    *,
    next_sequence: int,
    coordinates=None,
    covered=None,
):
    """Continue the fold over ``new_records`` from durable state.

    ``next_sequence`` is the sequence the FIRST new entry takes, which is the
    durable state's own `next_sequence`.

    Returns ``(rows, transitions)``. ``transitions`` names every batch whose
    durable status was `completed` and which this delta tainted; the caller
    turns each into a `CorrectionRebuildRequired` bounded at the causal record,
    because carrying a stale `completed` status forward is the one way an
    incremental path can make the pre-existing #510 staleness worse.

    The cutover pair carries forward and cannot be set here, for the reason
    `advance_counter` gives.

    Raises :class:`IncrementalSelectionUnavailable` when the delta contains a
    `journal_protocol_resolution` op: acknowledging a violation authenticates an
    exact length-framed raw-prefix SHA-256, a claimed hash is never accepted
    without recomputation, and no durable summary can reconstruct it.
    """
    fold = _seed_fold(rows)
    seeded_completed = {
        row.batch_id for row in rows.batches if row.status == _COMPLETED
    }
    # The shape a completed batch had when its cores were dropped. Phase 2 is
    # skipped for these batches, so anything the delta ADDS to one of them is
    # never folded into a verdict — see the refusal below.
    seeded_shape = {
        batch_id: (
            frozenset(fold.markers.get(batch_id, {})),
            frozenset(fold.actions.get(batch_id, {})),
        )
        for batch_id in seeded_completed
    }
    consumed = _jl.fold_records(fold, new_records, start_sequence=next_sequence)
    if fold.resolutions:
        raise IncrementalSelectionUnavailable(
            "a journal_protocol_resolution op requires a verified raw-prefix "
            "read; fall back to full selection"
        )
    for batch_id, (markers, actions) in sorted(seeded_shape.items()):
        # A DUPLICATE marker or action is safe to carry forward: phase 1 keeps
        # the first record and taints from the retained whole-record digest when
        # the duplicate differs, which the transition loop below then reports. A
        # record at a phase or action sequence the durable rows do not hold is
        # different in kind — a full pass would re-run phase 2 over the widened
        # set and could taint the batch (`manifest_action_sequence_mismatch`,
        # `record_order_violation`), while this path would keep it completed and
        # raise nothing. The cores were dropped at completion, so re-deriving the
        # verdict here is not an option; refusing is.
        if (
            frozenset(fold.markers.get(batch_id, {})) != markers
            or frozenset(fold.actions.get(batch_id, {})) != actions
        ):
            raise IncrementalSelectionUnavailable(
                f"batch {batch_id} is durably completed and the delta adds a "
                "marker phase or action sequence its stored rows do not hold; "
                "fall back to full selection"
            )
    resolved_batches = (
        set(fold.markers) | set(fold.actions)
    ) - seeded_completed
    _jl.resolve_batches(fold, batch_ids=resolved_batches)
    fold.completed |= seeded_completed - fold.tainted_batches

    transitions = []
    for batch_id in sorted(seeded_completed & fold.tainted_batches):
        # A completed batch carries no violations — `completed` and `tainted`
        # are disjoint — so every violation now standing against it was
        # established by THIS delta, and the earliest of them is the record that
        # caused the transition.
        causal = [
            fold.violation_available_after[fingerprint]
            for (candidate_batch, _kind, fingerprint) in fold.violations
            if candidate_batch == batch_id
            and fingerprint in fold.violation_available_after
        ]
        if not causal:
            # The causal offset is MANDATORY and this path fails closed without
            # it. Substituting the pinned high-water is unsafe: it is `st_size`,
            # `_iter_segment_lines` omits an incomplete trailing line, and
            # torn-tail repair can truncate below it, so a cursor written there
            # can sit beyond unread data (spec §3.7).
            raise IncrementalSelectionUnavailable(
                f"batch {batch_id} moved completed -> tainted with no causal "
                "offset; fall back to full selection"
            )
        transitions.append(
            TaintTransition(batch_id=batch_id, causal_sequence=min(causal))
        )
    # CAUSAL order, not batch-id order. The caller raises on the FIRST
    # transition, so raising the batch whose causal record sits later would
    # rebuild through a longer prefix than necessary. Both orders converge;
    # this one converges through the narrowest prefix.
    transitions.sort(key=lambda item: (item.causal_sequence, item.batch_id))

    winners, conflicts = _merge_candidates(rows, fold.candidates)
    batches, batch_records = _batch_rows(fold, coordinates)
    merged_batches = _carry_coordinates(rows, batches)
    state = replace(
        rows.state,
        next_sequence=consumed,
        selector_version=SELECTOR_VERSION,
        covered_segment=(
            rows.state.covered_segment if covered is None else covered[0]),
        covered_offset=(
            rows.state.covered_offset if covered is None else int(covered[1])),
    )
    return (
        SelectorRows(
            state=state,
            batches=merged_batches,
            batch_records=batch_records,
            effective=_merged_effective_rows(rows, winners, conflicts),
            violations=_merged_violation_rows(rows, fold, resolved_batches),
        ),
        transitions,
    )


def _carry_coordinates(rows: SelectorRows, batches):
    """Keep a coordinate the durable row already holds.

    A delta's `coordinates` map covers only the delta's own records, so a batch
    whose begin marker arrived in an earlier generation would otherwise lose the
    coordinate that generation resolved.
    """
    prior = {row.batch_id: row for row in rows.batches}
    carried = []
    for row in batches:
        old = prior.get(row.batch_id)
        if old is None:
            carried.append(row)
            continue
        carried.append(
            replace(
                row,
                begin_segment=row.begin_segment or old.begin_segment,
                begin_offset=(
                    row.begin_offset if row.begin_offset is not None
                    else old.begin_offset),
                earliest_commit_segment=(
                    row.earliest_commit_segment or old.earliest_commit_segment),
                earliest_commit_offset=(
                    row.earliest_commit_offset
                    if row.earliest_commit_offset is not None
                    else old.earliest_commit_offset),
            )
        )
    return tuple(carried)
