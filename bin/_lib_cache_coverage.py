"""Journal-to-cache coverage certificate — the pure kernel (#496 S5b, spec §4).

A stats rebuild replays every retained Codex quota observation into `cache.db`
under both cache writer flocks, whether or not the cache already holds them. On
the maintainer's store that is roughly 1.81 million observations and a 23.0 s
warm hold of the lock issue #297 blames for `database is locked`. The
certificate is what lets an intact cache be recognized instead of replayed.

**Its single promise is coverage, and nothing wider:**

    every cache-relevant journal record in the covered prefix has already been
    applied to `cache.db`.

It does **not** promise that `cache.db` contains only rows the journal explains.
`_append_codex_quota_obs` is deliberately best-effort — it catches every
exception so a failed journal append cannot break a sync, and it runs before the
cache write — so a swallowed failure leaves a cache row the journal lacks. That
is divergence in the `cache ⊃ journal` direction, which a coverage-only promise
does not cover and does not need to.

It does **not** promise that any individual row's values are correct. File-account
rows are first-wins, incarnation rows are MAX-set, and quota replay is
`INSERT OR IGNORE`, so a wrong pre-existing row survives a replay from byte zero
and would then be certified by it. A replay cannot prove correctness, so the
certificate does not claim it.

**The identity root binds the ordered vector of extents, not segment names.** A
late append into a non-last segment changes no name and moves no ordering, so a
name-only root stays valid while the cache lacks that observation, and the fast
path would skip a quota replay today's rebuild performs. #511's target
revalidation is what makes the extent vector stable for the duration of a pass;
this module is what makes a change to it invalidate the certificate.

**A covered extent is always a verified newline boundary, never a raw size.** The
promise concerns decoded records, so covering a raw torn-tail extent would let
`_repair_torn_tail` truncate that suffix and append a complete record ending at
the same size — leaving `(segment, size)` identical while the covered
contribution changed. The pinned vector therefore carries BOTH the raw `st_size`
and the complete-line offset per segment, and `coveredHighWater` is bounded to
the latter.

This module imports nothing outside the stdlib, so it is unit-testable without a
cache or a journal on disk — the same rule `bin/_lib_journal_router.py` follows.
"""
from __future__ import annotations

import hashlib
import json


#: The `cache_meta` key. Distinct from `codex_quota_projection_certificate`:
#: coverage binds journal-to-cache, the projection certificate binds
#: cache-to-stats, and neither may satisfy the other's gate.
CERTIFICATE_KEY = "codex_journal_coverage_certificate"

#: The `cache_meta` key for a recovery pass's in-flight progress, which is
#: deliberately SEPARATE from the certificate (spec §4.5). A certificate asserts
#: coverage; progress asserts only how far one pass has got, and a pass that
#: stops leaves progress behind without ever asserting coverage. Storing the two
#: under one key would make a partial pass look like a coverage claim.
PROGRESS_KEY = "codex_journal_coverage_progress"

#: Bumped whenever the certificate's own shape or validation semantics change.
#: A mismatch is one of the states that falls back to a full replay silently.
#:
#: Adding the required `appliedThrough` field did NOT bump it, and that is
#: deliberate rather than an oversight: no released binary ever wrote the
#: one-coordinate shape, and a certificate lacking the field reads
#: `REASON_MALFORMED` and falls back to a full replay — the same outcome a bump
#: would produce. A bump would additionally invalidate certificates written by
#: an in-development binary that already carries the field, for no gain.
COVERAGE_VERSION = 1

#: Bumped whenever the journal-record-to-cache-row materialization changes —
#: `_apply_quota_records`, `_apply_file_account_records`, or the §3.5 precedence
#: rule between them. A certificate written under different semantics describes a
#: cache this binary would not have produced, so it is rejected rather than
#: compared.
INTERPRETATION_VERSION = 1

