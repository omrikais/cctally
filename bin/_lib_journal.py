"""Pure journal kernel — line codec, identity, segment naming/order, tail scan.

The durable-truth journal (design spec
docs/superpowers/specs/2026-07-22-db-journal-redesign-design.md §4) stores one
compact JSON object per line in monthly segments. This module owns everything
about that format that is *pure*: encoding/decoding a line, deriving the stable
`id` for every line class, naming and canonically ordering segments, and
scanning a file's tail for the last complete line (torn-tail repair support).

I/O-free by construction — stdlib only (`json`, `hashlib`, `datetime`), no
imports from `_cctally_*`, no filesystem or lock access. The append/ingest I/O
lives in `bin/_cctally_journal.py`; the durability discipline that consumes
`valid_tail_offset`/`journal_high_water` lives there.

Line format (spec §4.2), additive-evolution — readers tolerate unknown keys
and unknown `t` values:

    {"v":1,"t":"obs","id":"o:…","at":"…Z","src":"…","provider":"…","payload":{…}}
    {"v":1,"t":"op","id":"o:…","at":"…Z","src":"record-credit","payload":{…}}
    {"v":1,"t":"evt","id":"<natural-key>","rev":0,"at":"…Z","src":"ingest","payload":{"kind":…}}

- `id`: obs/op carry a content digest over (t, at, src[, provider], payload);
  bootstrap-exported lines use `b:<table>:<rowid>`; evt lines carry their target
  table's full natural key with logical-id FK refs (spec §4.2 FK rule).
- `rev`: evt revision, default 0; completed correction batches select the
  highest non-conflicting revision per id before fold (#372 Task A).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass

LINE_VERSION = 1

SEGMENT_PREFIX = "observations-"
BOOTSTRAP_PREFIX = "bootstrap-"


class JournalProtocolError(ValueError):
    """A known journal record violates the revision/correction protocol."""


@dataclass(frozen=True)
class EffectiveEvent:
    """The selected state for one opaque logical event id."""

    event_id: str
    rev: int
    status: str
    content_hash: str
    batch_id: str | None
    record: dict | None
    sequence: int


@dataclass(frozen=True)
class EventConflict:
    """A divergent same-revision group, quarantined rather than fatal (#374).

    Two lines carrying the SAME `(id, rev)` but different content are a protocol
    violation the append-only journal cannot un-write: a live cycle that appends
    its evt and then aborts before the transactional `journal_id` stamp leaves
    the divergent line on disk forever. Raising on read wedged every subsequent
    `rebuild_stats_index`, which is the epoch-1002 release blocker. The selector
    now records the group here and falls through to the lowest-sequence
    provisional winner, so the index is complete and usable while the ambiguity
    is REPORTED rather than silently guessed at. `db rederive` resolves it by
    superseding the group at `rev + 1`.
    """

    event_id: str
    rev: int
    content_hashes: tuple          # every distinct hash in the group, sorted
    selected_hash: str             # the provisional (lowest-sequence) winner

    def to_dict(self) -> dict:
        """camelCase, JSON-serializable — the `journalConflicts` wire shape."""
        return {
            "eventId": self.event_id,
            "revision": self.rev,
            "contentHashes": list(self.content_hashes),
            "selectedHash": self.selected_hash,
        }


@dataclass(frozen=True)
class ProtocolViolation:
    """One structural correction-batch violation with bounded identity evidence.

    Unlike an :class:`EventConflict`, no provisional action is selected. The
    whole batch is tainted and omitted. ``evidence`` is an immutable tuple of at
    most eight scalar key/value pairs so diagnostics can identify the exact
    journal state without echoing arbitrary payloads.
    """

    batch_id: str
    kind: str
    evidence: tuple[tuple[str, object], ...]

    def __post_init__(self) -> None:
        if (
            not self.batch_id
            or not self.kind
            or not 1 <= len(self.evidence) <= 8
            or len({key for key, _value in self.evidence}) != len(self.evidence)
            or any(
                not isinstance(key, str)
                or not key
                or type(value) not in {int, str}
                for key, value in self.evidence
            )
        ):
            raise ValueError("protocol violation evidence must be bounded scalars")

    @property
    def fingerprint(self) -> str:
        """Stable identity for Task B's exact append-only acknowledgement."""
        return _sha256_canonical(
            {
                "batchId": self.batch_id,
                "kind": self.kind,
                "evidence": dict(self.evidence),
            }
        )

    def to_dict(self) -> dict:
        """Stable camelCase JSON wire shape."""
        return {
            "batchId": self.batch_id,
            "kind": self.kind,
            "evidence": dict(self.evidence),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class AcknowledgedProtocolViolation:
    """One exact tainted-batch violation plus its durable operator audit."""

    violation: ProtocolViolation
    audit_id: str
    journal_high_water: tuple[str, int]
    journal_prefix_hash: str

    @property
    def batch_id(self) -> str:
        return self.violation.batch_id

    @property
    def kind(self) -> str:
        return self.violation.kind

    @property
    def fingerprint(self) -> str:
        return self.violation.fingerprint

    def to_dict(self) -> dict:
        return {
            **self.violation.to_dict(),
            "auditId": self.audit_id,
            "journalHighWater": {
                "segment": self.journal_high_water[0],
                "offset": self.journal_high_water[1],
            },
            "journalPrefixHash": self.journal_prefix_hash,
        }


@dataclass(frozen=True)
class EffectiveSelection:
    """Active fold records plus active/tombstoned metadata keyed by event id.

    `conflicts` (#374) is additive and defaults to empty: the quarantined
    same-revision groups whose `rev` equals the WINNING revision for their event
    id. A group a completed correction batch has superseded at a higher revision
    reports nothing — that filter is what makes `db rederive` a real remedy.
    """

    active: list[dict]
    by_id: dict[str, EffectiveEvent]
    completed_batches: frozenset[str]
    conflicts: tuple = ()
    protocol_violations: tuple = ()
    acknowledged_protocol_violations: tuple = ()


# --------------------------------------------------------------------------
# line codec
# --------------------------------------------------------------------------

def _canonical_json(obj: dict) -> str:
    """Deterministic compact JSON: sorted keys, no separator whitespace,
    non-ASCII preserved literally. The single serialization shape used for
    both the on-disk line and the content digest, so an id recomputed from a
    decoded line matches the id computed at write time."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def encode_line(record: dict) -> bytes:
    """Canonical compact JSON for ``record`` plus a trailing ``\\n`` (UTF-8)."""
    return _canonical_json(record).encode("utf-8") + b"\n"


def decode_line(raw: bytes) -> dict | None:
    """Decode one journal line. Returns the dict, or ``None`` on ANY parse or
    shape failure — the line must be a JSON object carrying a string ``t``.

    ``None`` is how the ingester distinguishes a malformed line (skip + count,
    spec §4.4) from a real record; it never raises on bad input."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if not isinstance(obj.get("t"), str):
        return None
    return obj


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

def content_id(record_sans_id: dict) -> str:
    """Content digest id for an obs/op line: ``"o:" + sha256(canonical)[:16]``.

    ``record_sans_id`` is the identity-bearing subset — for obs/op that is
    ``{t, at, src[, provider], payload}`` (no ``v``, no ``id``). Stable under
    key insertion order (canonical JSON sorts keys), so re-deriving the id from
    a decoded line reproduces it (spec §4.2)."""
    digest = hashlib.sha256(_canonical_json(record_sans_id).encode("utf-8")).hexdigest()
    return "o:" + digest[:16]


def bootstrap_id(table: str, rowid: int) -> str:
    """Stable id for a row exported at cutover: ``b:<table>:<legacy rowid>``.
    Stable across cutover re-runs, which is what makes double-fold idempotent
    (spec §8)."""
    return f"b:{table}:{rowid}"


def evt_id(kind: str, *parts: object) -> str:
    """Natural-key id for an evt line: ``"<kind>:" + ":".join(str(p) …)``.

    Each caller passes its target table's full UNIQUE-constraint components,
    with any DB-assigned integer FK replaced by the *logical id* of the
    referenced record (spec §4.2). e.g.
    ``evt_id("pm", week_start_at, reset_segment_logical_id, pct)``."""
    return f"{kind}:" + ":".join(str(p) for p in parts)


# --------------------------------------------------------------------------
# fully-formed line records
# --------------------------------------------------------------------------

def make_obs(at: str, src: str, provider: str, payload: dict,
             account: str | None = None) -> dict:
    """Build a complete ``obs`` line record (raw capture; canonicalization is
    derivation-time, never baked into the stored line — spec §4.2).

    ``account`` (#341) is the account_key stamp, a top-level sibling of
    ``provider``. It is emitted ONLY when supplied, so a pre-epic / single-account
    writer that passes nothing produces a byte-identical line (and a byte-stable
    content id). When supplied it participates in the content id, so two obs that
    differ only by account are distinct records."""
    core = {"t": "obs", "at": at, "src": src, "provider": provider, "payload": payload}
    if account is not None:
        core["account"] = account
    return {"v": LINE_VERSION, **core, "id": content_id(core)}


def make_op(at: str, src: str, payload: dict) -> dict:
    """Build a complete ``op`` (operator record) line — no ``provider``."""
    core = {"t": "op", "at": at, "src": src, "payload": payload}
    return {"v": LINE_VERSION, **core, "id": content_id(core)}


_PROTOCOL_RESOLUTION_KIND = "journal_protocol_resolution"
_PROTOCOL_RESOLUTION_SRC = "journal-repair"


def make_protocol_resolution(
    *,
    at: str,
    violations: list[ProtocolViolation],
    journal_high_water: tuple[str, int],
    journal_prefix_hash: str,
) -> dict:
    """Build one deterministic audit decision over exact violation identities."""
    if not isinstance(at, str) or not at:
        raise ValueError("protocol resolution at must be a non-empty string")
    if not isinstance(violations, list) or not violations:
        raise ValueError("protocol resolution requires at least one violation")
    refs = [
        {
            "batch_id": violation.batch_id,
            "kind": violation.kind,
            "fingerprint": violation.fingerprint,
        }
        for violation in sorted(violations, key=lambda item: item.fingerprint)
    ]
    if len({item["fingerprint"] for item in refs}) != len(refs):
        raise ValueError("protocol resolution violations must be unique")
    segment, offset = journal_high_water
    if not isinstance(segment, str) or not segment or type(offset) is not int or offset < 0:
        raise ValueError("protocol resolution high-water is invalid")
    if (
        not isinstance(journal_prefix_hash, str)
        or not journal_prefix_hash.startswith("sha256:")
        or len(journal_prefix_hash) != 71
        or any(ch not in "0123456789abcdef" for ch in journal_prefix_hash[7:])
    ):
        raise ValueError("protocol resolution prefix hash is invalid")
    return make_op(
        at=at,
        src=_PROTOCOL_RESOLUTION_SRC,
        payload={
            "kind": _PROTOCOL_RESOLUTION_KIND,
            "violations": refs,
            "journal_high_water": {
                "segment": segment,
                "offset": offset,
            },
            "journal_prefix_hash": journal_prefix_hash,
        },
    )


def make_evt(kind: str, id: str, at: str, payload: dict, rev: int = 0) -> dict:
    """Build a complete ``evt`` (derived) line record.

    The ``id`` is the caller-built natural key (see ``evt_id``); ``kind`` is the
    fold-dispatch family written into ``payload["kind"]`` (spec §5.3). The
    caller's ``payload`` dict is not mutated. ``src`` is always ``"ingest"`` —
    evt lines exist only because the ingester derived them."""
    _validate_revision(rev)
    body = dict(payload)
    body["kind"] = kind
    return {"v": LINE_VERSION, "t": "evt", "id": id, "rev": rev,
            "at": at, "src": "ingest", "payload": body}


# --------------------------------------------------------------------------
# effective revisions + crash-safe correction batches (#372 Task A)
# --------------------------------------------------------------------------

def _validate_revision(rev: object) -> int:
    if type(rev) is not int or rev < 0:
        raise JournalProtocolError("event rev must be a non-negative integer")
    return rev


def event_revision(record: dict) -> int:
    """Return a strict event revision, defaulting an absent ``rev`` to zero."""
    return _validate_revision(record.get("rev", 0))


def _sha256_canonical(value: object) -> str:
    raw = json.dumps(
        value, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _protocol_violation(
    batch_id: str, kind: str, **evidence: int | str
) -> ProtocolViolation:
    """Build one bounded, immutable structural-violation result."""
    return ProtocolViolation(
        batch_id=batch_id,
        kind=kind,
        evidence=tuple(evidence.items()),
    )


def _validate_protocol_resolution(record: dict) -> dict:
    required = {"v", "t", "id", "at", "src", "payload"}
    if set(record) != required:
        raise JournalProtocolError(
            "journal protocol resolution record shape is invalid"
        )
    if (
        record.get("v") != LINE_VERSION
        or record.get("t") != "op"
        or record.get("src") != _PROTOCOL_RESOLUTION_SRC
        or not isinstance(record.get("at"), str)
        or not record["at"]
    ):
        raise JournalProtocolError(
            "journal protocol resolution record identity is invalid"
        )
    core = {
        "t": record["t"],
        "at": record["at"],
        "src": record["src"],
        "payload": record["payload"],
    }
    if record.get("id") != content_id(core):
        raise JournalProtocolError(
            "journal protocol resolution record id does not match its content"
        )
    payload = record.get("payload")
    if not isinstance(payload, dict) or set(payload) != {
        "kind",
        "violations",
        "journal_high_water",
        "journal_prefix_hash",
    }:
        raise JournalProtocolError(
            "journal protocol resolution payload shape is invalid"
        )
    if payload.get("kind") != _PROTOCOL_RESOLUTION_KIND:
        raise JournalProtocolError(
            "journal protocol resolution kind is invalid"
        )
    high_water = payload.get("journal_high_water")
    if not isinstance(high_water, dict) or set(high_water) != {"segment", "offset"}:
        raise JournalProtocolError(
            "journal protocol resolution high-water shape is invalid"
        )
    segment = high_water.get("segment")
    offset = high_water.get("offset")
    if (
        not isinstance(segment, str)
        or not segment
        or type(offset) is not int
        or offset < 0
    ):
        raise JournalProtocolError(
            "journal protocol resolution high-water is invalid"
        )
    prefix_hash = payload.get("journal_prefix_hash")
    if (
        not isinstance(prefix_hash, str)
        or not prefix_hash.startswith("sha256:")
        or len(prefix_hash) != 71
        or any(ch not in "0123456789abcdef" for ch in prefix_hash[7:])
    ):
        raise JournalProtocolError(
            "journal protocol resolution prefix hash is invalid"
        )
    refs = payload.get("violations")
    if not isinstance(refs, list) or not refs:
        raise JournalProtocolError(
            "journal protocol resolution requires exact violations"
        )
    normalized_refs = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {
            "batch_id",
            "kind",
            "fingerprint",
        }:
            raise JournalProtocolError(
                "journal protocol resolution violation shape is invalid"
            )
        if any(
            not isinstance(ref.get(key), str) or not ref[key]
            for key in ("batch_id", "kind", "fingerprint")
        ):
            raise JournalProtocolError(
                "journal protocol resolution violation identity is invalid"
            )
        normalized_refs.append(dict(ref))
    if len({ref["fingerprint"] for ref in normalized_refs}) != len(normalized_refs):
        raise JournalProtocolError(
            "journal protocol resolution violations must be unique"
        )
    return {
        "audit_id": record["id"],
        "journal_high_water": (segment, offset),
        "journal_prefix_hash": prefix_hash,
        "violations": normalized_refs,
    }


def _correction_action_core(record: dict) -> dict:
    return {
        "action": record["action"],
        "id": record["id"],
        "rev": record["rev"],
        "at": record["at"],
        "src": record["src"],
        "payload": record["payload"],
    }


def _validate_action_core(action: dict) -> dict:
    required = {"action", "id", "rev", "at", "payload"}
    if not isinstance(action, dict) or set(action) != required:
        raise JournalProtocolError(
            "correction action must contain action,id,rev,at,payload"
        )
    action_type = action.get("action")
    if action_type not in {"replace", "tombstone"}:
        raise JournalProtocolError("correction action must be replace or tombstone")
    event_id = action.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise JournalProtocolError("correction action id must be a non-empty string")
    _validate_revision(action.get("rev"))
    if not isinstance(action.get("at"), str) or not action["at"]:
        raise JournalProtocolError("correction action at must be a non-empty string")
    payload = action.get("payload")
    if action_type == "replace":
        if not isinstance(payload, dict) or not isinstance(payload.get("kind"), str):
            raise JournalProtocolError(
                "replacement correction payload must contain a string kind"
            )
    elif payload is not None:
        raise JournalProtocolError("tombstone correction payload must be null")
    return {
        "action": action_type,
        "id": event_id,
        "rev": action["rev"],
        "at": action["at"],
        "src": "rederive",
        "payload": dict(payload) if isinstance(payload, dict) else None,
    }


def make_correction_batch(
    *, batch_id: str, family: str, at: str, actions: list[dict]
) -> list[dict]:
    """Build begin/actions/commit records for one atomic correction batch.

    Callers append the returned records in order. A crash before the final
    commit marker leaves an incomplete batch that the effective selector
    ignores.
    """
    if not isinstance(batch_id, str) or not batch_id:
        raise JournalProtocolError("correction batch id must be a non-empty string")
    if not isinstance(family, str) or not family:
        raise JournalProtocolError("correction family must be a non-empty string")
    if not isinstance(at, str) or not at:
        raise JournalProtocolError("correction batch at must be a non-empty string")
    if not isinstance(actions, list):
        raise JournalProtocolError("correction actions must be a list")
    cores = [_validate_action_core(action) for action in actions]
    actions_hash = _sha256_canonical(cores)
    marker = {
        "v": LINE_VERSION,
        "t": "correction_batch",
        "id": batch_id,
        "at": at,
        "src": "rederive",
        "family": family,
        "action_count": len(cores),
        "actions_hash": actions_hash,
    }
    records = [{**marker, "phase": "begin"}]
    for seq, core in enumerate(cores):
        records.append(
            {
                "v": LINE_VERSION,
                "t": "correction",
                **core,
                "batch": batch_id,
                "seq": seq,
            }
        )
    records.append({**marker, "phase": "commit"})
    return records


def _validate_batch_marker(record: dict) -> dict:
    batch_id = record.get("id")
    if not isinstance(batch_id, str) or not batch_id:
        raise JournalProtocolError("correction batch id must be a non-empty string")
    phase = record.get("phase")
    if phase not in {"begin", "commit"}:
        raise JournalProtocolError("correction batch phase must be begin or commit")
    family = record.get("family")
    if not isinstance(family, str) or not family:
        raise JournalProtocolError("correction batch family must be a non-empty string")
    at = record.get("at")
    if not isinstance(at, str) or not at:
        raise JournalProtocolError("correction batch at must be a non-empty string")
    if record.get("src") != "rederive":
        raise JournalProtocolError("correction batch src must be rederive")
    count = record.get("action_count")
    if type(count) is not int or count < 0:
        raise JournalProtocolError(
            "correction batch action_count must be a non-negative integer"
        )
    actions_hash = record.get("actions_hash")
    if (
        not isinstance(actions_hash, str)
        or not actions_hash.startswith("sha256:")
        or len(actions_hash) != 71
    ):
        raise JournalProtocolError("correction batch actions_hash is invalid")
    try:
        int(actions_hash[7:], 16)
    except ValueError as exc:
        raise JournalProtocolError("correction batch actions_hash is invalid") from exc
    return {
        "v": record.get("v", LINE_VERSION),
        "t": "correction_batch",
        "id": batch_id,
        "at": at,
        "src": "rederive",
        "family": family,
        "action_count": count,
        "actions_hash": actions_hash,
    }


def _validate_correction_record(record: dict) -> dict:
    batch_id = record.get("batch")
    if not isinstance(batch_id, str) or not batch_id:
        raise JournalProtocolError("correction batch must be a non-empty string")
    seq = record.get("seq")
    if type(seq) is not int or seq < 0:
        raise JournalProtocolError("correction seq must be a non-negative integer")
    if record.get("src") != "rederive":
        raise JournalProtocolError("correction src must be rederive")
    core = _validate_action_core(
        {
            "action": record.get("action"),
            "id": record.get("id"),
            "rev": record.get("rev"),
            "at": record.get("at"),
            "payload": record.get("payload"),
        }
    )
    return {
        "v": record.get("v", LINE_VERSION),
        "t": "correction",
        **core,
        "batch": batch_id,
        "seq": seq,
    }


def _candidate_from_evt(record: dict, sequence: int) -> EffectiveEvent:
    event_id = record.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise JournalProtocolError("event id must be a non-empty string")
    rev = event_revision(record)
    if rev != 0:
        raise JournalProtocolError(
            "ordinary evt revision must be 0; use a completed correction batch"
        )
    if not isinstance(record.get("payload"), dict):
        raise JournalProtocolError("event payload must be an object")
    digest = _sha256_canonical(record)
    return EffectiveEvent(
        event_id=event_id,
        rev=rev,
        status="active",
        content_hash=digest,
        batch_id=None,
        record=record,
        sequence=sequence,
    )


def _candidate_from_correction(record: dict, sequence: int) -> EffectiveEvent:
    core = _correction_action_core(record)
    if record["action"] == "tombstone":
        return EffectiveEvent(
            event_id=record["id"],
            rev=record["rev"],
            status="tombstone",
            content_hash=_sha256_canonical(core),
            batch_id=record["batch"],
            record=None,
            sequence=sequence,
        )
    event = {
        "v": LINE_VERSION,
        "t": "evt",
        "id": record["id"],
        "rev": record["rev"],
        "at": record["at"],
        "src": record["src"],
        "payload": dict(record["payload"]),
    }
    return EffectiveEvent(
        event_id=record["id"],
        rev=record["rev"],
        status="active",
        content_hash=_sha256_canonical(event),
        batch_id=record["batch"],
        record=event,
        sequence=sequence,
    )


def is_legacy_quota_arming_record(record: dict | None) -> bool:
    """Recognize a pre-#372 qaa record whose natural id was reused."""
    if not isinstance(record, dict) or event_revision(record) != 0:
        return False
    payload = record.get("payload") or {}
    return (
        payload.get("kind") == "quota_alert_arming"
        and "journal_identity_version" not in payload
    )


def _is_legacy_quota_arming_state(candidate: EffectiveEvent) -> bool:
    return (
        candidate.status == "active"
        and is_legacy_quota_arming_record(candidate.record)
    )


def resolve_effective_events(
    records,
    *,
    protocol_prefix_evidence=(),
) -> EffectiveSelection:
    """Validate correction batches and select one highest revision per evt id.

    Three failure classes, deliberately asymmetric (#374 §5, #402 Task A):

    - Divergent same-revision EVENTS are **quarantined**: the lowest-sequence
      candidate becomes the provisional winner and the group is reported on
      `EffectiveSelection.conflicts`. The journal is append-only, so a divergent
      line can never be un-written; raising here wedged every rebuild forever.
    - The seven enumerated **structural** correction-batch violations taint their
      entire batch. No action from it is a candidate; the selector continues
      with distinct valid batches and reports a bounded `ProtocolViolation`.
    - Invalid marker/action field shapes and every other out-of-scope
      `JournalProtocolError` remain fatal. Unknown record types remain ignored.
    """
    candidates: list[EffectiveEvent] = []
    markers: dict[str, dict[str, tuple[dict, str, str, int]]] = {}
    actions: dict[str, dict[int, tuple[dict, str, int]]] = {}
    resolutions: list[tuple[int, dict]] = []
    tainted_batches: set[str] = set()
    violations: dict[tuple[str, str, str], ProtocolViolation] = {}
    violation_available_after: dict[str, int] = {}

    def taint(violation: ProtocolViolation, *, available_after: int) -> None:
        """Taint one batch and retain every distinct violation identity."""
        tainted_batches.add(violation.batch_id)
        violations[
            (violation.batch_id, violation.kind, violation.fingerprint)
        ] = violation
        violation_available_after[violation.fingerprint] = min(
            available_after,
            violation_available_after.get(
                violation.fingerprint,
                available_after,
            ),
        )

    for sequence, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        record_type = record.get("t")
        if record_type == "evt":
            candidates.append(_candidate_from_evt(record, sequence))
            continue
        if (
            record_type == "op"
            and isinstance(record.get("payload"), dict)
            and record["payload"].get("kind") == _PROTOCOL_RESOLUTION_KIND
        ):
            resolutions.append(
                (sequence, _validate_protocol_resolution(record))
            )
            continue
        if record_type == "correction_batch":
            normalized = _validate_batch_marker(record)
            batch_id = normalized["id"]
            phase = record["phase"]
            digest = _sha256_canonical(record)
            marker_identity = dict(record)
            marker_identity.pop("phase", None)
            identity_digest = _sha256_canonical(marker_identity)
            prior = markers.setdefault(batch_id, {}).get(phase)
            if prior is not None and prior[1] != digest:
                taint(
                    _protocol_violation(
                        batch_id,
                        "marker_conflict",
                        phase=phase,
                        firstRecordHash=prior[1],
                        conflictingRecordHash=digest,
                    ),
                    available_after=max(prior[3], sequence),
                )
            if prior is None:
                markers[batch_id][phase] = (
                    normalized,
                    digest,
                    identity_digest,
                    sequence,
                )
            continue
        if record_type == "correction":
            normalized = _validate_correction_record(record)
            batch_id = normalized["batch"]
            seq = normalized["seq"]
            digest = _sha256_canonical(record)
            prior = actions.setdefault(batch_id, {}).get(seq)
            if prior is not None and prior[1] != digest:
                taint(
                    _protocol_violation(
                        batch_id,
                        "action_sequence_conflict",
                        actionSequence=seq,
                        firstRecordHash=prior[1],
                        conflictingRecordHash=digest,
                    ),
                    available_after=max(prior[2], sequence),
                )
            if prior is None:
                actions[batch_id][seq] = (normalized, digest, sequence)

    completed: set[str] = set()
    for batch_id in sorted(set(markers) | set(actions)):
        batch_markers = markers.get(batch_id, {})
        begin = batch_markers.get("begin")
        commit = batch_markers.get("commit")
        if commit is not None and begin is None:
            _commit_core, commit_hash, _commit_identity, commit_sequence = commit
            taint(
                _protocol_violation(
                    batch_id,
                    "commit_without_begin",
                    commitSequence=commit_sequence,
                    commitRecordHash=commit_hash,
                ),
                available_after=commit_sequence,
            )
            continue
        if begin is None or commit is None:
            continue
        begin_core, _begin_hash, begin_identity, begin_sequence = begin
        commit_core, _commit_hash, commit_identity, commit_sequence = commit
        if begin_core != commit_core or begin_identity != commit_identity:
            taint(
                _protocol_violation(
                    batch_id,
                    "marker_manifest_mismatch",
                    beginSequence=begin_sequence,
                    commitSequence=commit_sequence,
                    beginIdentityHash=begin_identity,
                    commitIdentityHash=commit_identity,
                ),
                available_after=max(begin_sequence, commit_sequence),
            )
        if begin_sequence >= commit_sequence:
            taint(
                _protocol_violation(
                    batch_id,
                    "record_order_violation",
                    beginSequence=begin_sequence,
                    commitSequence=commit_sequence,
                    recordOrderHash=_sha256_canonical(
                        [begin_sequence, commit_sequence]
                    ),
                ),
                available_after=max(begin_sequence, commit_sequence),
            )
        batch_actions = actions.get(batch_id, {})
        count = begin_core["action_count"]
        present_sequences = sorted(batch_actions)
        complete_action_sequence = present_sequences == list(range(count))
        if not complete_action_sequence:
            taint(
                _protocol_violation(
                    batch_id,
                    "manifest_action_sequence_mismatch",
                    expectedActionCount=count,
                    actualActionCount=len(present_sequences),
                    presentSequencesHash=_sha256_canonical(present_sequences),
                ),
                available_after=max(
                    [
                        begin_sequence,
                        commit_sequence,
                        *(
                            item[2]
                            for item in batch_actions.values()
                        ),
                    ]
                ),
            )
        if complete_action_sequence:
            action_sequences = [batch_actions[seq][2] for seq in range(count)]
            if (
                action_sequences != sorted(action_sequences)
                or any(
                    not begin_sequence < sequence < commit_sequence
                    for sequence in action_sequences
                )
            ):
                taint(
                    _protocol_violation(
                        batch_id,
                        "record_order_violation",
                        beginSequence=begin_sequence,
                        commitSequence=commit_sequence,
                        recordOrderHash=_sha256_canonical(
                            [begin_sequence, *action_sequences, commit_sequence]
                        ),
                    ),
                    available_after=max(
                        [begin_sequence, commit_sequence, *action_sequences]
                    ),
                )
            ordered_records = [
                batch_actions[seq][0] for seq in range(count)
            ]
            cores = [
                _correction_action_core(record) for record in ordered_records
            ]
            actual_actions_hash = _sha256_canonical(cores)
            if actual_actions_hash != begin_core["actions_hash"]:
                taint(
                    _protocol_violation(
                        batch_id,
                        "manifest_actions_hash_mismatch",
                        expectedActionsHash=begin_core["actions_hash"],
                        actualActionsHash=actual_actions_hash,
                    ),
                    available_after=max(
                        [begin_sequence, commit_sequence, *action_sequences]
                    ),
                )
        if batch_id in tainted_batches:
            continue
        completed.add(batch_id)
        for seq in range(count):
            normalized, _digest, sequence = batch_actions[seq]
            candidates.append(_candidate_from_correction(normalized, sequence))

    violation_by_fingerprint = {
        violation.fingerprint: violation for violation in violations.values()
    }
    ordered_resolutions = sorted(resolutions, key=lambda item: item[0])
    prefix_evidence = tuple(protocol_prefix_evidence)
    if ordered_resolutions and len(prefix_evidence) != len(ordered_resolutions):
        raise JournalProtocolError(
            "journal protocol resolution requires verified raw-prefix evidence"
        )
    acknowledged: dict[str, AcknowledgedProtocolViolation] = {}
    for (_sequence, resolution), evidence in zip(
        ordered_resolutions,
        prefix_evidence,
    ):
        if (
            not isinstance(evidence, tuple)
            or len(evidence) != 2
            or evidence[0] != resolution["journal_high_water"]
            or evidence[1] != resolution["journal_prefix_hash"]
        ):
            raise JournalProtocolError(
                "journal protocol resolution raw-prefix binding does not match"
            )
        for ref in resolution["violations"]:
            violation = violation_by_fingerprint.get(ref["fingerprint"])
            if (
                violation is None
                or violation.batch_id != ref["batch_id"]
                or violation.kind != ref["kind"]
            ):
                raise JournalProtocolError(
                    "journal protocol resolution references an unknown violation"
                )
            if _sequence <= violation_available_after[violation.fingerprint]:
                raise JournalProtocolError(
                    "journal protocol resolution precedes the violation it resolves"
                )
            acknowledged.setdefault(
                violation.fingerprint,
                AcknowledgedProtocolViolation(
                    violation=violation,
                    audit_id=resolution["audit_id"],
                    journal_high_water=resolution["journal_high_water"],
                    journal_prefix_hash=resolution["journal_prefix_hash"],
                ),
            )

    by_revision: dict[tuple[str, int], EffectiveEvent] = {}
    # #374: divergent same-revision groups are QUARANTINED, not fatal. Keyed
    # `(event_id, rev) -> {content hashes}` while grouping; filtered to the
    # winning revision after selection (the only point where it is known).
    divergent: dict[tuple[str, int], set] = {}
    for candidate in candidates:
        key = (candidate.event_id, candidate.rev)
        prior = by_revision.get(key)
        if prior is not None and (
            prior.content_hash != candidate.content_hash
            or prior.status != candidate.status
        ):
            if (
                _is_legacy_quota_arming_state(prior)
                and _is_legacy_quota_arming_state(candidate)
            ):
                # Legacy qaa carve-out: last-wins AND silent. Unchanged by #374
                # — a pre-#372 arming record deliberately reuses its natural id
                # as a state stream, so successive lines are not a conflict.
                by_revision[key] = candidate
                continue
            divergent.setdefault(key, set()).update(
                {prior.content_hash, candidate.content_hash}
            )
        if prior is None or candidate.sequence < prior.sequence:
            by_revision[key] = candidate

    winners: dict[str, EffectiveEvent] = {}
    for candidate in by_revision.values():
        prior = winners.get(candidate.event_id)
        if prior is None or candidate.rev > prior.rev:
            winners[candidate.event_id] = candidate
    active = [
        candidate.record
        for candidate in sorted(winners.values(), key=lambda item: item.sequence)
        if candidate.status == "active" and candidate.record is not None
    ]
    # Revision scoping: report only groups AT the winning revision. A rev-0 group
    # a completed rev-1 correction batch superseded is resolved, not outstanding.
    conflicts: list[EventConflict] = []
    for key in sorted(divergent):
        event_id, rev = key
        winner = winners.get(event_id)
        if winner is None or winner.rev != rev:
            continue
        conflicts.append(
            EventConflict(
                event_id=event_id,
                rev=rev,
                content_hashes=tuple(sorted(divergent[key])),
                selected_hash=by_revision[key].content_hash,
            )
        )
    return EffectiveSelection(
        active=active,
        by_id=winners,
        completed_batches=frozenset(completed),
        conflicts=tuple(conflicts),
        protocol_violations=tuple(
            sorted(
                (
                    violation
                    for violation in violations.values()
                    if violation.fingerprint not in acknowledged
                ),
                key=lambda violation: (
                    violation.batch_id,
                    violation.kind,
                    violation.fingerprint,
                ),
            )
        ),
        acknowledged_protocol_violations=tuple(
            sorted(
                acknowledged.values(),
                key=lambda item: (
                    item.batch_id,
                    item.kind,
                    item.fingerprint,
                ),
            )
        ),
    )


# --------------------------------------------------------------------------
# accounts-machinery records (#341): registered op kinds folded into the
# `accounts` registry. These are NOT data-bearing account-stamped lines and are
# NOT legacy — the classifier recognises them by their registered `kind`.
# --------------------------------------------------------------------------

def make_account_observe(
    at: str,
    account_key: str,
    provider: str,
    *,
    natural_id: str | None = None,
    email: str | None = None,
    plan_type: str | None = None,
    label: str | None = None,
    label_source: str | None = None,
) -> dict:
    """Build an ``account_observe`` op line — appended on first sight of an
    account or an identity change (NOT every tick). Folded into the ``accounts``
    registry by ``_apply_op_account_observe`` (rebuild applier). ``last_seen_utc``
    is NOT carried here — it derives at fold time from the max ``at`` of any
    account-stamped line (spec §1)."""
    payload = {"kind": "account_observe", "account_key": account_key,
               "provider": provider}
    if natural_id is not None:
        payload["natural_id"] = natural_id
    if email is not None:
        payload["email"] = email
    if plan_type is not None:
        payload["plan_type"] = plan_type
    if label is not None:
        payload["label"] = label
    if label_source is not None:
        payload["label_source"] = label_source
    return make_op(at=at, src="account-observe", payload=payload)


def make_account_label(
    at: str,
    account_key: str,
    label: str,
    *,
    provider: str | None = None,
) -> dict:
    """Build an ``account_label`` op line (a user rename). Folded by
    ``_apply_op_account_label`` with ``label_source='user'`` — the top of the
    label-precedence order (user > switcher > auto), so a later switcher/auto
    enrichment never overrides it (spec §1)."""
    payload = {"kind": "account_label", "account_key": account_key, "label": label}
    if provider is not None:
        payload["provider"] = provider
    return make_op(at=at, src="account-label", payload=payload)


def make_codex_file_account(
    at: str,
    *,
    root_scope: str,
    file_identity: str,
    incarnation: int,
    from_offset: int,
    account_key: "str | None" = None,
) -> dict:
    """Build a ``codex_file_account`` op — the DURABLE attribution decision for
    one byte range of one rollout incarnation (#416 spec §3.3).

    Why this exists: the Codex account is otherwise re-derived from the live
    ``auth.json`` on every ingest cycle, so a ``cache-sync --rebuild`` (which
    re-reads every rollout from offset 0, cache.db being fully re-derivable by
    design) re-stamps the entire history with whoever is logged in at that
    moment. A decision that is journaled ONCE and thereafter only replayed is
    immune to that.

    The identity is ``(root_scope, file_identity, incarnation, from_offset)``,
    never ``(source_path, offset)`` (spec §3.2): discovery persists the first
    configured candidate spelling, so reordering ``$CODEX_HOME`` roots or
    respelling a symlink makes the same physical file miss a path-keyed map; and
    a truncation or root requalification resets the file to offset zero, so a
    permanent ``(path, offset)`` interval would overlap newly reused offsets and
    stamp a replacement file with the previous account. A truncation or
    requalification opens a NEW ``incarnation`` whose intervals can never
    overlap the old one.

    Sentinel encoding follows the two-shaped stamp rule
    (``docs/accounts-gotchas.md``): this is an **op**, so a real account rides
    ``payload.account_key`` and a stably-absent identity (no auth / api-key mode)
    is an explicit sentinel decision that OMITS the field. Never write the
    literal ``"unattributed"``. A **torn** read appends NOTHING at all — it is
    not a decision (spec §3.6).
    """
    payload = {
        "kind": "codex_file_account",
        "root_scope": root_scope,
        "file_identity": file_identity,
        "incarnation": incarnation,
        "from_offset": from_offset,
    }
    if account_key is not None:
        payload["account_key"] = account_key
    return make_op(at=at, src="codex-file-account", payload=payload)


# --------------------------------------------------------------------------
# segment naming + canonical order
# --------------------------------------------------------------------------

def segment_name(now_utc: dt.datetime) -> str:
    """``observations-YYYY-MM.jsonl`` for the UTC calendar month of ``now_utc``.

    A tz-aware datetime is converted to UTC first (spec §4.1: segments are cut
    by the UTC month of the append); a naive datetime is treated as UTC."""
    if now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(dt.timezone.utc)
    return f"{SEGMENT_PREFIX}{now_utc.year:04d}-{now_utc.month:02d}.jsonl"


def segment_sort_key(name: str) -> tuple:
    """Canonical segment order key (spec §4.1): bootstrap segments first, then
    observation segments, each class lexicographic by name. Anything else sorts
    last so a stray file can never wedge before real segments."""
    if name.startswith(BOOTSTRAP_PREFIX):
        return (0, name)
    if name.startswith(SEGMENT_PREFIX):
        return (1, name)
    return (2, name)


# --------------------------------------------------------------------------
# torn-tail scan
# --------------------------------------------------------------------------

def valid_tail_offset(chunk: bytes, chunk_start: int) -> int:
    """Absolute file offset just past the last ``\\n`` in ``chunk``.

    ``chunk`` is the file's final ≤64 KiB window; ``chunk_start`` is that
    window's absolute file offset. Used by the appender to ``ftruncate`` a torn
    tail back to the last complete line (spec §4.3). When the window holds no
    newline at all, returns ``chunk_start`` (the whole window is one incomplete
    line — the appender treats a >64 KiB such window as a hard error)."""
    idx = chunk.rfind(b"\n")
    if idx == -1:
        return chunk_start
    return chunk_start + idx + 1


# --------------------------------------------------------------------------
# decode helper
# --------------------------------------------------------------------------

def iter_decoded(lines):
    """Yield ``(offset, decode_line(raw))`` for each ``(offset, raw)`` in
    ``lines`` — a thin pairing helper so the ingester can count malformed lines
    (``None`` results) while keeping their offsets for diagnostics."""
    for offset, raw in lines:
        yield offset, decode_line(raw)
