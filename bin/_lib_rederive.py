"""Pure correction-plan kernel for journal rederivation.

Task B of #372 deliberately separates two responsibilities:

* eager code replays retained truth through the current derivation hooks in a
  scratch index and produces the desired effective events; and
* this module compares those events with Task A's ``EffectiveSelection`` and
  renders a deterministic, auditable correction plan.

No filesystem, SQLite, provider, alert, or journal operation belongs here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable, Mapping


FAMILY = "claude-usage"


class RederiveError(RuntimeError):
    """Base error for a plan that cannot be produced truthfully."""


class RederiveDataGap(RederiveError):
    """Required retained source data is absent or too old for current logic."""


class RederiveConflict(RederiveError):
    """The desired event set is internally inconsistent."""


@dataclasses.dataclass(frozen=True)
class KindClassification:
    mode: str
    reason: str


_EVT_CLASSIFICATIONS = {
    "snapshot_accept": KindClassification(
        "rederived", "accepted Claude usage observation"),
    "weekly_cost_snapshot": KindClassification(
        "rederived", "as-of-bounded Claude cache cost"),
    "weekly_credit_effects": KindClassification(
        "rederived", "record-credit destructive effects"),
    "week_reset": KindClassification(
        "rederived", "Claude weekly reset or credit decision"),
    "five_hour_credit": KindClassification(
        "rederived", "Claude five-hour credit decision"),
    "five_hour_block_close": KindClassification(
        "rederived", "closed Claude five-hour projection"),
    "percent_milestone": KindClassification(
        "rederived", "dependent Claude weekly milestone"),
    "five_hour_milestone": KindClassification(
        "rederived", "dependent Claude five-hour milestone"),
    "budget": KindClassification(
        "re_materialized_projection",
        "historical budget config is not journaled; stale Claude latches retire"),
    "projected": KindClassification(
        "re_materialized_projection",
        "historical alert config is not journaled; stale Claude latches retire"),
    "project_budget": KindClassification(
        "re_materialized_projection",
        "historical project budgets are not journaled; stale latches retire"),
    "quota_alert_arming": KindClassification(
        "retained", "Codex quota lifecycle state is outside claude-usage"),
}

_OP_CLASSIFICATIONS = {
    "weekly_credit_floor": KindClassification(
        "retained_input", "operator credit truth is replayed into scratch"),
    "account_observe": KindClassification(
        "retained_input", "account ownership truth is replayed into scratch"),
    "account_label": KindClassification(
        "retained_input", "operator account labels are replayed into scratch"),
    "accounts_cutover": KindClassification(
        "retained_input",
        "legacy Claude ownership normalizes unstamped journal history"),
    "sync_week": KindClassification(
        "retained_input", "operator cost-sync request is re-executed in scratch"),
}


def _is_owned_event(record: Mapping) -> bool:
    payload = record.get("payload") or {}
    kind = payload.get("kind")
    classification = _EVT_CLASSIFICATIONS.get(kind)
    if classification is None or classification.mode not in {
        "rederived", "re_materialized_projection",
    }:
        return False
    if kind == "budget":
        return payload.get("vendor", "claude") == "claude"
    if kind == "projected":
        metric = str(payload.get("metric") or "")
        return not metric.startswith("codex_")
    return True


@dataclasses.dataclass(frozen=True)
class FamilyRegistryReport:
    family: str
    evt: Mapping[str, KindClassification]
    op: Mapping[str, KindClassification]
    unclassified_evt_kinds: tuple[str, ...]
    unclassified_op_kinds: tuple[str, ...]

    def classification_for_evt(self, kind: str) -> KindClassification:
        return self.evt[kind]

    def classification_for_op(self, kind: str) -> KindClassification:
        return self.op[kind]


def validate_family_registry(*, evt_kinds: set[str],
                             op_kinds: set[str]) -> FamilyRegistryReport:
    """Return the closure table and expose any newly-added unclassified kind."""
    return FamilyRegistryReport(
        family=FAMILY,
        evt=dict(_EVT_CLASSIFICATIONS),
        op=dict(_OP_CLASSIFICATIONS),
        unclassified_evt_kinds=tuple(sorted(evt_kinds - _EVT_CLASSIFICATIONS.keys())),
        unclassified_op_kinds=tuple(sorted(op_kinds - _OP_CLASSIFICATIONS.keys())),
    )


_SESSION_ENTRY_COLUMNS = frozenset({
    "timestamp_utc",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_create_tokens",
    "cache_read_tokens",
    "cache_create_1h_tokens",
    "source_path",
    "account_key",
})
_SESSION_FILE_COLUMNS = frozenset({"path", "session_id", "project_path"})


def validate_claude_cache_contract(tables: Mapping[str, set[str]]) -> None:
    """Fail before planning if current Claude cost inputs are not retained."""
    if "session_entries" not in tables:
        raise RederiveDataGap("missing cache.db table session_entries")
    missing_entries = sorted(
        _SESSION_ENTRY_COLUMNS - set(tables["session_entries"]))
    if missing_entries:
        raise RederiveDataGap(
            "missing cache.db session_entries column(s): "
            + ", ".join(missing_entries)
        )
    if "session_files" not in tables:
        raise RederiveDataGap("missing cache.db table session_files")
    missing_files = sorted(_SESSION_FILE_COLUMNS - set(tables["session_files"]))
    if missing_files:
        raise RederiveDataGap(
            "missing cache.db session_files column(s): " + ", ".join(missing_files)
        )


def _canonical_bytes(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _sha256(value) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _semantic_event(record: Mapping) -> dict:
    return {
        "id": record.get("id"),
        "at": record.get("at"),
        "payload": record.get("payload") or {},
    }


def _preserve_non_derivable_state(
    current_record: Mapping, desired_record: Mapping
) -> Mapping:
    """Carry durable state that scratch replay cannot truthfully reconstruct.

    Percent-milestone ``alerted_at`` is the latch recorded when the first
    crossing actually dispatched. Historical alert configuration is not
    retained, so scratch replay deliberately runs with alerts disabled and
    produces ``None``. Preserve the selected event's exact latch both when the
    rest of the milestone is retained and when another field needs an audited
    higher-revision correction (#410 Task B).
    """
    current_payload = current_record.get("payload") or {}
    desired_payload = desired_record.get("payload") or {}
    if (
        current_payload.get("kind") != "percent_milestone"
        or desired_payload.get("kind") != "percent_milestone"
        or "alerted_at" not in current_payload
    ):
        return desired_record
    merged = dict(desired_record)
    merged_payload = dict(desired_payload)
    merged_payload["alerted_at"] = current_payload["alerted_at"]
    merged["payload"] = merged_payload
    return merged


@dataclasses.dataclass(frozen=True)
class PlanAction:
    disposition: str
    event_id: str
    revision: int
    at: str
    payload: "dict | None"
    payload_hash: str

    def to_dict(self) -> dict:
        out = {
            "disposition": self.disposition,
            "eventId": self.event_id,
            "revision": self.revision,
            "at": self.at,
            "payloadHash": self.payload_hash,
        }
        if self.payload is not None:
            out["payload"] = self.payload
        return out

    def to_correction_action(self) -> dict:
        if self.disposition == "tombstone":
            return {
                "action": "tombstone",
                "id": self.event_id,
                "rev": self.revision,
                "at": self.at,
                "payload": None,
            }
        return {
            "action": "replace",
            "id": self.event_id,
            "rev": self.revision,
            "at": self.at,
            "payload": dict(self.payload or {}),
        }


@dataclasses.dataclass(frozen=True)
class RederivePlan:
    family: str
    journal_high_water: "tuple[str, int] | None"
    cache_fingerprint: str
    config_fingerprint: str
    counts: Mapping[str, int]
    actions: tuple[PlanAction, ...]
    retained_event_count: int

    def _body(self) -> dict:
        return {
            "schemaVersion": 1,
            "family": self.family,
            "journalHighWater": (
                None if self.journal_high_water is None else {
                    "segment": self.journal_high_water[0],
                    "offset": self.journal_high_water[1],
                }
            ),
            "cacheFingerprint": self.cache_fingerprint,
            "configFingerprint": self.config_fingerprint,
            "counts": dict(self.counts),
            "retainedEventCount": self.retained_event_count,
            "payloadHashes": sorted(action.payload_hash for action in self.actions),
            "actions": [action.to_dict() for action in self.actions],
        }

    @property
    def plan_hash(self) -> str:
        return _sha256(self._body())

    def to_bytes(self) -> bytes:
        body = self._body()
        body["planHash"] = self.plan_hash
        return _canonical_bytes(body)

    def to_correction_actions(self) -> list[dict]:
        return [action.to_correction_action() for action in self.actions]


def _desired_by_id(events: Iterable[Mapping]) -> dict[str, Mapping]:
    desired = {}
    for event in events:
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise RederiveConflict("desired event is missing a non-empty id")
        if not _is_owned_event(event):
            continue
        prior = desired.get(event_id)
        if prior is not None and _semantic_event(prior) != _semantic_event(event):
            raise RederiveConflict(f"divergent desired event id {event_id}")
        desired[event_id] = event
    return desired


def build_claude_usage_plan(*, selection, desired_events: Iterable[Mapping],
                            journal_high_water: "tuple[str, int] | None",
                            cache_fingerprint: str,
                            config_fingerprint: str,
                            conflicted_event_ids=frozenset()) -> RederivePlan:
    """Diff current effective events against one scratch-derived desired set.

    ``conflicted_event_ids`` (#374) names the event ids this family owns whose
    same-revision group the selector QUARANTINED at the winning revision. They
    force a ``supersede`` at ``selected.rev + 1`` **even when the provisional
    winner already equals the desired state** — without that, the equality
    branch below returns ``retain`` and the rev-0 conflict survives in the
    append-only journal forever, which is precisely the case the first design
    called already-correct. The resulting correction is a semantic no-op in
    content and a revision advance in effect, and the revision advance is what
    suppresses the rev-0 group under the selector's revision filter."""
    conflicted_event_ids = frozenset(conflicted_event_ids or ())
    desired = _desired_by_id(desired_events)
    current = {
        event_id: selected.record
        for event_id, selected in selection.by_id.items()
        if selected.status == "active"
        and selected.record is not None
        and _is_owned_event(selected.record)
    }
    retained_event_count = sum(
        1 for selected in selection.by_id.values()
        if selected.status == "active"
        and selected.record is not None
        and not _is_owned_event(selected.record)
    )
    counts = {"retain": 0, "supersede": 0, "tombstone": 0, "add": 0}
    actions: list[PlanAction] = []

    for event_id in sorted(set(current) | set(desired)):
        current_record = current.get(event_id)
        desired_record = desired.get(event_id)
        selected = selection.by_id.get(event_id)
        if current_record is not None and desired_record is not None:
            desired_record = _preserve_non_derivable_state(
                current_record, desired_record
            )
            if (
                _semantic_event(current_record) == _semantic_event(desired_record)
                and event_id not in conflicted_event_ids
            ):
                counts["retain"] += 1
                continue
            revision = int(selected.rev) + 1
            disposition = "supersede"
            at = str(desired_record["at"])
            payload = dict(desired_record.get("payload") or {})
        elif current_record is not None:
            revision = int(selected.rev) + 1
            disposition = "tombstone"
            at = str(current_record["at"])
            payload = None
        else:
            if selected is not None:
                # A formerly-tombstoned id must advance rather than attempt rev 0.
                revision = int(selected.rev) + 1
            else:
                revision = 0
            disposition = "add"
            at = str(desired_record["at"])
            payload = dict(desired_record.get("payload") or {})
        counts[disposition] += 1
        action_shape = {
            "disposition": disposition,
            "eventId": event_id,
            "revision": revision,
            "at": at,
            "payload": payload,
        }
        actions.append(PlanAction(
            disposition=disposition,
            event_id=event_id,
            revision=revision,
            at=at,
            payload=payload,
            payload_hash=_sha256(action_shape),
        ))

    return RederivePlan(
        family=FAMILY,
        journal_high_water=journal_high_water,
        cache_fingerprint=cache_fingerprint,
        config_fingerprint=config_fingerprint,
        counts=counts,
        actions=tuple(actions),
        retained_event_count=retained_event_count,
    )