#: The reason strings `certificate_is_valid` returns. They are stable, because
#: the rebuild record reports them (spec §6.3, "recorded, not silent").
REASON_OK = "ok"
REASON_ABSENT = "absent"
REASON_MALFORMED = "malformed"
REASON_COVERAGE_VERSION = "coverageVersion"
REASON_INTERPRETATION_VERSION = "interpretationVersion"
REASON_PHYSICAL_SEQ = "physicalMutationSeq"
REASON_IDENTITY_ROOT = "identityRoot"
REASON_COVERED_HIGH_WATER = "coveredHighWater"
#: No boundary could be resolved at all — the pass has no high water, or its
#: high-water segment is absent from the pinned vector. Distinct from
#: `REASON_IDENTITY_ROOT`, which says the journal moved: this one says the
#: question was never askable, and reporting the former would tell an operator
#: the root changed when nothing about the certificate was even consulted.
REASON_NO_BOUNDARY = "noBoundary"


def _canonical(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=False)


def normalize_vector(pinned_vector):
    """``pinned_vector`` as a tuple of ``(name, raw_extent, covered_offset)``.

    Accepts any iterable of three-element sequences so a caller may build it
    from tuples or from lists decoded out of JSON, and rejects anything else
    rather than hashing a shape nobody checked.
    """
    normalized = []
    for item in pinned_vector:
        name, raw_extent, covered_offset = item
        normalized.append((str(name), int(raw_extent), int(covered_offset)))
    return tuple(normalized)


