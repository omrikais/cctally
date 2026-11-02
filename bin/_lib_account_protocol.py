"""Pure account-bound protocol kernel (#825/#832, spec sections 5.1-5.4).

One durable journal and one stats index are fenced by a durable
``account_bound_cutover`` op and a per-record writer-generation stamp. This
module owns:

* the protocol constants (writer generation, op kinds, the cutover op id);
* the stamp the shared append preparation applies to every record the current
  binary writes;
* the historical boundary the cutover op records, and its payload codec;
* the admission predicate every accounting reader applies BEFORE it interprets
  a record (ingest, rebuild, rederive, journal-repair, correction selection,
  cache recovery and the deep doctor scans).

"After the cutover" is a PHYSICAL journal position (segment and byte offset of
a complete line), never ``at``, capture time or provider time. A record is
historical when it lies inside the frozen complete-line extent the cutover op
recorded for its segment; a segment absent from that map (a later month, or a
bootstrap segment created after the cutover, which sorts FIRST in canonical
order) is post-boundary in its entirety.

The fence is Claude-scoped (operator Q5). Exclusion is the default: a
post-boundary record without the current writer generation is admitted only
when an explicit field its real producer writes proves it Codex-only. An
unknown or unclassifiable record is excluded, never admitted by omission.

#797/#823/#840 (spec section 4.6, B1-B3): revision 2 of the fence. ``wpg``
keeps its value (the previous release's exact-equality ``is_stamped`` must
keep admitting what the current binary writes), and a second reserved
top-level field ``wpr`` carries the integer protocol revision. A durable
``account_bound_revision`` op records a second physical boundary with the same
codec; after it, a Claude-scoped record without ``wpr == 2`` is excluded
exactly as an unstamped record is after the original cutover. The revision
boundary never re-admits a record the original rule excludes.

#1019/#1014 (spec section 4.7, C1-C8): revision 3 of the fence. ``wpg`` and
``wpr`` keep their values (1.116.1's exact-equality checks must keep
admitting what the current binary writes), and a third reserved top-level
field ``wpx`` carries the successor revision. A durable
``account_bound_revision_v3`` op records a third physical boundary with the
same codec plus the revision-2 op's coordinate; after it, a Claude-scoped
record admitted by its stamps is excluded unless it also carries
``wpx == 3`` (``revision-3-unstamped``). No boundary re-admits what an
earlier rule excludes, and the Codex-only exemptions are unchanged.

Pure: no I/O, no clock, no SQLite.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from typing import Mapping


PROTOCOL_GENERATION = "account-bound-v1"
WRITER_GENERATION_KEY = "wpg"
CUTOVER_OP_KIND = "account_bound_cutover"
CUTOVER_OP_ID = "account-bound-cutover-v1"
LEGACY_SPOOL_ACTIVITY_KIND = "legacy_spool_activity"
#: #797/#823/#840 (spec 4.6 B1, B2): the protocol revision stamp and the
#: durable op that records where revision 2 begins.
REVISION_KEY = "wpr"
PROTOCOL_REVISION = 2
REVISION_OP_KIND = "account_bound_revision"
REVISION_OP_ID = "account-bound-revision-2"
#: #1019/#1014 (spec 4.7 C1, C2): the successor-revision stamp and the
#: durable op that records where revision 3 begins.
SUCCESSOR_REVISION_KEY = "wpx"
SUCCESSOR_REVISION = 3
REVISION3_OP_KIND = "account_bound_revision_v3"
REVISION3_OP_ID = "account-bound-revision-3"
#: Spec revision 5 (section 7, operator decision on Codex design-fix-round-5
#: Q4): the `legacy_spool_activity` reason of an incomplete inventory. Such an
#: op records typed failure evidence and no digest, so it anchors no baseline.
INVENTORY_INCOMPLETE_REASON = "inventory-incomplete"
#: Fix round K12 (K11R-4; spec 5.5 step 6, 5.6 item 1(b) and section 7): the
#: reason of the anchoring op the first complete inventory appends after a
#: cutover that recorded inventory uncertainty and so anchored no baseline.
#: It records the complete digest and ends that episode. Like every
#: `legacy_spool_activity` op it is a trigger, but of the same uncertainty,
#: never an observed legacy change, and it clears nothing (residual 6).
INVENTORY_COMPLETE_REASON = "inventory-complete"
#: The reasons whose unresolved markers an incomplete inventory left.
INVENTORY_REASONS = (INVENTORY_INCOMPLETE_REASON, INVENTORY_COMPLETE_REASON)
#: #948 spec 6.2: the three non-trigger recovery ops of the outcome
#: quarantine. An inventory-episode completion (a complete scan found the
#: spool unchanged while the journal shows the episode open), an OAuth
#: authority completion (appended after its reconciliation committed) and a
#: per-axis release (appended right after its witness). The previous release
#: ignores an unknown op kind.
INVENTORY_COMPLETION_OP_KIND = "legacy_spool_inventory_completion"
AUTHORITY_COMPLETION_OP_KIND = "outcome_authority_completion"
AXIS_RELEASE_OP_KIND = "outcome_axis_release"
RECOVERY_OP_KINDS = (INVENTORY_COMPLETION_OP_KIND,
                     AUTHORITY_COMPLETION_OP_KIND, AXIS_RELEASE_OP_KIND)
#: The operations an inventory can fail at (the evidence's ``operation``).
INVENTORY_OPERATIONS = ("list", "stat", "read")

#: The versioned successors of the two administrative op kinds whose
#: previous-release validators require exact keys (spec 5.1). The previous
#: release ignores an unknown op kind, so the current binary emits only these.
PROTOCOL_RESOLUTION_V2_KIND = "journal_protocol_resolution_v2"
#: #797/#823/#840 (spec 4.6 B5): the previous release's strict validator of
#: the v2 acknowledgement rejects the extra ``wpr`` field, so the current
#: binary emits repair acknowledgements under this successor kind, which the
#: previous release ignores as an unknown op.
PROTOCOL_RESOLUTION_V3_KIND = "journal_protocol_resolution_v3"
#: #1019/#1014 (spec 4.7 C5): 1.116.1's strict validator of the v3
#: acknowledgement has an exact key set and an identity core without
#: ``wpx``, so the current binary emits repair acknowledgements under this
#: successor kind, which 1.116.1 ignores as an unknown op.
PROTOCOL_RESOLUTION_V4_KIND = "journal_protocol_resolution_v4"
WEEKLY_OBSERVATION_DECISION_V2_KIND = "claude_weekly_observation_decision_v2"

#: Admission families. ``codex`` is the only family admitted without a stamp.
FAMILIES = (
    "claude-observation",
    "claude-op",
    "claude-event",
    "administrative",
    "protocol",
    "codex",
    "unknown",
)

ADMISSION_REASONS = ("no-cutover", "historical", "stamped", "codex-only",
                     "excluded", "revision-unstamped",
                     "revision-3-unstamped")
#: The reasons that exclude a record.
EXCLUDED_REASONS = frozenset(("excluded", "revision-unstamped",
                              "revision-3-unstamped"))

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_CLAUDE_OP_KINDS = frozenset((
    "account_observe", "account_label", "accounts_cutover",
    "weekly_credit_floor", "sync_week",
    # #944 spec 5.10: a Claude cost attribution decision is Claude-scoped
    # accounting truth, so an unstamped post-boundary one is excluded.
    "claude_cost_account",
))
_CODEX_OP_KINDS = frozenset((
    "codex_file_account", "codex_window_attribution",
    "codex_window_attribution_retract",
))
_ADMIN_OP_KINDS = frozenset((
    "journal_protocol_resolution", "claude_weekly_observation_decision",
    PROTOCOL_RESOLUTION_V2_KIND, PROTOCOL_RESOLUTION_V3_KIND,
    PROTOCOL_RESOLUTION_V4_KIND, WEEKLY_OBSERVATION_DECISION_V2_KIND,
))
_PROTOCOL_OP_KINDS = frozenset((CUTOVER_OP_KIND, LEGACY_SPOOL_ACTIVITY_KIND,
                                REVISION_OP_KIND, REVISION3_OP_KIND,
                                *RECOVERY_OP_KINDS))
_ADMIN_RECORD_TYPES = frozenset(("correction", "correction_batch"))
_CLAUDE_EVT_KINDS = frozenset((
    "snapshot_accept", "weekly_cost_snapshot", "week_reset", "five_hour_credit",
    "five_hour_block_close", "percent_milestone", "five_hour_milestone",
    "weekly_credit_effects", "projected", "project_budget", "meter_rate_change",
    "meter_rate_change_supersession",
))
#: Evt kinds admitted without a stamp only when their payload proves a Codex
#: quota source (``payload.source == "codex"``, the source every Codex quota
#: identity carries and every Codex quota table filters on).
_QUOTA_EVT_KINDS = frozenset(("quota_alert_arming", "quota_threshold_event"))


@dataclasses.dataclass(frozen=True)
class Coordinate:
    """Physical journal position of a complete line: its segment name and the
    byte offset at which the line STARTS."""

    segment: str
    offset: int


@dataclasses.dataclass(frozen=True)
class CutoverBoundary:
    """The historical boundary recorded by the ``account_bound_cutover`` op.

    ``segment``/``end_offset`` name where the op itself was appended: the
    target segment and its complete-line end at that moment, which is the op's
    own start offset. ``frozen_extents`` maps every
    segment that existed before the op to its complete-line byte length at that
    moment; only bytes inside those extents are historical.

    Spec revision 5: when the establishment could not complete its legacy
    spool inventory, the op records the uncertainty itself —
    ``legacy_spool_inventory_failure`` holds the typed failure evidence and
    ``legacy_spool_baseline_digest`` is None, so the op anchors no baseline
    (the next complete inventory installs one under its own anchoring op).
    """

    segment: str
    end_offset: int
    frozen_extents: Mapping[str, int]
    legacy_spool_baseline_digest: "str | None"
    legacy_spool_inventory_failure: "Mapping[str, object] | None" = None
    #: #797/#823/#840 (spec 4.6 B3): the revision-2 boundary a lookup found
    #: after this cutover, carried beside it so every admission consumer
    #: applies both fences through the one boundary object it already
    #: threads. Not part of the cutover op's codec, and not part of equality:
    #: two lookups of one cutover op name the same original boundary, and
    #: the admission summary records the revision separately.
    revision: "RevisionBoundary | None" = dataclasses.field(
        default=None, compare=False)
    #: #1019/#1014 (spec 4.7 C3): the revision-3 boundary a lookup found
    #: after ``revision``, carried the same way and for the same reason.
    revision3: "Revision3Boundary | None" = dataclasses.field(
        default=None, compare=False)


@dataclasses.dataclass(frozen=True)
class RevisionBoundary:
    """The revision-2 boundary recorded by the ``account_bound_revision`` op
    (spec 4.6 B2): the same physical-position codec as `CutoverBoundary`,
    plus the original cutover it extends (``cutover_segment`` and
    ``cutover_offset``, the coordinate of the cutover op).

    ``legacy_spool_baseline_digest`` is the EXPANDED baseline digest — the
    pre-#825 legacy paths and the retired v1 directory's coordination state
    (spec 4.6 B4) — or None when the establishment could not complete that
    inventory and records ``legacy_spool_inventory_failure`` instead, exactly
    as a cutover does (#825 spec revision 5).

    ``continued_inventory_episode`` is the coordinate of the uncertainty
    record whose failure episode was still open when an incomplete revision
    inventory was recorded: the failure belongs to that episode, which
    already has its trigger, so the revision op raises none of its own (one
    trigger per failure episode, #825 spec revision 5). None otherwise."""

    segment: str
    end_offset: int
    frozen_extents: Mapping[str, int]
    legacy_spool_baseline_digest: "str | None"
    cutover_segment: str
    cutover_offset: int
    legacy_spool_inventory_failure: "Mapping[str, object] | None" = None
    continued_inventory_episode: "tuple[str, int] | None" = None


@dataclasses.dataclass(frozen=True)
class Revision3Boundary:
    """The revision-3 boundary recorded by the ``account_bound_revision_v3``
    op (#1019/#1014 spec 4.7 C2): the `RevisionBoundary` fields plus the
    revision-2 op it extends (``revision2_segment`` and ``revision2_offset``,
    the coordinate of that op).

    ``legacy_spool_baseline_digest`` is the baseline digest of the inventory
    that also covers the retired v2 directory's coordination state (C4), or
    None with ``legacy_spool_inventory_failure`` when that inventory could
    not complete; ``continued_inventory_episode`` as for revision 2."""

    segment: str
    end_offset: int
    frozen_extents: Mapping[str, int]
    legacy_spool_baseline_digest: "str | None"
    cutover_segment: str
    cutover_offset: int
    revision2_segment: str
    revision2_offset: int
    legacy_spool_inventory_failure: "Mapping[str, object] | None" = None
    continued_inventory_episode: "tuple[str, int] | None" = None


@dataclasses.dataclass(frozen=True)
class Admission:
    admitted: bool
    family: str
    reason: str


# --------------------------------------------------------------------------
# writer-generation stamp
# --------------------------------------------------------------------------

def stamp(record: dict) -> dict:
    """Return a shallow copy of ``record`` carrying the current writer
    generation, protocol revision and successor revision (spec 4.6 B1;
    #1019/#1014 spec 4.7 C1). Idempotent; refuses a record that already
    names a DIFFERENT value of any of them (a caller must never relabel
    another protocol's record). The payload is never touched: the stamps
    are transport metadata."""
    if not isinstance(record, dict):
        raise ValueError("journal record must be a dict")
    existing = record.get(WRITER_GENERATION_KEY)
    if existing is not None and existing != PROTOCOL_GENERATION:
        raise ValueError(
            f"record already carries writer generation {existing!r}")
    if REVISION_KEY in record and not has_current_revision(record):
        raise ValueError(
            f"record already carries protocol revision "
            f"{record[REVISION_KEY]!r}")
    if SUCCESSOR_REVISION_KEY in record and not has_successor_revision(record):
        raise ValueError(
            f"record already carries successor revision "
            f"{record[SUCCESSOR_REVISION_KEY]!r}")
    out = dict(record)
    out[WRITER_GENERATION_KEY] = PROTOCOL_GENERATION
    out[REVISION_KEY] = PROTOCOL_REVISION
    out[SUCCESSOR_REVISION_KEY] = SUCCESSOR_REVISION
    return out


def is_stamped(record: dict) -> bool:
    return (isinstance(record, dict)
            and record.get(WRITER_GENERATION_KEY) == PROTOCOL_GENERATION)


def has_current_revision(record: dict) -> bool:
    """Whether ``record`` carries exactly protocol revision 2 (spec 4.6 B1).
    Only an ``int`` equal to 2: never ``True``, ``"2"``, ``2.0`` or a later
    revision. A future revision is admitted only through an explicit
    compatibility rule added here, never an open-ended ``>= 2``."""
    if not isinstance(record, dict):
        return False
    value = record.get(REVISION_KEY)
    return type(value) is int and value == PROTOCOL_REVISION


def has_successor_revision(record: dict) -> bool:
    """Whether ``record`` carries exactly successor revision 3 (#1019/#1014
    spec 4.7 C1), the writer-authority check after the revision-3 boundary.
    Only an ``int`` equal to 3: never ``True``, ``"3"``, ``3.0`` or a later
    revision."""
    if not isinstance(record, dict):
        return False
    value = record.get(SUCCESSOR_REVISION_KEY)
    return type(value) is int and value == SUCCESSOR_REVISION


_STAMP_KEYS = (WRITER_GENERATION_KEY, REVISION_KEY, SUCCESSOR_REVISION_KEY)


def strip_stamp(record: dict) -> dict:
    """``record`` without its writer-generation, protocol-revision and
    successor-revision fields — the comparison shape for semantic event
    duplicate/conflict classification. Comparison only: a content id is
    computed over the stamped core."""
    if not isinstance(record, dict) or not any(
            key in record for key in _STAMP_KEYS):
        return record
    return {k: v for k, v in record.items() if k not in _STAMP_KEYS}


# --------------------------------------------------------------------------
# boundary
# --------------------------------------------------------------------------

def is_historical(coord: Coordinate, boundary: CutoverBoundary) -> bool:
    extent = boundary.frozen_extents.get(coord.segment)
    return extent is not None and coord.offset < extent


def inventory_failure_evidence(*, path: str, operation: str,
                               error: str, message: str = "") -> dict:
    """The typed evidence of a failed legacy spool inventory (spec revision
    5): the path, the failed operation and the error (its errno name). It is
    diagnostic evidence only, never accounting input."""
    evidence = {"path": str(path), "operation": str(operation),
                "error": str(error)}
    if message:
        evidence["message"] = str(message)
    if not is_inventory_failure(evidence):
        raise ValueError(f"invalid inventory failure evidence {evidence!r}")
    return evidence


def is_inventory_failure(value) -> bool:
    """Whether ``value`` is well-formed inventory failure evidence."""
    if not isinstance(value, dict):
        return False
    if not (isinstance(value.get("path"), str)
            and value.get("operation") in INVENTORY_OPERATIONS
            and isinstance(value.get("error"), str) and value["error"]):
        return False
    message = value.get("message")
    return message is None or isinstance(message, str)


def boundary_payload(boundary: CutoverBoundary) -> dict:
    """The camelCase payload fields of the cutover op (without ``kind``).
    ``legacySpoolInventoryFailure`` is present only on a cutover that
    recorded inventory uncertainty, so a complete cutover's bytes are
    unchanged."""
    payload = {
        "segment": boundary.segment,
        "endOffset": boundary.end_offset,
        "frozenExtents": {k: boundary.frozen_extents[k]
                          for k in sorted(boundary.frozen_extents)},
        "legacySpoolBaselineDigest": boundary.legacy_spool_baseline_digest,
    }
    if boundary.legacy_spool_inventory_failure is not None:
        payload["legacySpoolInventoryFailure"] = dict(
            boundary.legacy_spool_inventory_failure)
    return payload


def _nonneg_int(value) -> bool:
    return type(value) is int and value >= 0


def boundary_from_op(op_record: dict) -> CutoverBoundary:
    """Decode the boundary from a cutover op. Raises ``ValueError`` on any
    shape failure: a malformed cutover op never yields a boundary."""
    if not isinstance(op_record, dict) or op_record.get("t") != "op":
        raise ValueError("cutover boundary must come from an op record")
    payload = op_record.get("payload")
    if not isinstance(payload, dict) or payload.get("kind") != CUTOVER_OP_KIND:
        raise ValueError("record is not an account_bound_cutover op")
    segment = payload.get("segment")
    end_offset = payload.get("endOffset")
    extents = payload.get("frozenExtents")
    digest = payload.get("legacySpoolBaselineDigest")
    if not isinstance(segment, str) or not segment:
        raise ValueError("cutover boundary segment is invalid")
    if not _nonneg_int(end_offset):
        raise ValueError("cutover boundary endOffset is invalid")
    if not isinstance(extents, dict) or not all(
            isinstance(k, str) and k and _nonneg_int(v)
            for k, v in extents.items()):
        raise ValueError("cutover boundary frozenExtents is invalid")
    failure = payload.get("legacySpoolInventoryFailure")
    if failure is None:
        if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
            raise ValueError("cutover boundary legacy spool digest is invalid")
    else:
        # Revision 5: an uncertain inventory records its evidence and NO
        # digest; a digest beside failure evidence is malformed.
        if not is_inventory_failure(failure) or digest is not None:
            raise ValueError(
                "cutover boundary legacy spool inventory failure is invalid")
        failure = dict(failure)
    return CutoverBoundary(
        segment=segment,
        end_offset=end_offset,
        frozen_extents=dict(extents),
        legacy_spool_baseline_digest=digest,
        legacy_spool_inventory_failure=failure,
    )


def with_revision(boundary: "CutoverBoundary | None",
                  revision: "RevisionBoundary | None"):
    """``boundary`` carrying ``revision`` (spec 4.6 B3), or None when there is
    no cutover. A revision that names a different cutover is malformed and
    never attached."""
    if boundary is None:
        return None
    if revision is not None and (
            (revision.cutover_segment, revision.cutover_offset)
            != (boundary.segment, boundary.end_offset)):
        raise ValueError("revision boundary names a different cutover")
    revision3 = boundary.revision3
    if revision3 is not None and (
            revision is None
            or (revision3.revision2_segment, revision3.revision2_offset)
            != (revision.segment, revision.end_offset)):
        # A revision-3 boundary never outlives the revision-2 boundary it
        # extends.
        revision3 = None
    return dataclasses.replace(boundary, revision=revision,
                               revision3=revision3)


def with_revision3(boundary: "CutoverBoundary | None",
                   revision3: "Revision3Boundary | None"):
    """``boundary`` carrying ``revision3`` (#1019/#1014 spec 4.7 C3), or None
    when there is no cutover. A revision 3 that names a different cutover or
    revision-2 op (or arrives without a revision 2) is malformed and never
    attached."""
    if boundary is None:
        return None
    if revision3 is not None:
        revision = boundary.revision
        if ((revision3.cutover_segment, revision3.cutover_offset)
                != (boundary.segment, boundary.end_offset)):
            raise ValueError("revision-3 boundary names a different cutover")
        if revision is None or (
                (revision3.revision2_segment, revision3.revision2_offset)
                != (revision.segment, revision.end_offset)):
            raise ValueError(
                "revision-3 boundary names a different revision-2 op")
    return dataclasses.replace(boundary, revision3=revision3)


def original_boundary(boundary: "CutoverBoundary | None"):
    """``boundary`` with neither revision attached: the cutover op's own
    boundary."""
    if boundary is None:
        return None
    return dataclasses.replace(boundary, revision=None, revision3=None)


def revision_boundary_payload(boundary: RevisionBoundary) -> dict:
    """The camelCase payload fields of the revision op (without ``kind``):
    the `boundary_payload` codec, the revision number and the original
    cutover's coordinate."""
    payload = {
        "revision": PROTOCOL_REVISION,
        "cutover": {"segment": boundary.cutover_segment,
                    "offset": boundary.cutover_offset},
        "segment": boundary.segment,
        "endOffset": boundary.end_offset,
        "frozenExtents": {k: boundary.frozen_extents[k]
                          for k in sorted(boundary.frozen_extents)},
        "legacySpoolBaselineDigest": boundary.legacy_spool_baseline_digest,
    }
    if boundary.legacy_spool_inventory_failure is not None:
        payload["legacySpoolInventoryFailure"] = dict(
            boundary.legacy_spool_inventory_failure)
        if boundary.continued_inventory_episode is not None:
            segment, offset = boundary.continued_inventory_episode
            payload["inventoryEpisode"] = {"segment": segment,
                                           "offset": offset}
    return payload


def revision_boundary_from_op(op_record: dict) -> RevisionBoundary:
    """Decode the revision boundary from an ``account_bound_revision`` op.
    Raises ``ValueError`` on any shape failure, including a revision other
    than exactly 2 and an op that does not itself carry ``wpr == 2``."""
    if not isinstance(op_record, dict) or op_record.get("t") != "op":
        raise ValueError("revision boundary must come from an op record")
    payload = op_record.get("payload")
    if not isinstance(payload, dict) or payload.get("kind") != REVISION_OP_KIND:
        raise ValueError("record is not an account_bound_revision op")
    if type(payload.get("revision")) is not int or (
            payload["revision"] != PROTOCOL_REVISION):
        raise ValueError("revision boundary revision is invalid")
    cutover = payload.get("cutover")
    if (not isinstance(cutover, dict)
            or set(cutover) != {"segment", "offset"}
            or not isinstance(cutover.get("segment"), str)
            or not cutover["segment"]
            or not _nonneg_int(cutover.get("offset"))):
        raise ValueError("revision boundary cutover coordinate is invalid")
    episode = payload.get("inventoryEpisode")
    if episode is not None:
        if (payload.get("legacySpoolInventoryFailure") is None
                or not isinstance(episode, dict)
                or set(episode) != {"segment", "offset"}
                or not isinstance(episode.get("segment"), str)
                or not episode["segment"]
                or not _nonneg_int(episode.get("offset"))):
            raise ValueError("revision boundary inventory episode is invalid")
        episode = (episode["segment"], episode["offset"])
    base = boundary_from_op({
        "t": "op",
        "payload": {**{k: v for k, v in payload.items()
                       if k not in ("revision", "cutover",
                                    "inventoryEpisode")},
                    "kind": CUTOVER_OP_KIND},
    })
    return RevisionBoundary(
        segment=base.segment,
        end_offset=base.end_offset,
        frozen_extents=base.frozen_extents,
        legacy_spool_baseline_digest=base.legacy_spool_baseline_digest,
        cutover_segment=cutover["segment"],
        cutover_offset=cutover["offset"],
        legacy_spool_inventory_failure=base.legacy_spool_inventory_failure,
        continued_inventory_episode=episode,
    )


def revision3_boundary_payload(boundary: Revision3Boundary) -> dict:
    """The camelCase payload fields of the revision-3 op (without ``kind``):
    the revision-2 codec with ``revision: 3`` and the revision-2 op's
    coordinate beside the cutover's."""
    payload = {
        "revision": SUCCESSOR_REVISION,
        "cutover": {"segment": boundary.cutover_segment,
                    "offset": boundary.cutover_offset},
        "revision2": {"segment": boundary.revision2_segment,
                      "offset": boundary.revision2_offset},
        "segment": boundary.segment,
        "endOffset": boundary.end_offset,
        "frozenExtents": {k: boundary.frozen_extents[k]
                          for k in sorted(boundary.frozen_extents)},
        "legacySpoolBaselineDigest": boundary.legacy_spool_baseline_digest,
    }
    if boundary.legacy_spool_inventory_failure is not None:
        payload["legacySpoolInventoryFailure"] = dict(
            boundary.legacy_spool_inventory_failure)
        if boundary.continued_inventory_episode is not None:
            segment, offset = boundary.continued_inventory_episode
            payload["inventoryEpisode"] = {"segment": segment,
                                           "offset": offset}
    return payload


def _coordinate_member(value) -> bool:
    return (isinstance(value, dict) and set(value) == {"segment", "offset"}
            and isinstance(value.get("segment"), str) and value["segment"]
            and _nonneg_int(value.get("offset")))


def revision3_boundary_from_op(op_record: dict) -> Revision3Boundary:
    """Decode the revision-3 boundary from an ``account_bound_revision_v3``
    op. Raises ``ValueError`` on any shape failure, including a revision
    other than exactly 3. Whether the op itself carries ``wpx == 3`` is the
    caller's check (as `revision_boundary_from_op` leaves ``wpr``)."""
    if not isinstance(op_record, dict) or op_record.get("t") != "op":
        raise ValueError("revision-3 boundary must come from an op record")
    payload = op_record.get("payload")
    if (not isinstance(payload, dict)
            or payload.get("kind") != REVISION3_OP_KIND):
        raise ValueError("record is not an account_bound_revision_v3 op")
    if type(payload.get("revision")) is not int or (
            payload["revision"] != SUCCESSOR_REVISION):
        raise ValueError("revision-3 boundary revision is invalid")
    revision2 = payload.get("revision2")
    if not _coordinate_member(revision2):
        raise ValueError("revision-3 boundary revision-2 coordinate is invalid")
    base = revision_boundary_from_op({
        "t": "op",
        "payload": {**{k: v for k, v in payload.items()
                       if k != "revision2"},
                    "kind": REVISION_OP_KIND,
                    "revision": PROTOCOL_REVISION},
    })
    return Revision3Boundary(
        segment=base.segment,
        end_offset=base.end_offset,
        frozen_extents=base.frozen_extents,
        legacy_spool_baseline_digest=base.legacy_spool_baseline_digest,
        cutover_segment=base.cutover_segment,
        cutover_offset=base.cutover_offset,
        revision2_segment=revision2["segment"],
        revision2_offset=revision2["offset"],
        legacy_spool_inventory_failure=base.legacy_spool_inventory_failure,
        continued_inventory_episode=base.continued_inventory_episode,
    )


# --------------------------------------------------------------------------
# admission
# --------------------------------------------------------------------------

def _family(record: dict) -> tuple[str, bool]:
    """``(family, codex_only)`` from an explicit allow-list per kind. Anything
    not named here is ``("unknown", False)``."""
    if not isinstance(record, dict):
        return ("unknown", False)
    t = record.get("t")
    if t in _ADMIN_RECORD_TYPES:
        return ("administrative", False)
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ("unknown", False)
    kind = payload.get("kind")
    if t == "obs":
        provider = record.get("provider")
        if provider == "claude":
            return ("claude-observation", False)
        if provider == "codex" and kind == "quota_window_snapshot":
            return ("codex", True)
        return ("unknown", False)
    if t == "op":
        if kind in _CODEX_OP_KINDS:
            return ("codex", True)
        if kind in ("account_observe", "account_label"):
            if payload.get("provider") == "codex":
                return ("codex", True)
            return ("claude-op", False)
        if kind in _CLAUDE_OP_KINDS:
            return ("claude-op", False)
        if kind in _ADMIN_OP_KINDS:
            return ("administrative", False)
        if kind in _PROTOCOL_OP_KINDS:
            return ("protocol", False)
        return ("unknown", False)
    if t == "evt":
        if kind in _CLAUDE_EVT_KINDS:
            return ("claude-event", False)
        if kind == "budget":
            if payload.get("vendor") == "codex":
                return ("codex", True)
            return ("claude-event", False)
        if kind in _QUOTA_EVT_KINDS:
            if payload.get("source") == "codex":
                return ("codex", True)
            return ("claude-event", False)
        return ("unknown", False)
    return ("unknown", False)


def record_family(record: dict) -> str:
    return _family(record)[0]


def classify_admission(record: dict, coord: Coordinate,
                       boundary: CutoverBoundary | None,
                       revision_boundary: "RevisionBoundary | None" = None,
                       revision3_boundary: "Revision3Boundary | None" = None,
                       ) -> Admission:
    """The single admission predicate (spec 5.3; #797/#823/#840 spec 4.6 B3).

    The original rule: admitted when there is no cutover yet, when the
    record lies inside the historical boundary, when it carries the current
    writer generation, or when its family is proven Codex-only; every other
    post-boundary record is excluded.

    Composed with revision 2: ``revision_boundary`` (default: the revision a
    lookup attached to ``boundary``) excludes, after its own frozen extents,
    a record the original rule admitted only by its writer generation unless
    it also carries ``wpr == 2`` (``revision-unstamped``). The revision never
    re-admits what the original rule excludes, and the Codex-only exemptions
    are unchanged.

    Composed with revision 3 (#1019/#1014 spec 4.7 C3): ``revision3_boundary``
    (default: ``boundary.revision3``) excludes, after its own frozen extents,
    a record the earlier rules admitted only by its stamps unless it also
    carries ``wpx == 3`` (``revision-3-unstamped``)."""
    family, codex_only = _family(record)
    if boundary is None:
        return Admission(True, family, "no-cutover")
    if is_historical(coord, boundary):
        return Admission(True, family, "historical")
    if is_stamped(record):
        revision = (revision_boundary if revision_boundary is not None
                    else boundary.revision)
        if not (revision is None or is_historical(coord, revision)
                or has_current_revision(record)):
            if codex_only:
                return Admission(True, family, "codex-only")
            return Admission(False, family, "revision-unstamped")
        revision3 = (revision3_boundary if revision3_boundary is not None
                     else boundary.revision3)
        if (revision3 is None or is_historical(coord, revision3)
                or has_successor_revision(record)):
            return Admission(True, family, "stamped")
        if codex_only:
            return Admission(True, family, "codex-only")
        return Admission(False, family, "revision-3-unstamped")
    if codex_only:
        return Admission(True, family, "codex-only")
    return Admission(False, family, "excluded")


# --------------------------------------------------------------------------
# excluded-population digest (spec section 8)
# --------------------------------------------------------------------------

_DIGEST_MODULUS = 1 << 256


def _coordinate_term(coord: Coordinate) -> int:
    data = f"{coord.segment}\x00{coord.offset}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(data).digest(), "big")


def coordinate_population_digest(coords, base: str | None = None) -> str:
    """An ORDER-INDEPENDENT digest over a set of physical coordinates: the sum
    of each coordinate's sha256 modulo 2**256. ``base`` extends an earlier
    digest incrementally, so an ingest cycle can add the coordinates it newly
    excludes and a full scan reproduces the same value in any order."""
    total = 0
    if base is not None:
        if not isinstance(base, str) or not _DIGEST_RE.match(base):
            raise ValueError("coordinate population digest is invalid")
        total = int(base[len("sha256:"):], 16)
    for coord in coords:
        total = (total + _coordinate_term(coord)) % _DIGEST_MODULUS
    return "sha256:" + format(total, "064x")


#: The record type of an inert positional placeholder for consumers that need
#: a dict in an excluded record's slot. No reader recognises the type, so the
#: selector, the pipeline and the legacy classifier all skip it while it still
#: consumes the slot's sequence number.
EXCLUDED_PLACEHOLDER_TYPE = "excluded"


def excluded_placeholder() -> dict:
    return {"t": EXCLUDED_PLACEHOLDER_TYPE}
