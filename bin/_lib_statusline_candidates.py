"""Pure candidate-arbitration schemas and reducer primitives for #318.

This module deliberately has no filesystem, database, clock, process, or
``cctally`` namespace dependency.  Callers supply all mutable state and time.
"""
from __future__ import annotations

import dataclasses
import json
import math
import re
from typing import Callable, Mapping


SCHEMA_VERSION = 1
CANDIDATE_DOCUMENT_MAX_BYTES = 4 * 1024
CONTROL_DOCUMENT_MAX_BYTES = 1024 * 1024
TOMBSTONE_DOCUMENT_MAX_BYTES = 1024
CANDIDATE_TOKEN_RE = re.compile(r"[0-9a-f]{64}\Z")
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


@dataclasses.dataclass(frozen=True)
class AxisProjection:
    percent: float
    raw_resets_at: int
    canonical_key: int
    captured_at: int
    source: str
    reset_generation: int


@dataclasses.dataclass(frozen=True)
class FileFingerprint:
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclasses.dataclass(frozen=True)
class DbProjection:
    five_hour: AxisProjection | None
    seven_day: AxisProjection | None
    db_files: Mapping[str, FileFingerprint | None] | None = None


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
    db_reset_generation: int | None


@dataclasses.dataclass(frozen=True)
class PendingDrop:
    canonical_key: int
    reduced_percent: float
    first_seen_at: int
    kernel_stage: str
    attempts: int
    contributors: Mapping[str, Contributor]
    retry_signature: RetrySignature | None


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
    if not _is_nonnegative_int(doc["resetGeneration"]):
        raise StateValidationError("resetGeneration must be nonnegative")
    return AxisProjection(
        percent=_percent(doc["percent"]),
        raw_resets_at=doc["rawResetsAt"],
        canonical_key=doc["canonicalKey"],
        captured_at=doc["capturedAt"],
        source=_require_ascii_source(doc["source"]),
        reset_generation=doc["resetGeneration"],
    )


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
    _require_exact_keys(
        doc,
        {"candidateKey", "candidatePercent", "dbKey", "dbPercent", "dbResetGeneration"},
    )
    if not _is_int(doc["candidateKey"]):
        raise StateValidationError("candidateKey must be an integer")
    db_generation = doc["dbResetGeneration"]
    if db_generation is not None and not _is_nonnegative_int(db_generation):
        raise StateValidationError("dbResetGeneration must be nonnegative or null")
    return RetrySignature(
        candidate_key=doc["candidateKey"],
        candidate_percent=_percent(doc["candidatePercent"]),
        db_key=_nullable_int(doc["dbKey"], "dbKey"),
        db_percent=_nullable_percent(doc["dbPercent"], "dbPercent"),
        db_reset_generation=db_generation,
    )


def _pending_drop(value: object, *, now_epoch: int) -> PendingDrop | None:
    if value is None:
        return None
    doc = _require_object(value, "pending drop")
    _require_exact_keys(
        doc,
        {"canonicalKey", "reducedPercent", "firstSeenAt", "kernelStage", "attempts", "contributors", "retrySignature"},
    )
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
    return PendingDrop(
        canonical_key=doc["canonicalKey"],
        reduced_percent=_percent(doc["reducedPercent"]),
        first_seen_at=doc["firstSeenAt"],
        kernel_stage=doc["kernelStage"],
        attempts=doc["attempts"],
        contributors=contributors,
        retry_signature=_retry_signature(doc["retrySignature"]),
    )


def validate_control_document(document: object, *, now_epoch: int) -> ControlState:
    doc = _require_object(document, "control state")
    _require_exact_keys(doc, {"schemaVersion", "dbProjection", "dbFiles", "pendingDrops"})
    if not _is_int(doc["schemaVersion"]) or doc["schemaVersion"] != SCHEMA_VERSION:
        raise StateValidationError("unsupported schemaVersion")
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
            "fiveHour": _pending_drop(pending_doc["fiveHour"], now_epoch=now_epoch),
            "sevenDay": _pending_drop(pending_doc["sevenDay"], now_epoch=now_epoch),
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


def _candidate_axis(candidate: Candidate, axis: str) -> AxisValue | None:
    return candidate.five_hour if axis == "fiveHour" else candidate.seven_day


def _projection_axis(projection: DbProjection, axis: str) -> AxisProjection | None:
    return projection.five_hour if axis == "fiveHour" else projection.seven_day


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


