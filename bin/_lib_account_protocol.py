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
#: The operations an inventory can fail at (the evidence's ``operation``).
INVENTORY_OPERATIONS = ("list", "stat", "read")

#: The versioned successors of the two administrative op kinds whose
#: previous-release validators require exact keys (spec 5.1). The previous
#: release ignores an unknown op kind, so the current binary emits only these.
PROTOCOL_RESOLUTION_V2_KIND = "journal_protocol_resolution_v2"
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
                     "excluded")

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_CLAUDE_OP_KINDS = frozenset((
    "account_observe", "account_label", "accounts_cutover",
    "weekly_credit_floor", "sync_week",
))
_CODEX_OP_KINDS = frozenset((
    "codex_file_account", "codex_window_attribution",
    "codex_window_attribution_retract",
))
_ADMIN_OP_KINDS = frozenset((
    "journal_protocol_resolution", "claude_weekly_observation_decision",
    PROTOCOL_RESOLUTION_V2_KIND, WEEKLY_OBSERVATION_DECISION_V2_KIND,
))
_PROTOCOL_OP_KINDS = frozenset((CUTOVER_OP_KIND, LEGACY_SPOOL_ACTIVITY_KIND))
_ADMIN_RECORD_TYPES = frozenset(("correction", "correction_batch"))
_CLAUDE_EVT_KINDS = frozenset((
    "snapshot_accept", "weekly_cost_snapshot", "week_reset", "five_hour_credit",
    "five_hour_block_close", "percent_milestone", "five_hour_milestone",
    "weekly_credit_effects", "projected", "project_budget", "meter_rate_change",
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
    generation. Idempotent; refuses a record that already names a DIFFERENT
    generation (a caller must never relabel another protocol's record). The
    payload is never touched: the stamp is transport metadata."""
    if not isinstance(record, dict):
        raise ValueError("journal record must be a dict")
    existing = record.get(WRITER_GENERATION_KEY)
    if existing is not None and existing != PROTOCOL_GENERATION:
        raise ValueError(
            f"record already carries writer generation {existing!r}")
    out = dict(record)
    out[WRITER_GENERATION_KEY] = PROTOCOL_GENERATION
    return out


def is_stamped(record: dict) -> bool:
    return (isinstance(record, dict)
            and record.get(WRITER_GENERATION_KEY) == PROTOCOL_GENERATION)


def strip_stamp(record: dict) -> dict:
    """``record`` without its writer-generation field — the comparison shape
    for semantic event duplicate/conflict classification."""
    if not isinstance(record, dict) or WRITER_GENERATION_KEY not in record:
        return record
    return {k: v for k, v in record.items() if k != WRITER_GENERATION_KEY}


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
                       boundary: CutoverBoundary | None) -> Admission:
    """The single admission predicate (spec 5.3).

    Admitted when there is no cutover yet, when the record lies inside the
    historical boundary, when it carries the current writer generation, or when
    its family is proven Codex-only. Every other post-boundary record is
    excluded."""
    family, codex_only = _family(record)
    if boundary is None:
        return Admission(True, family, "no-cutover")
    if is_historical(coord, boundary):
        return Admission(True, family, "historical")
    if is_stamped(record):
        return Admission(True, family, "stamped")
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
