"""Pure candidate-arbitration schemas and reducer primitives for #318.

This module deliberately has no filesystem, database, clock, process, or
``cctally`` namespace dependency.  Callers supply all mutable state and time.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from typing import Callable, Mapping


#: Candidate and tombstone documents.  Every session on this machine writes
#: candidates, so this version may only move when peer sessions running the
#: previous binary can still be read.
SCHEMA_VERSION = 1
#: The control document, versioned independently (#755).  It is written by
#: whichever session holds the persist lock and read by the next tick, so a
#: shape change here must not invalidate the spool documents peers are writing.
CONTROL_SCHEMA_VERSION = 2
#: Control documents this binary can read.  Version 1 predates the pending
#: drop's deadline and retained evidence; see `_pending_drop`.
CONTROL_SCHEMA_VERSIONS_READ = (1, 2)
#: #825/#832: the account-bound protocol generation. Every current document,
#: retry namespace and plan names it; the legacy spool never carries it.
PROTOCOL_GENERATION = "account-bound-v1"
#: D5: a non-extendable wall clock measured from the first pending drop.
PENDING_DROP_DEADLINE_SECONDS = 180
CANDIDATE_DOCUMENT_MAX_BYTES = 4 * 1024
CONTROL_DOCUMENT_MAX_BYTES = 1024 * 1024
TOMBSTONE_DOCUMENT_MAX_BYTES = 1024
CANDIDATE_TOKEN_RE = re.compile(r"[0-9a-f]{64}\Z")
OBSERVATION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
AXES = ("fiveHour", "sevenDay")
_SIGNED_I64_MIN = -(2**63)
_SIGNED_I64_MAX = 2**63 - 1


class StateValidationError(ValueError):
    """A persisted arbitration artifact is malformed or outside its contract."""


@dataclasses.dataclass(frozen=True)
class AxisValue:
    percent: float
    raw_resets_at: int
    canonical_key: int | None = None


@dataclasses.dataclass(frozen=True)
class Candidate:
    token: str = ""
    received_at: int = 0
    five_hour: AxisValue | None = None
    seven_day: AxisValue | None = None
    #: The immutable `AccountBinding` captured with this candidate (#825/#832
    #: spec 4.2). The reducer partitions by its account; it is never inferred.
    binding: object | None = None


@dataclasses.dataclass(frozen=True)
class AxisProjection:
    percent: float
    raw_resets_at: int
    canonical_key: int
    captured_at: int
    source: str
    #: The reset generation the projected value belongs to. Weekly: the
    #: reset-aware floor epoch (an int). Five-hour (#832): the LOGICAL
    #: generation string of the exact account's active reset event, or "" for
    #: no event — never a SQLite rowid, so a rebuild cannot change it.
    reset_generation: int | str


@dataclasses.dataclass(frozen=True)
class FileFingerprint:
    device: int
    inode: int
    size: int
    mtime_ns: int


#: The classification evidence of one projected axis (#825/#832 spec 4.3
#: revision 5, Codex design-fix-round-5 Q1). ``present``: the axis holds a
#: baseline. ``expired_predecessor``: every admitted five-hour predecessor
#: left the projection only through expiry at the evaluation's instant (on
#: the weekly axis: every retained weekly row lies in a week that ends at or
#: before ``predecessor_key``, fix round K14c). ``proven_empty``: the covered
#: snapshot proves the account has no admitted baseline on the axis.
#: ``floor_retired`` (revision 6, Codex design-fix-round-6 Q2): the axis's
#: group has an effective, admitted floor of the account and no accepted
#: observation of the axis survives at or after it. ``unknown``: anything
#: else — never inferred from an absent value.
EVIDENCE_PRESENT = "present"
EVIDENCE_EXPIRED_PREDECESSOR = "expired_predecessor"
EVIDENCE_PROVEN_EMPTY = "proven_empty"
EVIDENCE_FLOOR_RETIRED = "floor_retired"
EVIDENCE_UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class AxisEvidence:
    """How an axis of a projection was classified, computed inside the same
    validated, prefix-covered snapshot as the projection itself. An
    expired predecessor keeps the greatest expired window's account-canonical
    key and raw reset: the evidence a later reading's window is compared
    with, never a value to display or to fill an axis with."""

    kind: str
    predecessor_key: int | None = None
    predecessor_raw: int | None = None
    #: Fix round K12 (K11R-10): for an unknown axis that wall time alone can
    #: classify — a row beyond the future band — the earliest instant at
    #: which such a row becomes admissible; else None.
    admissible_at: int | None = None
    #: ``floor_retired`` only (spec 4.3 revision 6): the account, the axis,
    #: the retired group's account-canonical key, the floor instant, the
    #: floor's logical identity and the group's anchor raw reset. Never a
    #: retired percentage.
    account_key: str | None = None
    axis: str | None = None
    group_key: int | None = None
    floor_epoch: int | None = None
    floor_identity: str | None = None
    anchor_raw: int | None = None


EVIDENCE_UNKNOWN_AXIS = AxisEvidence(EVIDENCE_UNKNOWN)

#: How `classify_absent_axis` classifies a reading on an axis that holds no
#: projected baseline. ``publish``: a first reading or a new window, under
#: either drop gate. ``stale``: an older window, or a reading proven captured
#: before the floor of a retired group — the older-window disposition, which
#: publishes nothing on the axis and opens no consensus. ``floor``: a reading
#: in a floor-retired group, which publishes only from contributors captured
#: at or after the floor. ``unknown``: unclassifiable; the caller defers the
#: whole evaluation.
CLASS_PUBLISH = "publish"
CLASS_STALE = "stale"
CLASS_FLOOR = "floor"
CLASS_UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class DbProjection:
    five_hour: AxisProjection | None
    seven_day: AxisProjection | None
    db_files: Mapping[str, FileFingerprint | None] | None = None
    #: The exact account this projection was read for (#825/#832 spec 4.5).
    account_key: str | None = None
    #: Per-axis `AxisEvidence` (spec 4.3 revision 5), keyed by axis name. Only
    #: the covered projection read carries it; a projection without it — a
    #: persisted control document, a deferred read — classifies an absent
    #: axis as unknown.
    evidence: Mapping[str, AxisEvidence] | None = None


@dataclasses.dataclass(frozen=True)
class Contributor:
    baseline_received_at: int
    satisfied: bool


@dataclasses.dataclass(frozen=True)
class RetrySignature:
    candidate_key: int
    candidate_percent: float
    db_key: int | None
    db_percent: float | None
    db_reset_generation: int | str | None
    #: The retry namespace (#825/#832 spec 4.3): protocol generation, account
    #: and axis. Window, value and logical generation are the fields above.
    protocol_generation: str | None = None
    account_key: str | None = None
    axis: str | None = None


@dataclasses.dataclass(frozen=True)
class SupportingObservation:
    """One observation that reported the pending low.

    ``observation_id`` identifies the observation itself rather than the moment
    this machine read it.  It is a pure function of the reporting contributor
    and the axis content that contributor reported, so re-reading one spool
    record and receiving another render of the same upstream reading both
    produce the identity already retained.  Neither is a second distinct
    confirmation, which a receipt-time comparison alone cannot express.
    """

    observation_id: str
    token: str
    percent: float
    raw_resets_at: int
    received_at: int


@dataclasses.dataclass(frozen=True)
class RetainedLow:
    """The low value a pending drop will publish, and the evidence for it.

    A candidate is active only while ``-5 <= now - received_at < 90`` while the
    deadline is 180 seconds, so every contributor supporting a pending low can
    age out of the active set before that drop's own deadline expires.  The
    record therefore carries the value and its supporting observations rather
    than re-deriving them from a live candidate set that is empty by then.
    """

    percent: float
    raw_resets_at: int
    canonical_key: int
    observations: tuple[SupportingObservation, ...]


@dataclasses.dataclass(frozen=True)
class PendingDrop:
    canonical_key: int
    reduced_percent: float
    first_seen_at: int
    kernel_stage: str
    attempts: int
    contributors: Mapping[str, Contributor]
    retry_signature: RetrySignature | None
    #: First-seen plus ``PENDING_DROP_DEADLINE_SECONDS``.  Persisted once and
    #: never advanced: neither membership churn nor an upward report may move
    #: it.  Deliberately carries no default, so that every construction site
    #: states the deadline the record will be judged against.
    deadline_at: int
    retained: RetainedLow | None = None


@dataclasses.dataclass(frozen=True)
class ControlState:
    db_projection: DbProjection
    pending_drops: Mapping[str, PendingDrop | None]


@dataclasses.dataclass(frozen=True)
class Tombstone:
    axis: str
    state: str
    started_at: int | None = None
    prior_block_received_at_through: int | None = None
    block_received_at_through: int | None = None


@dataclasses.dataclass(frozen=True)
class PublicationPlan:
    seven_day: AxisValue | None
    five_hour: AxisValue | None


@dataclasses.dataclass(frozen=True)
class ReductionDecision:
    action: str
    control: ControlState
    plan: PublicationPlan | None = None
    #: Fix round K13 (review cycle 7 finding P1-2, spec 4.3 *Classification
    #: evidence*): the axes on which a reading met no projected baseline and
    #: no classification evidence. Neither drop gate publishes such an axis;
    #: the caller defers the whole evaluation instead of reading the NOOP
    #: on it as an unchanged reading.
    unknown_axes: frozenset = frozenset()


def _is_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and _SIGNED_I64_MIN <= value <= _SIGNED_I64_MAX
    )


def _is_nonnegative_int(value: object) -> bool:
    return _is_int(value) and value >= 0


def _percent(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateValidationError("percent must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 100.0:
        raise StateValidationError("percent outside [0,100]")
    return result


def _require_object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise StateValidationError(f"{label} must be an object")
    return value


def _require_exact_keys(doc: dict, required: set[str]) -> None:
    if set(doc) != required:
        raise StateValidationError("unexpected document keys")


def _require_ascii_source(value: object) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 64):
        raise StateValidationError("source must be 1..64 ASCII characters")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise StateValidationError("source must be ASCII") from exc
    return value


def _axis_value(
    value: object,
    *,
    axis: str,
    reset_is_plausible: Callable[[str, int], bool],
) -> AxisValue:
    doc = _require_object(value, axis)
    _require_exact_keys(doc, {"percent", "resetsAt"})
    resets_at = doc["resetsAt"]
    if not _is_int(resets_at) or not reset_is_plausible(axis, resets_at):
        raise StateValidationError("invalid resetsAt")
    return AxisValue(percent=_percent(doc["percent"]), raw_resets_at=resets_at)


def _reject_json_constant(value: str) -> None:
    raise StateValidationError(f"non-standard JSON constant {value}")


def _load_json_document(raw: str | bytes, *, maximum_bytes: int) -> object:
    if isinstance(raw, bytes):
        data = raw
        try:
            raw_text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StateValidationError("document is not UTF-8") from exc
    elif isinstance(raw, str):
        raw_text = raw
        data = raw.encode("utf-8")
    else:
        raise StateValidationError("document must be text")
    if len(data) > maximum_bytes:
        raise StateValidationError("document exceeds size limit")
    try:
        return json.loads(raw_text, parse_constant=_reject_json_constant)
    except StateValidationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StateValidationError("invalid JSON document") from exc


def validate_candidate_document(
    document: object,
    *,
    now_epoch: int,
    reset_is_plausible: Callable[[str, int], bool],
    token: str = "",
) -> Candidate:
    """Validate one persisted candidate document without reading filesystem state."""
    doc = _require_object(document, "candidate")
    allowed = {"schemaVersion", "receivedAt", "fiveHour", "sevenDay"}
    if (set(doc) - allowed or not {"schemaVersion", "receivedAt"} <= set(doc)
            or not ({"fiveHour", "sevenDay"} & set(doc))):
        raise StateValidationError("unexpected document keys")
    if not _is_int(doc["schemaVersion"]) or doc["schemaVersion"] != SCHEMA_VERSION:
        raise StateValidationError("unsupported schemaVersion")
    received_at = doc["receivedAt"]
    if not _is_int(received_at) or not 0 <= received_at <= now_epoch + 5:
        raise StateValidationError("invalid receivedAt")
    if token and not CANDIDATE_TOKEN_RE.fullmatch(token):
        raise StateValidationError("invalid candidate token")
    return Candidate(
        token=token,
        received_at=received_at,
        five_hour=(
            _axis_value(doc["fiveHour"], axis="fiveHour", reset_is_plausible=reset_is_plausible)
            if "fiveHour" in doc else None
        ),
        seven_day=(
            _axis_value(doc["sevenDay"], axis="sevenDay", reset_is_plausible=reset_is_plausible)
            if "sevenDay" in doc else None
        ),
    )


def load_candidate_document(
    raw: str | bytes,
    *,
    now_epoch: int,
    reset_is_plausible: Callable[[str, int], bool],
    token: str = "",
) -> Candidate:
    return validate_candidate_document(
        _load_json_document(raw, maximum_bytes=CANDIDATE_DOCUMENT_MAX_BYTES),
        now_epoch=now_epoch,
        reset_is_plausible=reset_is_plausible,
        token=token,
    )


def _axis_projection(value: object) -> AxisProjection | None:
    if value is None:
        return None
    doc = _require_object(value, "axis projection")
    _require_exact_keys(
        doc,
        {"percent", "rawResetsAt", "canonicalKey", "capturedAt", "source", "resetGeneration"},
    )
    for key in ("rawResetsAt", "canonicalKey", "capturedAt"):
        if not _is_int(doc[key]):
            raise StateValidationError(f"{key} must be an integer")
    generation = _reset_generation(doc["resetGeneration"], "resetGeneration")
    return AxisProjection(
        percent=_percent(doc["percent"]),
        raw_resets_at=doc["rawResetsAt"],
        canonical_key=doc["canonicalKey"],
        captured_at=doc["capturedAt"],
        source=_require_ascii_source(doc["source"]),
        reset_generation=generation,
    )


#: A logical five-hour generation is `<effective>|<detected>|<journal id>`.
_RESET_GENERATION_MAX_CHARS = 512


def _reset_generation(value: object, label: str) -> int | str:
    """A weekly floor epoch (nonnegative int) or a five-hour logical
    generation (bounded ASCII string; "" means no event)."""
    if _is_nonnegative_int(value):
        return value
    if (isinstance(value, str) and len(value) <= _RESET_GENERATION_MAX_CHARS
            and value.isascii()):
        return value
    raise StateValidationError(f"{label} must be a nonnegative int or a logical generation")


def _fingerprint(value: object) -> FileFingerprint | None:
    if value is None:
        return None
    doc = _require_object(value, "fingerprint")
    _require_exact_keys(doc, {"device", "inode", "size", "mtimeNs"})
    if not all(_is_nonnegative_int(doc[k]) for k in doc):
        raise StateValidationError("invalid fingerprint")
    return FileFingerprint(
        device=doc["device"], inode=doc["inode"], size=doc["size"], mtime_ns=doc["mtimeNs"]
    )


def _nullable_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if not _is_int(value):
        raise StateValidationError(f"{label} must be an integer or null")
    return value


def _nullable_percent(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _percent(value)


def _retry_signature(value: object) -> RetrySignature | None:
    if value is None:
        return None
    doc = _require_object(value, "retrySignature")
    base = {"candidateKey", "candidatePercent", "dbKey", "dbPercent", "dbResetGeneration"}
    scoped = base | {"protocolGeneration", "accountKey", "axis"}
    if set(doc) not in (base, scoped):
        raise StateValidationError("unexpected retrySignature keys")
    if not _is_int(doc["candidateKey"]):
        raise StateValidationError("candidateKey must be an integer")
    db_generation = doc["dbResetGeneration"]
    if db_generation is not None:
        db_generation = _reset_generation(db_generation, "dbResetGeneration")
    scope = (None, None, None)
    if set(doc) == scoped:
        scope = (doc["protocolGeneration"], doc["accountKey"], doc["axis"])
        if (not isinstance(scope[0], str) or not isinstance(scope[1], str)
                or not scope[1] or scope[2] not in AXES):
            raise StateValidationError("invalid retry namespace")
    return RetrySignature(
        candidate_key=doc["candidateKey"],
        candidate_percent=_percent(doc["candidatePercent"]),
        db_key=_nullable_int(doc["dbKey"], "dbKey"),
        db_percent=_nullable_percent(doc["dbPercent"], "dbPercent"),
        db_reset_generation=db_generation,
        protocol_generation=scope[0],
        account_key=scope[1],
        axis=scope[2],
    )


def _supporting_observation(value: object, *, now_epoch: int) -> SupportingObservation:
    doc = _require_object(value, "supporting observation")
    _require_exact_keys(
        doc, {"observationId", "token", "percent", "rawResetsAt", "receivedAt"}
    )
    if not isinstance(doc["observationId"], str) or not OBSERVATION_ID_RE.fullmatch(
        doc["observationId"]
    ):
        raise StateValidationError("invalid observationId")
    if not isinstance(doc["token"], str) or not CANDIDATE_TOKEN_RE.fullmatch(doc["token"]):
        raise StateValidationError("invalid supporting observation token")
    if not _is_int(doc["rawResetsAt"]):
        raise StateValidationError("rawResetsAt must be an integer")
    received = doc["receivedAt"]
    if not _is_int(received) or not 0 <= received <= now_epoch + 5:
        raise StateValidationError("invalid supporting receivedAt")
    return SupportingObservation(
        observation_id=doc["observationId"],
        token=doc["token"],
        percent=_percent(doc["percent"]),
        raw_resets_at=doc["rawResetsAt"],
        received_at=received,
    )


def _retained_low(value: object, *, now_epoch: int) -> RetainedLow | None:
    if value is None:
        return None
    doc = _require_object(value, "retained low")
    _require_exact_keys(doc, {"percent", "rawResetsAt", "canonicalKey", "observations"})
    for key in ("rawResetsAt", "canonicalKey"):
        if not _is_int(doc[key]):
            raise StateValidationError(f"{key} must be an integer")
    observations = doc["observations"]
    if not isinstance(observations, list) or len(observations) > 4096:
        raise StateValidationError("observations must be a bounded array")
    return RetainedLow(
        percent=_percent(doc["percent"]),
        raw_resets_at=doc["rawResetsAt"],
        canonical_key=doc["canonicalKey"],
        observations=tuple(
            _supporting_observation(item, now_epoch=now_epoch) for item in observations
        ),
    )


def _pending_drop(
    value: object, *, now_epoch: int, control_version: int = CONTROL_SCHEMA_VERSION
) -> PendingDrop | None:
    if value is None:
        return None
    doc = _require_object(value, "pending drop")
    required = {
        "canonicalKey", "reducedPercent", "firstSeenAt", "kernelStage",
        "attempts", "contributors", "retrySignature",
    }
    if control_version >= 2:
        required |= {"deadlineAt", "retained"}
    _require_exact_keys(doc, required)
    if not _is_int(doc["canonicalKey"]):
        raise StateValidationError("canonicalKey must be an integer")
    if not _is_int(doc["firstSeenAt"]) or not 0 <= doc["firstSeenAt"] <= now_epoch + 5:
        raise StateValidationError("invalid firstSeenAt")
    if doc["kernelStage"] not in {"settling", "ready", "zero_armed", "suppressed"}:
        raise StateValidationError("invalid kernelStage")
    if not _is_int(doc["attempts"]) or not 0 <= doc["attempts"] <= 2:
        raise StateValidationError("invalid attempts")
    contributors_doc = _require_object(doc["contributors"], "contributors")
    if len(contributors_doc) > 4096:
        raise StateValidationError("too many contributors")
    contributors: dict[str, Contributor] = {}
    for token, contributor in contributors_doc.items():
        if not isinstance(token, str) or not CANDIDATE_TOKEN_RE.fullmatch(token):
            raise StateValidationError("invalid contributor token")
        contributor_doc = _require_object(contributor, "contributor")
        _require_exact_keys(contributor_doc, {"baselineReceivedAt", "satisfied"})
        baseline = contributor_doc["baselineReceivedAt"]
        if not _is_int(baseline) or not 0 <= baseline <= now_epoch + 5:
            raise StateValidationError("invalid baselineReceivedAt")
        if not isinstance(contributor_doc["satisfied"], bool):
            raise StateValidationError("satisfied must be boolean")
        contributors[token] = Contributor(
            baseline_received_at=baseline, satisfied=contributor_doc["satisfied"]
        )
    reduced_percent = _percent(doc["reducedPercent"])
    retry_signature = _retry_signature(doc["retrySignature"])
    if control_version < 2:
        # A pending drop written by the previous binary carries neither the
        # deadline nor the retained evidence, and neither can be recovered.  The
        # evidence is not invented, and the deadline is not reconstructed from
        # `firstSeenAt` either: that instant was stamped under the pre-#755
        # arming rule, where a pending drop was armed from the reduced maximum
        # rather than from a retained low, so it does not mean "when this low
        # was first seen" under this contract.  The record is therefore
        # unreadable here and is discarded on read; the drop re-arms from live
        # evidence at the next evaluation.  The rest of the document — the
        # projection it was read for — is kept, and its shape is still validated
        # above, so a malformed record is refused rather than dropped quietly.
        # A binary running the previous version discards this one's control
        # document in the same way, for the same reason.
        return None
    deadline_at = doc["deadlineAt"]
    if not _is_int(deadline_at) or deadline_at < doc["firstSeenAt"]:
        raise StateValidationError("invalid deadlineAt")
    retained = _retained_low(doc["retained"], now_epoch=now_epoch)
    return PendingDrop(
        canonical_key=doc["canonicalKey"],
        reduced_percent=reduced_percent,
        first_seen_at=doc["firstSeenAt"],
        kernel_stage=doc["kernelStage"],
        attempts=doc["attempts"],
        contributors=contributors,
        retry_signature=retry_signature,
        deadline_at=deadline_at,
        retained=retained,
    )


def validate_control_document(document: object, *, now_epoch: int) -> ControlState:
    doc = _require_object(document, "control state")
    _require_exact_keys(doc, {"schemaVersion", "dbProjection", "dbFiles", "pendingDrops"})
    if not _is_int(doc["schemaVersion"]) or doc["schemaVersion"] not in CONTROL_SCHEMA_VERSIONS_READ:
        raise StateValidationError("unsupported schemaVersion")
    control_version = doc["schemaVersion"]
    projection_doc = _require_object(doc["dbProjection"], "dbProjection")
    _require_exact_keys(projection_doc, {"fiveHour", "sevenDay"})
    files_doc = _require_object(doc["dbFiles"], "dbFiles")
    _require_exact_keys(files_doc, {"main", "wal"})
    main = _fingerprint(files_doc["main"])
    if main is None:
        raise StateValidationError("dbFiles.main is required")
    pending_doc = _require_object(doc["pendingDrops"], "pendingDrops")
    _require_exact_keys(pending_doc, {"fiveHour", "sevenDay"})
    return ControlState(
        db_projection=DbProjection(
            five_hour=_axis_projection(projection_doc["fiveHour"]),
            seven_day=_axis_projection(projection_doc["sevenDay"]),
            db_files={"main": main, "wal": _fingerprint(files_doc["wal"])},
        ),
        pending_drops={
            "fiveHour": _pending_drop(
                pending_doc["fiveHour"], now_epoch=now_epoch, control_version=control_version
            ),
            "sevenDay": _pending_drop(
                pending_doc["sevenDay"], now_epoch=now_epoch, control_version=control_version
            ),
        },
    )


def load_control_document(raw: str | bytes, *, now_epoch: int) -> ControlState:
    return validate_control_document(
        _load_json_document(raw, maximum_bytes=CONTROL_DOCUMENT_MAX_BYTES), now_epoch=now_epoch
    )


def validate_tombstone_document(
    document: object, *, expected_axis: str, now_epoch: int
) -> Tombstone:
    doc = _require_object(document, "tombstone")
    if expected_axis not in AXES:
        raise StateValidationError("invalid expected axis")
    if not _is_int(doc.get("schemaVersion")) or doc["schemaVersion"] != SCHEMA_VERSION:
        raise StateValidationError("unsupported schemaVersion")
    if doc.get("axis") != expected_axis:
        raise StateValidationError("wrong tombstone axis")
    state = doc.get("state")
    if state == "inflight":
        _require_exact_keys(
            doc,
            {"schemaVersion", "axis", "state", "startedAt", "priorBlockReceivedAtThrough"},
        )
        started = doc["startedAt"]
        prior = doc["priorBlockReceivedAtThrough"]
        if not _is_int(started) or not 0 <= started <= now_epoch + 5:
            raise StateValidationError("invalid startedAt")
        if prior is not None and (not _is_int(prior) or not 0 <= prior <= now_epoch + 5):
            raise StateValidationError("invalid priorBlockReceivedAtThrough")
        return Tombstone(
            axis=expected_axis,
            state=state,
            started_at=started,
            prior_block_received_at_through=prior,
        )
    if state == "committed":
        _require_exact_keys(
            doc,
            {"schemaVersion", "axis", "state", "blockReceivedAtThrough"},
        )
        cutoff = doc["blockReceivedAtThrough"]
        if not _is_int(cutoff) or not 0 <= cutoff <= now_epoch + 5:
            raise StateValidationError("invalid blockReceivedAtThrough")
        return Tombstone(axis=expected_axis, state=state, block_received_at_through=cutoff)
    raise StateValidationError("invalid tombstone state")


def load_tombstone_document(
    raw: str | bytes, *, expected_axis: str, now_epoch: int
) -> Tombstone:
    return validate_tombstone_document(
        _load_json_document(raw, maximum_bytes=TOMBSTONE_DOCUMENT_MAX_BYTES),
        expected_axis=expected_axis,
        now_epoch=now_epoch,
    )


def canonicalize_five_hour_axes(
    values: list[AxisValue] | tuple[AxisValue, ...],
    *,
    db_anchor: tuple[int, int] | None,
    canonicalize: Callable[[int, tuple[int, int] | None], int],
) -> tuple[AxisValue, ...]:
    """Canonicalize sorted raw 5h resets with a rolling physical-window anchor.

    A distant old DB anchor must not make two adjacent new raw resets form two
    artificial windows.  Only a newly established key advances the rolling
    anchor; a reused key retains the first raw reset of that cluster.
    """
    anchor = db_anchor
    result: list[AxisValue] = []
    for value in sorted(values, key=lambda item: item.raw_resets_at):
        key = canonicalize(value.raw_resets_at, anchor)
        if anchor is None or key != anchor[1]:
            anchor = (value.raw_resets_at, key)
        result.append(dataclasses.replace(value, canonical_key=key))
    return tuple(result)


@dataclasses.dataclass(frozen=True)
class _AxisDecision:
    action: str
    candidate: AxisValue | None
    pending: PendingDrop | None
    #: A reading on an axis with no baseline and no classification evidence
    #: (fix round K13): never published, whatever the drop gate.
    unknown: bool = False


def _candidate_axis(candidate: Candidate, axis: str) -> AxisValue | None:
    return candidate.five_hour if axis == "fiveHour" else candidate.seven_day


def _projection_axis(projection: DbProjection, axis: str) -> AxisProjection | None:
    return projection.five_hour if axis == "fiveHour" else projection.seven_day


def axis_evidence(projection: DbProjection, axis: str) -> AxisEvidence:
    """The classification evidence of ``axis``: ``present`` whenever the
    axis holds a baseline, else what the covered read recorded, else
    unknown (an absent value alone proves nothing)."""
    if _projection_axis(projection, axis) is not None:
        return AxisEvidence(EVIDENCE_PRESENT)
    recorded = (projection.evidence or {}).get(axis)
    return recorded if isinstance(recorded, AxisEvidence) else EVIDENCE_UNKNOWN_AXIS


def classify_absent_axis(evidence: AxisEvidence | None, key: int, *,
                         axis: str) -> str:
    """How a reading in window ``key`` on ``axis``, which holds no projected
    baseline, is classified (spec 4.3 revisions 5 and 6): one of the
    ``CLASS_*`` answers.

    - Proven empty, or a predecessor whose window ``key`` strictly follows:
      a first reading or a new window, so it publishes under a closed drop
      gate too.
    - An expired five-hour predecessor whose key ``key`` does not strictly
      exceed (revision 6, *Stale expired window*, Codex design-fix-round-6
      Q3): stale. On the weekly axis the predecessor's key is only a bound,
      so such a reading stays unknown.
    - A floor-retired group (revision 6, *Floor retired*, Q2): a strictly
      newer group takes the new-window rule, an older group is stale, and a
      reading in the group itself depends on its contributors' capture
      instants (``CLASS_FLOOR``).
    - Anything else is unknown classification."""
    if evidence is None:
        return CLASS_UNKNOWN
    if evidence.kind == EVIDENCE_PROVEN_EMPTY:
        return CLASS_PUBLISH
    if (evidence.kind == EVIDENCE_EXPIRED_PREDECESSOR
            and evidence.predecessor_key is not None):
        if int(key) > int(evidence.predecessor_key):
            return CLASS_PUBLISH
        return CLASS_STALE if axis == "fiveHour" else CLASS_UNKNOWN
    if (evidence.kind == EVIDENCE_FLOOR_RETIRED
            and evidence.group_key is not None
            and evidence.floor_epoch is not None):
        if int(key) > int(evidence.group_key):
            return CLASS_PUBLISH
        if int(key) < int(evidence.group_key):
            return CLASS_STALE
        return CLASS_FLOOR
    return CLASS_UNKNOWN


def classifies_first_reading(evidence: AxisEvidence | None, key: int) -> bool:
    """Whether a reading in window ``key`` on an axis with no projected
    baseline is a first reading or a new window whatever its contributors
    (`classify_absent_axis` answers ``publish``). Such a reading is never a
    drop, so it publishes under a closed drop gate too."""
    return classify_absent_axis(evidence, key, axis="sevenDay") == CLASS_PUBLISH


def _axis_value_key(value: AxisValue) -> int:
    return value.canonical_key if value.canonical_key is not None else value.raw_resets_at


def _eligible_candidate_axis(
    candidate: Candidate,
    axis: str,
    tombstone: Tombstone | None,
) -> AxisValue | None:
    value = _candidate_axis(candidate, axis)
    if value is None:
        return None
    if tombstone is None:
        return value
    if tombstone.state == "inflight":
        return None
    if (tombstone.state == "committed"
            and tombstone.block_received_at_through is not None
            and candidate.received_at <= tombstone.block_received_at_through):
        return None
    return value


def _reduced_candidate(
    candidates: tuple[Candidate, ...],
    *,
    axis: str,
    tombstone: Tombstone | None,
    db_axis: AxisProjection | None,
) -> tuple[AxisValue, tuple[tuple[Candidate, AxisValue], ...]] | None:
    eligible = tuple(
        (candidate, value)
        for candidate in candidates
        if (value := _eligible_candidate_axis(candidate, axis, tombstone)) is not None
    )
    if not eligible:
        return None
    newest_key = max(_axis_value_key(value) for _, value in eligible)
    in_window = tuple(
        (candidate, value)
        for candidate, value in eligible if _axis_value_key(value) == newest_key
    )
    maximum = max(value.percent for _, value in in_window)
    maxima = tuple((candidate, value) for candidate, value in in_window if value.percent == maximum)
    if db_axis is not None and db_axis.canonical_key == newest_key:
        raw = db_axis.raw_resets_at
    else:
        raw = min(value.raw_resets_at for _, value in maxima)
    return AxisValue(maximum, raw, canonical_key=newest_key), in_window


def observation_identity(token: str, axis: str, value: AxisValue) -> str:
    """Identify one upstream reading, independently of when it was read.

    The digest covers the reporting contributor and the axis content it
    reported.  It deliberately excludes the receipt time, because the receipt
    time records when this machine rendered the reading rather than when the
    provider produced it: several renders of one cached upstream block carry
    distinct receipt times and are still one observation.
    """
    payload = f"{token}|{axis}|{float(value.percent)!r}|{int(value.raw_resets_at)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _supporting_observations(
    in_window: tuple[tuple[Candidate, AxisValue], ...], axis: str, low: float
) -> tuple[SupportingObservation, ...]:
    """The observations reporting ``low``, deduplicated by observation identity."""
    seen: dict[str, SupportingObservation] = {}
    for candidate, value in in_window:
        if value.percent != low:
            continue
        identity = observation_identity(candidate.token, axis, value)
        existing = seen.get(identity)
        if existing is None or candidate.received_at > existing.received_at:
            seen[identity] = SupportingObservation(
                observation_id=identity,
                token=candidate.token,
                percent=value.percent,
                raw_resets_at=value.raw_resets_at,
                received_at=candidate.received_at,
            )
    return tuple(sorted(seen.values(), key=lambda item: item.observation_id))


def _merged_retained(
    previous: RetainedLow | None,
    *,
    canonical_key: int,
    low: float,
    observations: tuple[SupportingObservation, ...],
) -> RetainedLow:
    """Refresh the retained evidence from the live observations.

    Observations carrying an identity already retained are the same upstream
    reading seen again, so they replace their own entry rather than adding a
    second confirmation.  A different low replaces the record outright, which
    is how an upward correction moves the target without restarting anything
    else.
    """
    merged: dict[str, SupportingObservation] = {}
    if previous is not None and previous.canonical_key == canonical_key and previous.percent == low:
        merged.update({item.observation_id: item for item in previous.observations})
    for item in observations:
        # `setdefault`, not assignment: an identity already retained is the same
        # upstream reading seen again, and rewriting its receipt time would make
        # the control document differ on every tick while nothing was confirmed.
        merged.setdefault(item.observation_id, item)
    ordered = tuple(sorted(merged.values(), key=lambda item: item.observation_id))
    return RetainedLow(
        percent=low,
        raw_resets_at=min(item.raw_resets_at for item in ordered),
        canonical_key=canonical_key,
        observations=ordered,
    )


def _retained_axis_value(retained: RetainedLow, db_axis: AxisProjection | None) -> AxisValue:
    """The value a pending drop publishes, using the DB's own raw reset when it
    describes the same physical window — the rule `_reduced_candidate` applies."""
    if db_axis is not None and db_axis.canonical_key == retained.canonical_key:
        raw = db_axis.raw_resets_at
    else:
        raw = retained.raw_resets_at
    return AxisValue(retained.percent, raw, canonical_key=retained.canonical_key)


def _new_pending(
    reduced: AxisValue,
    contributors: tuple[tuple[Candidate, AxisValue], ...],
    now_epoch: int,
    *,
    retained: RetainedLow,
) -> PendingDrop:
    return PendingDrop(
        canonical_key=_axis_value_key(reduced),
        reduced_percent=reduced.percent,
        first_seen_at=now_epoch,
        kernel_stage="settling",
        attempts=0,
        contributors={
            candidate.token: Contributor(candidate.received_at, False)
            for candidate, _ in contributors
        },
        retry_signature=None,
        deadline_at=now_epoch + PENDING_DROP_DEADLINE_SECONDS,
        retained=retained,
    )


def _reconcile_pending(
    pending: PendingDrop,
    contributors: tuple[tuple[Candidate, AxisValue], ...],
) -> PendingDrop:
    current = {candidate.token: candidate for candidate, _ in contributors}
    merged: dict[str, Contributor] = {}
    for token, candidate in current.items():
        old = pending.contributors.get(token)
        if old is None:
            merged[token] = Contributor(candidate.received_at, False)
        else:
            merged[token] = Contributor(
                baseline_received_at=old.baseline_received_at,
                satisfied=(old.satisfied or candidate.received_at > old.baseline_received_at),
            )
    return dataclasses.replace(pending, contributors=merged)


def _axis_control_changed(before: PendingDrop | None, after: PendingDrop | None) -> bool:
    return before != after


def _build_retry_signature(
    reduced: AxisValue, db_axis: AxisProjection | None, *, scope=None,
) -> RetrySignature:
    """``scope`` is ``(protocol_generation, account_key, axis)`` for a bound
    partition (#825/#832 spec 4.3), so two accounts never share a spent
    attempt even with identical values."""
    protocol, account, axis = scope if scope is not None else (None, None, None)
    return RetrySignature(
        candidate_key=_axis_value_key(reduced),
        candidate_percent=reduced.percent,
        db_key=None if db_axis is None else db_axis.canonical_key,
        db_percent=None if db_axis is None else db_axis.percent,
        db_reset_generation=None if db_axis is None else db_axis.reset_generation,
        protocol_generation=protocol,
        account_key=account,
        axis=axis,
    )


def _pending_kernel_attempt(
    pending: PendingDrop,
    reduced: AxisValue,
    db_axis: AxisProjection | None,
    *,
    scope=None,
) -> PendingDrop | None:
    """Return the persisted state for one bounded kernel attempt.

    The reducer cannot know whether the external record kernel will mutate the
    DB, so it advances the per-axis retry state *before* returning a publication
    plan.  A successful post-record re-reduction clears it from DB truth; an
    unchanged projection retains this bounded retry state for the next tick.
    """
    signature = _build_retry_signature(reduced, db_axis, scope=scope)
    if pending.kernel_stage == "suppressed":
        if pending.retry_signature == signature:
            return None
        return dataclasses.replace(
            pending,
            kernel_stage="zero_armed" if reduced.percent == 0.0 else "ready",
            attempts=1,
            retry_signature=signature,
        )
    if pending.kernel_stage == "settling" or pending.retry_signature != signature:
        return dataclasses.replace(
            pending,
            kernel_stage="zero_armed" if reduced.percent == 0.0 else "ready",
            attempts=1,
            retry_signature=signature,
        )
    if pending.kernel_stage in {"ready", "zero_armed"}:
        # The second attempt is the final expensive retry.  For zero this is
        # also the revalidated confirmation pass after cmd_record_usage armed
        # its existing debounce marker on the first attempt.
        return dataclasses.replace(
            pending,
            kernel_stage="suppressed",
            attempts=2,
            retry_signature=signature,
        )
    return None


def _has_evidence(retained: RetainedLow | None) -> bool:
    """Does this record carry a low with at least one supporting observation?

    A retained low with no observations supports nothing, so it can neither be
    published at expiry nor be retracted by a live contributor.  Both readers of
    a pending record consult this, because disagreeing about the evidence-free
    case had `_reduce_axis` cancelling where `_hold_pending` would publish.
    """
    return retained is not None and bool(retained.observations)


def _supporters_retracted(
    pending: PendingDrop,
    in_window: tuple[tuple[Candidate, AxisValue], ...],
    db_axis: AxisProjection,
) -> bool:
    """Has every PRESENT retained supporter reported at or above the baseline?

    Cancellation and expiry are distinct outcomes.  Cancellation means a
    contributor actively reported a value at or above the database baseline,
    retracting the evidence it once supplied.  A supporter that merely aged out
    of the 90-second active window has reported nothing, and reading its absence
    as retraction is the same mistake as clearing on an empty spool.

    Both rules hold at once only if the decision is taken over the supporters
    that are actually present, and only if that set is non-empty.  Requiring
    EVERY supporter to be present instead let one absent peer veto a live
    retraction: a session that came back and said the database value was right
    was ignored because a peer had ended, and the drop then published a low no
    live contributor still supported.
    """
    if not _has_evidence(pending.retained):
        return False
    supporters = {item.token for item in pending.retained.observations}
    present = {candidate.token: value for candidate, value in in_window}
    live = [present[token] for token in supporters if token in present]
    if not live:
        # Every supporter is absent, which is silence rather than retraction.
        return False
    return all(value.percent >= db_axis.percent for value in live)


def _hold_pending(
    pending: PendingDrop | None, db_axis: AxisProjection | None, now_epoch: int,
    *, scope=None, drop_allowed: bool = True,
) -> _AxisDecision:
    """Decide a pending drop with no eligible candidate on its axis.

    An axis can lose every candidate while the spool is not empty at all,
    because another axis still has candidates and evaluations keep running, and
    an in-flight tombstone suppresses an axis the same way.  Empty membership is
    never unanimous confirmation, so preservation is decided here per axis and
    per physical window rather than per spool.
    """
    if pending is None:
        return _AxisDecision("NOOP", None, None)
    if not _has_evidence(pending.retained):
        # Evidence-free pending state — a record built by hand, or one whose
        # retained low carries no observation — has nothing to publish and no
        # live contributor left to confirm it.
        return _AxisDecision("WRITE_CONTROL", None, None)
    if db_axis is None or db_axis.canonical_key != pending.canonical_key:
        return _AxisDecision("NOOP", None, pending)
    if db_axis.percent <= pending.retained.percent:
        # The database already holds the drop, so the record has done its work.
        return _AxisDecision("WRITE_CONTROL", None, None)
    if now_epoch < pending.deadline_at or not drop_allowed:
        # #825/#832 spec 5.6 item 7: while the account lacks either recovery
        # certificate a drop below the database baseline is not published at
        # its deadline either. It stays pending, with no attempt spent.
        return _AxisDecision("NOOP", None, pending)
    publish = _retained_axis_value(pending.retained, db_axis)
    attempted = _pending_kernel_attempt(pending, publish, db_axis, scope=scope)
    if attempted is not None:
        return _AxisDecision("PUBLISH_DB", publish, attempted)
    # The bounded attempts are spent and no contributor remains that could
    # change the signature, so retaining the record would never advance it.
    return _AxisDecision("WRITE_CONTROL", None, None)


def _reduce_axis(
    axis: str,
    candidates: tuple[Candidate, ...],
    db_axis: AxisProjection | None,
    pending: PendingDrop | None,
    tombstone: Tombstone | None,
    now_epoch: int,
    *,
    scope=None,
    drop_allowed: bool = True,
    evidence: AxisEvidence | None = None,
) -> _AxisDecision:
    reduced_data = _reduced_candidate(
        candidates, axis=axis, tombstone=tombstone, db_axis=db_axis
    )
    if reduced_data is None:
        return _hold_pending(pending, db_axis, now_epoch, scope=scope,
                             drop_allowed=drop_allowed)
    reduced, in_window = reduced_data
    key = _axis_value_key(reduced)

    if db_axis is None:
        verdict = classify_absent_axis(evidence, key, axis=axis)
        if verdict == CLASS_FLOOR:
            # Spec 4.3 revision 6 (*Floor retired*, Codex design-fix-round-6
            # Q2): the group's admitted floor retired every accepted reading.
            # Only contributors captured at or after the floor are the
            # ordinary first post-floor reading; one captured before it is
            # stale and is never relabelled as post-floor. The reading opens
            # no consensus and spends no attempt, under either gate.
            post = tuple((candidate, value) for candidate, value in in_window
                         if candidate.received_at >= int(evidence.floor_epoch))
            if not post:
                verdict = CLASS_STALE
            else:
                maximum = max(value.percent for _, value in post)
                reduced = AxisValue(
                    maximum,
                    min(value.raw_resets_at for _, value in post
                        if value.percent == maximum),
                    canonical_key=key)
                verdict = CLASS_PUBLISH
        if verdict == CLASS_UNKNOWN:
            # #825/#832 review finding P1-2 (Codex fix-round-4 Q2): a missing
            # projection alone proves no empty account — it cannot tell a
            # first reading from a drop below a baseline it does not show.
            # Only classification evidence does (spec 4.3 revision 5,
            # design-fix-round-5 Q1): a proven-empty baseline, or an expired
            # five-hour predecessor whose window this reading's strictly
            # follows. Without it the reading is unknown: never published,
            # and a pending drop's evidence is kept. Fix round K13 (review
            # cycle 7 P1-2): whatever the drop gate — an open gate is no
            # classification evidence either.
            return _AxisDecision("NOOP", None, pending, unknown=True)
        if verdict == CLASS_STALE:
            # Spec 4.3 revision 6 (*Stale expired window*, *Floor retired*):
            # the existing older-window disposition — nothing published on
            # this axis, no consensus opened, the pending drop kept.
            return _hold_pending(pending, None, now_epoch, scope=scope,
                                 drop_allowed=drop_allowed)
        if (evidence.kind == EVIDENCE_FLOOR_RETIRED and pending is not None
                and pending.canonical_key > int(evidence.group_key)):
            # Pending state retires only where the floor proves it obsolete:
            # a pending drop of a newer window is not the retired group's.
            return _AxisDecision("PUBLISH_DB", reduced, pending)
        return _AxisDecision("PUBLISH_DB", reduced, None)
    if key > db_axis.canonical_key:
        return _AxisDecision("PUBLISH_DB", reduced, None)
    if key < db_axis.canonical_key:
        # A stale older window says nothing about the pending newer one.
        return _hold_pending(pending, db_axis, now_epoch, scope=scope,
                             drop_allowed=drop_allowed)
    # `reduced` is the MAXIMUM across active contributors, so it can equal or
    # exceed the database value while a lower contributor is reporting a real
    # drop.  The maximum decides what is displayed; it may not erase the
    # evidence that a drop is pending, which is why the low is read separately
    # here and why a rise is a publication rather than a branch of its own.
    rising = reduced.percent > db_axis.percent
    low = min(value.percent for _, value in in_window)
    armed = (
        pending is not None
        and pending.canonical_key == key
        and _has_evidence(pending.retained)
    )

    if not armed:
        if rising:
            # The rise moves the baseline and no drop is pending against the old
            # one, so there is nothing to preserve.
            return _AxisDecision("PUBLISH_DB", reduced, None)
        if low >= db_axis.percent:
            return _AxisDecision(
                "WRITE_CONTROL" if pending is not None else "NOOP", None, None
            )
        supporters = _supporting_observations(in_window, axis, low)
        retained = _merged_retained(
            None, canonical_key=key, low=low, observations=supporters
        )
        return _AxisDecision(
            "WRITE_CONTROL", None, _new_pending(reduced, in_window, now_epoch, retained=retained)
        )

    # A cancelled or already-recorded drop stops being pending, and a rise still
    # publishes on the way out.
    cancelled = _AxisDecision(
        "PUBLISH_DB" if rising else "WRITE_CONTROL", reduced if rising else None, None
    )
    if _supporters_retracted(pending, in_window, db_axis):
        return cancelled
    if db_axis.percent <= pending.retained.percent:
        return cancelled

    reconciled = _reconcile_pending(pending, in_window)
    if low < db_axis.percent:
        supporters = _supporting_observations(in_window, axis, low)
        retained = _merged_retained(
            pending.retained, canonical_key=key, low=low, observations=supporters
        )
    else:
        # Every present contributor is at or above the baseline while at least
        # one supporter is merely absent.  Freeze the evidence rather than
        # rewriting it from a set that no longer contains the low.
        retained = pending.retained
    # An upward correction updates the reduced maximum and the retained low.  It
    # never restarts the generation: `first_seen_at`, `deadline_at` and every
    # other contributor's progress are carried through unchanged.
    updated = dataclasses.replace(
        reconciled, reduced_percent=reduced.percent, retained=retained
    )

    if rising:
        # The rise moves the database baseline, and that is all it does.
        # Discarding the pending drop here and re-arming it against the new
        # baseline was an upward report advancing the single deadline D5 fixes
        # at the first pending drop, and it is the stall family: a peer
        # reporting a higher number erased another session's real reset.  The
        # retained low keeps its meaning, because the baseline only moved UP —
        # a low armed below the old baseline is still a drop below the new one.
        return _AxisDecision("PUBLISH_DB", reduced, updated)

    unanimous = (
        reduced.percent < db_axis.percent
        and bool(updated.contributors)
        and all(item.satisfied for item in updated.contributors.values())
    )
    if not drop_allowed:
        # #825/#832 spec 5.6 item 7: withheld — neither consensus nor the
        # deadline publishes a drop below the baseline, and no bounded attempt
        # is spent; the record stays pending with its evidence.
        pass
    elif unanimous:
        attempted = _pending_kernel_attempt(updated, reduced, db_axis, scope=scope)
        if attempted is not None:
            return _AxisDecision("PUBLISH_DB", reduced, attempted)
    elif now_epoch >= updated.deadline_at:
        # D5: the first eligible evaluation after expiry publishes the retained
        # supported low despite unsatisfied contributors.  Never the
        # all-contributor maximum, never the old high, and never a fresh
        # generation over the same drop.
        publish = _retained_axis_value(updated.retained, db_axis)
        attempted = _pending_kernel_attempt(updated, publish, db_axis, scope=scope)
        if attempted is not None:
            return _AxisDecision("PUBLISH_DB", publish, attempted)
        # The bounded attempts are spent against an unchanged signature, so this
        # record can no longer advance — the same state `_hold_pending`
        # terminates on, reached with contributors still present.  Keeping it
        # would rewrite the control document on every tick whose membership
        # differs, and would let a later signature change re-publish evidence
        # that expired long ago.  What a live contributor still reports arms a
        # NEW drop from current evidence at the next evaluation, which is the
        # difference from the unanimous path above: that one publishes the LIVE
        # reduced value, so re-firing it on a signature change republishes
        # something current.
        return _AxisDecision("WRITE_CONTROL", None, None)

    return _AxisDecision(
        "WRITE_CONTROL" if _axis_control_changed(pending, updated) else "NOOP",
        None,
        updated,
    )


def reduce_candidates(
    candidates: tuple[Candidate, ...] | list[Candidate],
    *,
    db: DbProjection,
    control: ControlState,
    tombstones: Mapping[str, Tombstone | None],
    now_epoch: int,
    account_key: str | None = None,
    drop_consensus_allowed: bool = True,
) -> ReductionDecision:
    """Reduce active candidates independently for 5h and 7d axes.

    The output describes only a pure state transition.  The caller owns
    filesystem persistence, DB reconciliation, and invoking the record kernel.

    ``account_key`` names the bound partition (#825/#832 spec 4.3): every
    retry signature minted here then carries the namespace
    ``(PROTOCOL_GENERATION, account_key, axis)``. The caller passes only that
    partition's candidates, control and tombstones.

    ``drop_consensus_allowed`` is the outcome-quarantine control gate
    (#825/#832 spec 5.6 item 7). False while the account lacks either recovery
    certificate: a pending drop below the database baseline is then published
    neither by consensus nor at its deadline, and stays pending; rises and new
    windows publish as always. An axis with no projected baseline publishes
    under either gate only when ``db`` carries classification evidence for
    it (`classify_absent_axis`: a first reading, a new window or the first
    reading after a retiring floor); a stale reading takes the older-window
    disposition, and an unclassifiable one is named in ``unknown_axes`` (fix
    rounds K13 and K14c).
    """
    active = tuple(
        candidate
        for candidate in candidates
        if -5 <= now_epoch - candidate.received_at < 90
    )
    five = _reduce_axis(
        "fiveHour",
        active,
        db.five_hour,
        control.pending_drops.get("fiveHour"),
        tombstones.get("fiveHour"),
        now_epoch,
        scope=None if account_key is None else (PROTOCOL_GENERATION, account_key, "fiveHour"),
        drop_allowed=drop_consensus_allowed,
        evidence=axis_evidence(db, "fiveHour"),
    )
    seven = _reduce_axis(
        "sevenDay",
        active,
        db.seven_day,
        control.pending_drops.get("sevenDay"),
        tombstones.get("sevenDay"),
        now_epoch,
        scope=None if account_key is None else (PROTOCOL_GENERATION, account_key, "sevenDay"),
        drop_allowed=drop_consensus_allowed,
        evidence=axis_evidence(db, "sevenDay"),
    )
    next_control = ControlState(
        db_projection=db,
        pending_drops={"fiveHour": five.pending, "sevenDay": seven.pending},
    )
    unknown = frozenset(
        axis for axis, decided in (("fiveHour", five), ("sevenDay", seven))
        if decided.unknown)
    if five.action == "PUBLISH_DB" or seven.action == "PUBLISH_DB":
        return ReductionDecision(
            "PUBLISH_DB",
            next_control,
            PublicationPlan(seven_day=seven.candidate, five_hour=five.candidate),
            unknown_axes=unknown,
        )
    if five.action == "WRITE_CONTROL" or seven.action == "WRITE_CONTROL":
        return ReductionDecision("WRITE_CONTROL", next_control,
                                 unknown_axes=unknown)
    return ReductionDecision("NOOP", next_control, unknown_axes=unknown)