def _new_pending(
    reduced: AxisValue, contributors: tuple[tuple[Candidate, AxisValue], ...], now_epoch: int
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


def _build_retry_signature(reduced: AxisValue, db_axis: AxisProjection | None) -> RetrySignature:
    return RetrySignature(
        candidate_key=_axis_value_key(reduced),
        candidate_percent=reduced.percent,
        db_key=None if db_axis is None else db_axis.canonical_key,
        db_percent=None if db_axis is None else db_axis.percent,
        db_reset_generation=None if db_axis is None else db_axis.reset_generation,
    )


def _pending_kernel_attempt(
    pending: PendingDrop,
    reduced: AxisValue,
    db_axis: AxisProjection | None,
) -> PendingDrop | None:
    """Return the persisted state for one bounded kernel attempt.

    The reducer cannot know whether the external record kernel will mutate the
    DB, so it advances the per-axis retry state *before* returning a publication
    plan.  A successful post-record re-reduction clears it from DB truth; an
    unchanged projection retains this bounded retry state for the next tick.
    """
    signature = _build_retry_signature(reduced, db_axis)
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


def _reduce_axis(
    axis: str,
    candidates: tuple[Candidate, ...],
    db_axis: AxisProjection | None,
    pending: PendingDrop | None,
    tombstone: Tombstone | None,
    now_epoch: int,
) -> _AxisDecision:
    reduced_data = _reduced_candidate(
        candidates, axis=axis, tombstone=tombstone, db_axis=db_axis
    )
    if reduced_data is None:
        return _AxisDecision("WRITE_CONTROL" if pending is not None else "NOOP", None, None)
    reduced, in_window = reduced_data
    key = _axis_value_key(reduced)

    if db_axis is None:
        return _AxisDecision("PUBLISH_DB", reduced, None)
    if key > db_axis.canonical_key:
        return _AxisDecision("PUBLISH_DB", reduced, None)
    if key < db_axis.canonical_key:
        return _AxisDecision("NOOP", None, pending)
    if reduced.percent > db_axis.percent:
        return _AxisDecision("PUBLISH_DB", reduced, None)
    if reduced.percent == db_axis.percent:
        return _AxisDecision(
            "WRITE_CONTROL" if pending is not None else "NOOP",
            None,
            None,
        )

    # The current max is lower than the current DB value.  Because `reduced`
    # is that max, every active contributor in this canonical window is lower.
    if pending is None or pending.canonical_key != key:
        return _AxisDecision("WRITE_CONTROL", None, _new_pending(reduced, in_window, now_epoch))
    if reduced.percent > pending.reduced_percent:
        # A lower-only upward correction starts a distinct consensus generation;
        # old baselines must never be mutated into this new target.
        return _AxisDecision("WRITE_CONTROL", None, _new_pending(reduced, in_window, now_epoch))

    reconciled = _reconcile_pending(pending, in_window)
    if reconciled.contributors and all(item.satisfied for item in reconciled.contributors.values()):
        attempted = _pending_kernel_attempt(reconciled, reduced, db_axis)
        if attempted is not None:
            return _AxisDecision("PUBLISH_DB", reduced, attempted)
        return _AxisDecision(
            "WRITE_CONTROL" if _axis_control_changed(pending, reconciled) else "NOOP",
            None,
            reconciled,
        )
    return _AxisDecision(
        "WRITE_CONTROL" if _axis_control_changed(pending, reconciled) else "NOOP",
        None,
        reconciled,
    )


def reduce_candidates(
    candidates: tuple[Candidate, ...] | list[Candidate],
    *,
    db: DbProjection,
    control: ControlState,
    tombstones: Mapping[str, Tombstone | None],
    now_epoch: int,
) -> ReductionDecision:
    """Reduce active candidates independently for 5h and 7d axes.

    The output describes only a pure state transition.  The caller owns
    filesystem persistence, DB reconciliation, and invoking the record kernel.
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
    )
    seven = _reduce_axis(
        "sevenDay",
        active,
        db.seven_day,
        control.pending_drops.get("sevenDay"),
        tombstones.get("sevenDay"),
        now_epoch,
    )
    next_control = ControlState(
        db_projection=db,
        pending_drops={"fiveHour": five.pending, "sevenDay": seven.pending},
    )
    if five.action == "PUBLISH_DB" or seven.action == "PUBLISH_DB":
        return ReductionDecision(
            "PUBLISH_DB",
            next_control,
            PublicationPlan(seven_day=seven.candidate, five_hour=five.candidate),
        )
    if five.action == "WRITE_CONTROL" or seven.action == "WRITE_CONTROL":
        return ReductionDecision("WRITE_CONTROL", next_control)
    return ReductionDecision("NOOP", next_control)