def identity_root(pinned_vector) -> str:
    """SHA-256 over the ORDERED ``(name, raw_extent, covered_offset)`` triples.

    Ordered, because `list_segments` sorts bootstraps before observations and an
    inserted bootstrap changes the canonical order without changing any existing
    segment. Hashing the order is what makes that insertion invalidate the root.
    """
    payload = _canonical([list(item) for item in normalize_vector(pinned_vector)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CoverageOutOfVector(ValueError):
    """A covered boundary the pinned vector does not offer.

    Raised rather than asserted: `python -O` strips `assert`, and a guard that
    disappears under an optimized interpreter would store an out-of-vector
    certificate instead of refusing to build one. `certificate_is_valid` would
    reject it on first use, so the outcome is a replay either way — but the
    guard is stated as a real check so it holds in every interpreter.
    """


def advance(prior, *, covered, applied_through, pinned_vector,
            physical_seq) -> dict:
    """The certificate describing ``covered`` over ``pinned_vector``.

    ``covered`` is ``(segment, complete_line_offset)`` — a verified newline
    boundary, never a raw size, for the reason the module docstring gives.

    ``applied_through`` is the RAW journal coordinate the writer consumed to,
    and it is a separate field because the two are not the same number. The
    ingest cycle advances its scalar cursor to the raw high water, while the
    covered boundary is clamped down to the last line the pass actually decoded.
    Storing only the clamped value made the next writer compare its cursor
    against a boundary that was deliberately smaller, read the difference as a
    gap, and discard the predecessor — so one torn or malformed trailing line
    froze the certificate until the next rebuild. The contiguity check compares
    `appliedThrough`; the coverage claim is `coveredHighWater`; neither operand
    stands in for the other.

    ``prior`` is accepted and deliberately not merged into the result. A
    certificate is a statement about the CURRENT physical state, not an
    accumulation over previous ones, so a stale field cannot survive an advance.
    It is a parameter only so a caller cannot advance without having read the
    predecessor it is required to validate first.
    """
    del prior
    segment, offset = covered
    applied_segment, applied_offset = applied_through
    vector = normalize_vector(pinned_vector)
    # A mint that `certificate_is_valid` would immediately reject is a caller
    # bug, not a degraded state to fall back from: it means the covered boundary
    # names a segment or an offset the pinned vector does not offer. Refuse here
    # rather than storing it and discovering it one rebuild later.
    if not _covered_within((segment, offset), vector):
        raise CoverageOutOfVector(
            f"coverage {(str(segment), int(offset))!r} is outside the pinned "
            "vector"
        )
    return {
        "coverageVersion": COVERAGE_VERSION,
        "interpretationVersion": INTERPRETATION_VERSION,
        "physicalMutationSeq": int(physical_seq),
        "coveredHighWater": [str(segment), int(offset)],
        "appliedThrough": [str(applied_segment), int(applied_offset)],
        "identityRoot": identity_root(vector),
    }


def prior_is_extendable(prior, *, applied_through) -> "tuple[bool, str]":
    """Whether a contiguous writer may EXTEND ``prior`` rather than discard it.

    This is deliberately NOT `certificate_is_valid`. An advance's predecessor
    necessarily describes an older, smaller journal — the writer is about to
    certify records that grew a segment after the predecessor was stored — so
    its identity root cannot match this pass's pinned vector and its
    `physicalMutationSeq` predates the bump this transaction is about to make.
    Requiring either would refuse every advance that has ever been correct.

    What an advance CAN check is the part that does not move with the journal:
    the two version fields, and contiguity against the coordinate the
    predecessor was applied through. The versions matter because
    `_lib_cache_coverage.advance` re-stamps the CURRENT module constants and
    discards `prior`, so extending a certificate written under an older
    `interpretationVersion` would launder it into a current-version one and the
    next rebuild would skip exactly the replay the version bump exists to force.
    """
    if prior is None:
        return False, REASON_ABSENT
    if not isinstance(prior, dict):
        return False, REASON_MALFORMED
    try:
        coverage_version = int(prior["coverageVersion"])
        interpretation_version = int(prior["interpretationVersion"])
        stored_applied = prior["appliedThrough"]
        stored_segment, stored_offset = str(stored_applied[0]), int(
            stored_applied[1])
    except (KeyError, IndexError, TypeError, ValueError):
        return False, REASON_MALFORMED
    if coverage_version != COVERAGE_VERSION:
        return False, REASON_COVERAGE_VERSION
    if interpretation_version != INTERPRETATION_VERSION:
        return False, REASON_INTERPRETATION_VERSION
    try:
        expected = (str(applied_through[0]), int(applied_through[1]))
    except (IndexError, TypeError, ValueError):
        return False, REASON_MALFORMED
    if (stored_segment, stored_offset) != expected:
        return False, REASON_COVERED_HIGH_WATER
    return True, REASON_OK


def _vector_position(coordinate, vector):
    """``(segment_index, offset)`` for ``coordinate``, or None.

    The ordering comes from the pinned vector rather than from the segment name,
    because `list_segments` sorts bootstraps before observations and a name
    comparison would order those two families lexically instead of canonically.
    """
    try:
        name, offset = str(coordinate[0]), int(coordinate[1])
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    for index, (candidate, _raw_extent, _covered_offset) in enumerate(vector):
        if candidate == name:
            return (index, offset)
    return None


def applied_through_regresses(stored, applied_through, pinned_vector) -> bool:
    """Whether storing ``applied_through`` would move ``stored`` BACKWARD.

    The recovery mint is the one writer allowed to establish a certificate where
    none existed, and it stores with `prior=None`, so without this check it
    overwrites whatever is present. That is unreachable today only because the
    other certificate writer runs under the ingest lock `cmd_db_rebuild` holds
    exclusively — safety supplied by a lock in another module rather than by the
    mint. This applies the same monotonicity `progress_supersedes` already
    applies to progress records.

    An incomparable pair answers False: a stored coordinate naming a segment the
    pinned vector does not offer describes a journal this pass is not looking
    at, and `certificate_is_valid` already rejects it on the identity root.
    """
    if not isinstance(stored, dict):
        return False
    try:
        vector = normalize_vector(pinned_vector)
    except (TypeError, ValueError):
        return False
    stored_position = _vector_position(stored.get("appliedThrough"), vector)
    candidate_position = _vector_position(applied_through, vector)
    if stored_position is None or candidate_position is None:
        return False
    return stored_position > candidate_position


def _covered_within(covered, vector) -> bool:
    """Whether ``covered`` names a boundary the pinned vector actually offers."""
    if not covered or len(covered) != 2:
        return False
    name, offset = str(covered[0]), int(covered[1])
    for candidate, _raw_extent, covered_offset in vector:
        if candidate == name:
            return 0 <= offset <= covered_offset
    return False


def certificate_is_valid(cert, *, pinned_vector, physical_seq) -> "tuple[bool, str]":
    """``(verdict, reason)`` for one stored certificate against this pass.

    The reason is returned rather than logged: every degraded state here falls
    back to a full replay SILENTLY (spec §6.3), and the rebuild record is the one
    surface that reports which state it was.

    **Validity is an identity check, and the caller must still bound its elision
    by `coveredHighWater`.** A certificate covering only the first of three
    segments is VALID — `_covered_within` asks whether the boundary is one the
    pinned vector offers, not whether it reaches the end of the journal. A caller
    that reads this verdict as "the whole pinned prefix is covered" would skip a
    replay for the two segments the certificate says nothing about.
    """
    if cert is None:
        return False, REASON_ABSENT
    if not isinstance(cert, dict):
        return False, REASON_MALFORMED
    try:
        vector = normalize_vector(pinned_vector)
    except (TypeError, ValueError):
        return False, REASON_MALFORMED
    try:
        coverage_version = int(cert["coverageVersion"])
        interpretation_version = int(cert["interpretationVersion"])
        stored_seq = int(cert["physicalMutationSeq"])
        stored_root = str(cert["identityRoot"])
        covered = cert["coveredHighWater"]
        applied = cert["appliedThrough"]
        str(applied[0]), int(applied[1])
    except (KeyError, IndexError, TypeError, ValueError):
        return False, REASON_MALFORMED
    if coverage_version != COVERAGE_VERSION:
        return False, REASON_COVERAGE_VERSION
    if interpretation_version != INTERPRETATION_VERSION:
        return False, REASON_INTERPRETATION_VERSION
    if stored_seq != int(physical_seq):
        return False, REASON_PHYSICAL_SEQ
    if stored_root != identity_root(vector):
        return False, REASON_IDENTITY_ROOT
    try:
        if not _covered_within(covered, vector):
            return False, REASON_COVERED_HIGH_WATER
    except (TypeError, ValueError):
        return False, REASON_MALFORMED
    return True, REASON_OK


# --------------------------------------------------------------------------
# recovery progress (spec §4.5)
# --------------------------------------------------------------------------

#: What a resumed recovery pass compares before continuing. Every one of these
#: is read again after each lock reacquisition, because releasing the flocks
#: admits a destructive writer.
PROGRESS_FIELDS = (
    "passId", "startedAt", "chunks", "identityRoot",
    "physicalMutationSeq", "sourceRootsDigest", "coveredHighWater",
)

#: `resume` — the stored progress is this pass's own and still describes the
#: state it left behind, so the pass may keep going.
#:
#: **The record is a revalidation token, not a resume point, and the wording
#: matters because an earlier draft claimed the latter.** A fresh process always
#: starts at chunk zero: `_run_bounded_recovery` initializes `chunk_index = 0`
#: unconditionally and revalidates only from chunk 1 onward, so the stored
#: `chunks` is read by `progress_supersedes` for compare-and-swap ordering and
#: by nothing else. Recovery is therefore resumable WITHIN one process and
#: restart-from-zero ACROSS processes.
#:
#: That is the conservative direction and it was chosen over implementing the
#: cross-process resume. The reason is NOT that revalidation would have to move
#: ahead of chunk zero — an earlier draft said that and it does not follow, since
#: a process resuming at chunk k has `chunk_index = k > 0` and the existing
#: `if chunk_index > 0` revalidation already fires in its first transaction,
#: with §3.5's precedence satisfied for the earlier chunks by the earlier
#: process and every apply idempotent. The real awkwardness is that `chunks`
#: indexes a plan derived from THIS process's `quota_raw`, and a journal that
#: grew between the two passes gives the second process a different plan for
#: the same index — so the stored number would have to be re-expressed as a
#: journal coordinate to mean anything across processes. The identity-root
#: comparison already catches that mismatch, so nothing is unsafe today; the
#: cross-process resume is simply not worth the extra invariant, because
#: restarting is idempotent and costs repeated work and nothing else.
RESUME = "resume"
#: `restart` — no usable progress. Either a destructive writer deleted it in the
#: same transaction as its deletes, or it describes a state this pass cannot
#: continue from. The pass starts again from chunk zero, which is always sound
#: because every apply is idempotent on its natural key.
RESTART = "restart"
#: `yield` — a NEWER pass owns the progress record. This pass stops rather than
#: restarting, because two passes that each restart on seeing the other make no
#: progress at all.
YIELD = "yield"


def make_progress(*, pass_id, started_at, chunks, identity_root,
                  physical_seq, source_roots_digest, covered) -> dict:
    """One pass's progress record, in the canonical field order.

    There is no applied-record count. An earlier shape carried one and nothing
    ever read it, and a durable field nobody consumes is a field a later reader
    will mistake for state the mechanism depends on.
    """
    segment, offset = covered
    return {
        "passId": str(pass_id),
        "startedAt": int(started_at),
        "chunks": int(chunks),
        "identityRoot": str(identity_root),
        "physicalMutationSeq": int(physical_seq),
        "sourceRootsDigest": str(source_roots_digest),
        "coveredHighWater": [str(segment), int(offset)],
    }


def source_roots_digest(root_keys) -> str:
    """A stable digest over the cache's Codex source roots.

    `_clear_codex_derived_rows` empties `codex_source_roots` along with the
    quota rows, so this is a second, independent witness that the destructive
    path ran — one that does not depend on the progress delete having happened.
    """
    payload = _canonical(sorted(str(key) for key in root_keys))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: How far AHEAD of this pass's own start a foreign progress record may sit and
#: still be treated as a live concurrent pass rather than an orphan.
#:
#: `startedAt` is a wall clock compared with `>` across processes, and there is
#: no pid, heartbeat or TTL behind it. Two passes on one machine read the same
#: clock, so a genuinely live competitor's start time is at most seconds ahead.
#: A record arbitrarily far in the future means the clock stepped BACKWARD
#: between the two passes — an NTP correction or a VM restore — and without this
#: bound every later pass would look older than that orphan and yield to it on
#: every rebuild until the clock caught up. One hour is far past any real
#: inter-pass skew and far short of the clock steps that produce the failure.
PROGRESS_YIELD_MAX_SKEW_US = 3_600 * 1_000_000

#: A foreign record so far in the future that it cannot describe a live pass.
REASON_ORPHANED_PASS = "orphanedPass"


def resume_verdict(stored, *, pass_id, started_at, identity_root,
                   physical_seq, source_roots_digest) -> "tuple[str, str, bool]":
    """``(RESUME | RESTART | YIELD, reason, concurrent_writer)`` after a lock
    reacquisition.

    Ordering matters. A FOREIGN newer pass is checked before anything else,
    because yielding to it must not be turned into a restart by some other
    mismatch; a foreign OLDER pass is simply overwritten, which is what makes
    the compare-and-swap monotonic.

    **DELIBERATE DEVIATION from spec §4.5, which restarts on any mismatch of
    the four compared quantities. It narrows ONE of the four, not two.** A moved
    `physicalMutationSeq` is REPORTED — the third element of the result — but
    does not by itself restart the pass; a changed `sourceRootsDigest` restarts
    like every other mismatch. The liveness argument covers only the sequence.
    An ordinary rollout batch bumps the sequence on every status-line tick, and
    what it reports is an ADDITIVE writer whose rows this pass's `INSERT OR
    IGNORE` applies over without any loss of coverage, and whose bump the final
    transaction's mint reads anyway, so it cannot produce a stale-valid
    certificate. Restarting on it would abandon a pass every time a tick landed
    mid-recovery, and after the restart limit would report an uncovered
    remainder for a cache that has no shortfall at all.

    **The digest does not behave that way and must not share the narrowing.**
    It is computed over the SET of `source_root_key` values, so an ordinary
    batch's `INSERT … ON CONFLICT DO UPDATE SET last_seen_utc` does not move it;
    only a new root appearing or `_prune_inactive_codex_source_roots` deleting
    one does. `_clear_codex_derived_rows` empties `codex_source_roots` along
    with the quota rows, which makes the digest a second, independent witness
    that the destructive path ran — one that does not depend on the progress
    delete having happened. Folding that witness into a report would leave the
    single mechanism designed to catch a destructive clear whose progress delete
    was missed present and deliberately not acted on, so it restarts, and
    restarting on it costs essentially nothing.

    Everything that does restart is a restart rather than a failure. Restarting
    is always sound — every apply is idempotent on its natural key — so the cost
    of being wrong in that direction is repeated work, while the cost of
    resuming over a cleared cache is a certificate claiming coverage the cache
    does not have.
    """
    if stored is None:
        return RESTART, REASON_ABSENT, False
    if not isinstance(stored, dict):
        return RESTART, REASON_MALFORMED, False
    try:
        stored_pass = str(stored["passId"])
        stored_started = int(stored["startedAt"])
        int(stored["chunks"])
        stored_root = str(stored["identityRoot"])
        stored_seq = int(stored["physicalMutationSeq"])
        stored_digest = str(stored["sourceRootsDigest"])
    except (KeyError, TypeError, ValueError):
        return RESTART, REASON_MALFORMED, False
    if stored_pass != str(pass_id):
        if stored_started > int(started_at):
            if stored_started - int(started_at) > PROGRESS_YIELD_MAX_SKEW_US:
                return RESTART, REASON_ORPHANED_PASS, False
            return YIELD, "newerPass", True
        # A dead pass's leftover record is not a concurrent writer, and
        # reporting one would put a writer that does not exist on the rebuild
        # record.
        return RESTART, "foreignPass", False
    concurrent = stored_seq != int(physical_seq)
    if stored_digest != str(source_roots_digest):
        return RESTART, "sourceRootsDigest", concurrent
    if stored_root != str(identity_root):
        return RESTART, REASON_IDENTITY_ROOT, concurrent
    return RESUME, REASON_OK, concurrent


def progress_supersedes(stored, candidate) -> bool:
    """Whether ``candidate`` may replace ``stored`` under the monotonic CAS.

    An older worker cannot overwrite a newer pass's progress, and a pass cannot
    move its own progress backwards.
    """
    if stored is None:
        return True
    try:
        stored_pass = str(stored["passId"])
        stored_started = int(stored["startedAt"])
        stored_chunks = int(stored["chunks"])
        candidate_pass = str(candidate["passId"])
        candidate_started = int(candidate["startedAt"])
        candidate_chunks = int(candidate["chunks"])
    except (KeyError, TypeError, ValueError):
        return True
    if stored_pass == candidate_pass:
        return candidate_chunks > stored_chunks
    return candidate_started > stored_started


def chunk_spans(sizes, *, byte_cap, record_cap):
    """``[(start, stop, encoded_bytes), ...]`` over ``sizes``, capped BOTH ways.

    Capping by records alone lets one chunk of large observations blow the
    memory bound, and capping by bytes alone lets a chunk of tiny ones carry far
    more rows than one transaction should. A single record larger than the byte
    cap still gets its own chunk rather than none — refusing it would stall the
    pass on a record it is required to apply.
    """
    spans = []
    start = 0
    total = 0
    for index, size in enumerate(sizes):
        size = int(size)
        if index > start and (
            total + size > int(byte_cap) or index - start >= int(record_cap)
        ):
            spans.append((start, index, total))
            start = index
            total = 0
        total += size
    if start < len(sizes):
        spans.append((start, len(sizes), total))
    return spans
