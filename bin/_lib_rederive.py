"""Pure correction-plan kernel for journal rederivation.

Task B of #372 deliberately separates two responsibilities:

* eager code replays retained truth through the current derivation hooks in a
  scratch index and produces the desired effective events; and
* this module compares those events with Task A's ``EffectiveSelection`` and
  renders a deterministic, auditable correction plan.

No filesystem, SQLite, provider, alert, or journal operation belongs here.
"""

from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence

import _lib_accounts
import _lib_journal
from _lib_cost_provenance import (
    HISTORICAL_CLOSE_BASIS, RECORDED_CLOSE_CORRECTION_BASIS,
)


FAMILY = "claude-usage"

# Prefix of a correction batch this family authored. Only its own destructive
# batches may be undone by :func:`build_claude_usage_plan` (#426) — a tombstone
# written by anyone else is a deliberate retirement and stays retired.
_FAMILY_BATCH_PREFIX = f"rederive:{FAMILY}:"

# Id shape minted by the journal cutover exporter (`_lib_journal.bootstrap_id`,
# `b:<table>:<rowid>`) for a row that predates the journal. The family's own
# derivation only ever mints natural-key ids (`sa:`, `wcs:`, `pm:`, …), and the
# cutover runs once per install, so this prefix identifies exactly the events a
# replay can never reproduce — independently of any timestamp (#426).
_CUTOVER_EXPORT_ID_PREFIX = "b:"


class RederiveError(RuntimeError):
    """Base error for a plan that cannot be produced truthfully."""


class RederiveDataGap(RederiveError):
    """Required retained source data is absent or too old for current logic."""


class RederiveConflict(RederiveError):
    """The desired event set is internally inconsistent."""


class RederivePlanGuardConflict(RederiveConflict):
    """A fully assembled plan is unsafe to preview or apply."""

    def __init__(self, plan: "RederivePlan", plan_guard: dict):
        super().__init__(
            "week-reset add burst: "
            f"{len(plan_guard['violations'])} group(s) exceed "
            f"the limit of {plan_guard['limit']}"
        )
        self.plan = plan
        self.plan_guard = plan_guard


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
    # #943 spec 4.2.2: the exact boundary companion of a five-hour credit.
    "five_hour_credit_boundary": KindClassification(
        "rederived", "Claude five-hour credit boundary"),
    # #956 spec 4.6.1: every five-hour block creation's opening fact.
    "five_hour_block_open": KindClassification(
        "rederived", "Claude five-hour block opening"),
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
    # #416 spec §7.2: terminal Codex quota alert evidence. `retained`, exactly
    # like its `quota_alert_arming` sibling — it is provider state OUTSIDE the
    # claude-usage family, replayed by its own fold applier rather than
    # re-derived into the claude-usage scratch index.
    "quota_threshold_event": KindClassification(
        "retained", "Codex terminal quota alert evidence is outside claude-usage"),
    # #661 S2 §6.4: the metering-rate-change latch. `retained`, for the same
    # reason as both siblings above — it is a durable forward-only alert
    # boundary replayed by its OWN fold applier, and nothing in the
    # claude-usage family derives it. Re-deriving it would be impossible in
    # any case: its trigger is a persistence transition on an unjournaled
    # calibration file, which is exactly why §6.5 requires the payload to be
    # self-sufficient.
    "meter_rate_change": KindClassification(
        "retained",
        "the metering-rate latch is replayed, never re-derived"),
    # #1015 spec 5.7: the terminal supersession of one metering-rate
    # transition identity. `retained` like its latch: a self-sufficient
    # payload replayed by its own natural-key fold, never re-derived.
    "meter_rate_change_supersession": KindClassification(
        "retained",
        "a transition's supersession is replayed, never re-derived"),
}

_OP_CLASSIFICATIONS = {
    "claude_weekly_observation_decision": KindClassification(
        "retained_input", "reviewed weekly-axis decisions are replayed into scratch"),
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
    # #416: the durable Codex file/range attribution decision. `retained` (not
    # `retained_input`) mirrors the `quota_alert_arming` precedent above — it is
    # provider state OUTSIDE the claude-usage family, replayed by the Codex cache
    # leg rather than into the claude-usage scratch index.
    "codex_file_account": KindClassification(
        "retained", "Codex file attribution decision is outside claude-usage"),
    # #500: operator attribution of recorded Codex quota windows. `retained`,
    # exactly like the `codex_file_account` precedent above — provider state
    # OUTSIDE the claude-usage family, replayed by the Codex cache leg rather
    # than into the claude-usage scratch index.
    "codex_window_attribution": KindClassification(
        "retained", "operator Codex window attribution is outside claude-usage"),
    "codex_window_attribution_retract": KindClassification(
        "retained", "operator Codex window retraction is outside claude-usage"),
    # #944 spec 5.10: the durable Claude cost attribution decision. `retained`
    # like `codex_file_account`: it is replayed by the Claude cache leg into
    # `claude_cost_decisions`, and re-derivation reads the decided
    # `session_entries.account_key` it materialized rather than deriving it.
    "claude_cost_account": KindClassification(
        "retained", "Claude cost attribution decisions are replayed by the "
        "cache leg, never re-derived"),
    # #825/#832 spec 5.1. The versioned reviewed decision is replayed into
    # scratch exactly like its previous-release twin.
    "claude_weekly_observation_decision_v2": KindClassification(
        "retained_input",
        "reviewed weekly-axis decisions are replayed into scratch"),
    # The account-bound protocol ops are fence metadata: they decide what is
    # admitted and anchor the legacy spool baseline, and nothing in the
    # claude-usage family derives them.
    "account_bound_cutover": KindClassification(
        "retained", "the account-bound journal fence is replayed, never "
        "re-derived"),
    "legacy_spool_activity": KindClassification(
        "retained", "legacy spool evidence is replayed, never re-derived"),
    # The versioned journal-repair acknowledgement is an audit record over the
    # raw prefix, outside claude-usage (its previous-release twin is consumed
    # by the selector directly and never registered).
    "journal_protocol_resolution_v2": KindClassification(
        "retained", "journal-repair acknowledgements are audit records"),
    # #797/#823/#840 spec 4.6 B2, B5: the revision-2 boundary is fence
    # metadata like the cutover, and the v3 acknowledgement is the v2's
    # successor.
    "account_bound_revision": KindClassification(
        "retained", "the revision-2 journal fence is replayed, never "
        "re-derived"),
    "journal_protocol_resolution_v3": KindClassification(
        "retained", "journal-repair acknowledgements are audit records"),
    # #1019/#1014 spec 4.7 C2, C5: the revision-3 boundary and the v4
    # acknowledgement, likewise.
    "account_bound_revision_v3": KindClassification(
        "retained", "the revision-3 journal fence is replayed, never "
        "re-derived"),
    "journal_protocol_resolution_v4": KindClassification(
        "retained", "journal-repair acknowledgements are audit records"),
    # #948 spec 6.2: the outcome quarantine's recovery ops are preserved
    # machinery. The fold replays them in journal order; nothing in the
    # claude-usage family derives them.
    "legacy_spool_inventory_completion": KindClassification(
        "retained", "inventory-episode completions are replayed by the "
        "quarantine fold, never re-derived"),
    "outcome_authority_completion": KindClassification(
        "retained", "authority completions are replayed by the quarantine "
        "fold, never re-derived"),
    "outcome_axis_release": KindClassification(
        "retained", "axis releases are replayed by the quarantine fold, "
        "never re-derived"),
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
        return (
            payload.get("vendor", "claude") == "claude"
            or (payload.get("vendor") == "codex"
                and (payload.get("_pricing") or {}).get("version") == 1)
        )
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


# #874: the contract is structural. It names every column any planner reader
# of the table reads (`_validate_cache_rows`, `_cache_fingerprint`,
# `_joined_entries`, `iter_entries`), not only those the current journal
# reaches, so whether a cache is accepted never depends on journal or pricing
# state, and a missing column is a typed gap rather than a raw sqlite error.
_SESSION_ENTRY_COLUMNS = frozenset({
    "id",
    "source_path",
    "line_offset",
    "timestamp_utc",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_create_tokens",
    "cache_read_tokens",
    "cache_create_1h_tokens",
    "cost_usd_raw",
    "speed",
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


# #874: the Codex columns `_cache_fingerprint(include_codex=True)` and the
# Codex budget re-pricing reader (`iter_codex_entries`) read. Required only
# when the planner reads the Codex cache at all.
_CODEX_SESSION_ENTRY_COLUMNS = frozenset({
    "source_path",
    "line_offset",
    "timestamp_utc",
    "session_id",
    "model",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "account_key",
})


def validate_codex_cache_contract(tables: Mapping[str, set[str]]) -> None:
    """Fail before planning if the Codex cost inputs the planner reads are gone."""
    if "codex_session_entries" not in tables:
        raise RederiveDataGap("missing cache.db table codex_session_entries")
    missing = sorted(
        _CODEX_SESSION_ENTRY_COLUMNS - set(tables["codex_session_entries"]))
    if missing:
        raise RederiveDataGap(
            "missing cache.db codex_session_entries column(s): "
            + ", ".join(missing)
        )


def preserved_history(records: Iterable[Mapping], *,
                      evidence_retained: bool) -> dict[str, Mapping]:
    """Owned events the current re-derivation cannot reach, keyed by event id.

    Two independent, deterministic reasons an event can have no retained source
    behind it — so a replay-derived desired set can never contain it:

    * its id was minted by the cutover exporter (``b:<table>:<rowid>``). The
      journal only starts recording observations AT the cutover, so those
      exported lines ARE the pre-journal truth; the family's own derivation
      mints natural keys and can never produce them.
    * nothing is retained at all (``evidence_retained=False``) — every desired
      set is then empty, so a diff could only ever be destructive.

    Diffing such an event anyway put it in the "current but not desired" branch
    and TOMBSTONED it: one ``db rederive --yes`` retired every pre-cutover
    weekly usage/cost snapshot on a real install (#426). Those events are
    preserved instead — never tombstoned, never rewritten from a re-derivation
    that does not cover them. Everything the retained observations DO cover
    still diffs normally, so an obsolete derivation still retires.

    The returned record is the highest-revision non-tombstone line for that id
    — a rev-0 evt, or a replacement from a committed correction batch — i.e.
    exactly the payload the selector would choose if no tombstone existed.
    """
    committed = {
        record.get("id") for record in records
        if record.get("t") == "correction_batch"
        and record.get("phase") == "commit"
    }
    best: dict[str, tuple[int, int, Mapping]] = {}
    for sequence, record in enumerate(records):
        if record.get("t") == "evt":
            candidate = record
        elif (
            record.get("t") == "correction"
            and record.get("action") == "replace"
            and record.get("batch") in committed
        ):
            candidate = {
                "v": record.get("v"),
                "t": "evt",
                "id": record.get("id"),
                "rev": record.get("rev", 0),
                "at": record.get("at"),
                "src": "ingest",
                "payload": dict(record.get("payload") or {}),
            }
        else:
            continue
        event_id = candidate.get("id")
        if not isinstance(event_id, str) or not event_id:
            continue
        if not _is_owned_event(candidate):
            continue
        if _is_codex_budget(candidate):
            # #869: a Codex crossing's desired state comes only from its own
            # recorded provenance, never from Claude replay evidence, so it is
            # never "un-re-derivable history". Holding one here would retain a
            # crossing the correction retires below its threshold, and would
            # revive that intentionally retired crossing on the next run.
            continue
        if evidence_retained and not event_id.startswith(
            _CUTOVER_EXPORT_ID_PREFIX
        ):
            continue  # the retained observations can re-derive it
        revision = candidate.get("rev", 0)
        if not isinstance(revision, int):
            revision = 0
        prior = best.get(event_id)
        if prior is None or (revision, sequence) >= (prior[0], prior[1]):
            best[event_id] = (revision, sequence, candidate)
    return {event_id: entry[2] for event_id, entry in best.items()}


def _is_codex_budget(record: Mapping) -> bool:
    payload = record.get("payload") or {}
    return payload.get("kind") == "budget" and payload.get("vendor") == "codex"


_BLOCK_TOKEN_KEYS = (
    "input_tokens", "output_tokens", "cache_create_tokens", "cache_read_tokens",
)
#: The monetary fields a five-hour milestone snapshots from its block.
_FIVE_HOUR_MILESTONE_MONETARY_KEYS = frozenset({
    "block_cost_usd", "marginal_cost_usd",
})
#: Milestone kinds whose ``alerted_at`` records an actual dispatch. Scratch
#: replay runs without alert config, so it can never reproduce that latch.
_ALERT_LATCHED_MILESTONE_KINDS = frozenset({
    "percent_milestone", "five_hour_milestone",
})


def _positive_token_models(payload: Mapping) -> set:
    return {
        child.get("model")
        for child in payload.get("_models", [])
        if any(int(child.get(key) or 0) > 0 for key in _BLOCK_TOKEN_KEYS)
    }


def _child_population(payload: Mapping, key: str) -> list:
    """A close's child set without its monetary field, order-independent."""
    return sorted(
        [
            {k: v for k, v in child.items() if k != "cost_usd"}
            for child in payload.get(key, [])
        ],
        key=lambda item: json.dumps(item, sort_keys=True),
    )


def _same_child_population(current: Mapping, desired: Mapping) -> bool:
    return (
        _child_population(current, "_models")
        == _child_population(desired, "_models")
        and _child_population(current, "_projects")
        == _child_population(desired, "_projects")
    )


def close_cost_is_uncertain(payload: Mapping) -> bool:
    """A closed block's retained cost lacks proof that every model was priced.

    A computation-time marker settles it except for an ambiguous model, or a
    missing-card model beside a priced one (project children have no model
    axis). An unmarked close is uncertain only while a positive-token model
    carries a zero or unresolved cost; a historical correction marker (#869)
    is proof in itself.
    """
    pricing = payload.get("_pricing")
    positive = _positive_token_models(payload)
    if pricing:
        if pricing.get("basis") == HISTORICAL_CLOSE_BASIS:
            return False
        missing = set(pricing.get("unpricedModels") or [])
        return bool(
            pricing.get("uncertainModels")
            or (missing and missing != positive)
        )
    return any(
        child.get("model") in positive and not child.get("cost_usd")
        for child in payload.get("_models", [])
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


def _with_close_marker(record: Mapping, marker: Mapping) -> dict:
    """A close record carrying ``marker`` in both of its provenance fields."""
    corrected = dict(record)
    payload = dict(record.get("payload") or {})
    payload["_pricing"] = dict(marker)
    payload["pricing_provenance_json"] = json.dumps(
        dict(marker), sort_keys=True, separators=(",", ":"),
    )
    corrected["payload"] = payload
    return corrected


#: The non-``total_`` members of a close's money group (#1040 spec §5.2).
_CLOSE_MONEY_EXTRA_KEYS = frozenset({
    "_models", "_projects", "_pricing", "pricing_provenance_json",
})


def _close_money_keys(payload: Mapping) -> frozenset:
    """#1040 (spec §5.2): the keys of a close payload's money group.

    Every key starting with ``total_`` plus the child sets and both pricing
    provenance fields: the partition :func:`_preserve_non_derivable_state`
    compares the closure's non-monetary structure without.
    """
    return frozenset(
        key for key in payload
        if key in _CLOSE_MONEY_EXTRA_KEYS
        or (isinstance(key, str) and key.startswith("total_"))
    )


def close_money_overlay(desired: Mapping, current: Mapping) -> dict:
    """#1040 (spec §5.2): ``overlay(D, C)`` on whole records.

    ``desired`` with ``current``'s money group: every money key of D is
    replaced by C's, and a money key C lacks is removed.
    """
    desired_payload = desired.get("payload") or {}
    current_payload = current.get("payload") or {}
    drop = _close_money_keys(desired_payload)
    payload = {key: value for key, value in desired_payload.items()
               if key not in drop}
    for key in _close_money_keys(current_payload):
        payload[key] = current_payload[key]
    return {**desired, "payload": payload}


_CLOSE_POPULATION_TOTALS = (
    "total_input_tokens", "total_output_tokens", "total_cache_create_tokens",
    "total_cache_read_tokens",
)


def close_population(payload: Mapping) -> tuple:
    """#1040 (spec §5.2): the card-independent part of a close's money group.

    The four block token totals, and per ``_models`` / ``_projects`` child its
    model or project path, account key, four token counts and entry count,
    order-independent. Compared exactly.
    """
    def children(key: str, name: str) -> tuple:
        rows = [
            (child.get(name), child.get("account_key"),
             *(child.get(token) for token in _BLOCK_TOKEN_KEYS),
             child.get("entry_count"))
            for child in payload.get(key) or ()
            if isinstance(child, Mapping)
        ]
        return tuple(sorted(rows, key=lambda row: json.dumps(
            row, sort_keys=True, default=repr)))

    return (
        tuple(payload.get(key) for key in _CLOSE_POPULATION_TOTALS),
        children("_models", "model"),
        children("_projects", "project_path"),
    )


def _preserve_non_derivable_state(
    current_record: Mapping, desired_record: Mapping, *,
    historical_marker: "Mapping | None" = None,
    drifted_ids: "set | None" = None,
) -> Mapping:
    """Carry durable state that scratch replay cannot truthfully reconstruct.

    A percent or five-hour milestone's ``alerted_at`` is the latch recorded
    when the first crossing actually dispatched. Historical alert
    configuration is not retained, so scratch replay deliberately runs with
    alerts disabled and produces ``None``. Preserve the selected event's exact
    latch both when the rest of the milestone is retained and when another
    field needs an audited higher-revision correction (#410 Task B; five-hour
    milestones since #869 V3).

    #869: ``historical_marker`` is the planner's proof that an UNMARKED close
    priced every positive-token model at $0 only for want of a card; the close
    then adopts the replay under that marker. A marked or historically proven
    close whose replayed child population differs is kept unchanged and its id
    added to ``drifted_ids``. A close corrected from its recorded missing-card
    marker is stamped with its own basis, ``RECORDED_CLOSE_CORRECTION_BASIS``.

    #875: the milestones of a frozen or replaced close are decided by
    :func:`_resolve_closed_block_milestones`, not here: an unchanged crossing
    keeps its selected block cost there unless its lineage proves an earlier
    rederive repriced it (#884). This function only carries a milestone's
    ``alerted_at`` latch.
    """
    current_payload = current_record.get("payload") or {}
    desired_payload = desired_record.get("payload") or {}
    if (
        current_payload.get("kind") == "weekly_cost_snapshot"
        and desired_payload.get("kind") == "weekly_cost_snapshot"
        and current_record.get("at") == desired_record.get("at")
    ):
        # A live ingest writes range bounds in the configured display zone,
        # while scratch replay can recover the same instants with the source
        # session's offset. Offset spelling is not a cost correction. Keep the
        # selected payload byte-for-byte when both bounds name the same
        # instants and every other priced fact agrees.
        def cost_structure(payload):
            facts = dict(payload)
            for field in ("range_start_iso", "range_end_iso"):
                instant = _iso_instant(facts.get(field))
                if instant is not None:
                    facts[field] = instant.isoformat()
            return facts

        if cost_structure(current_payload) == cost_structure(desired_payload):
            return current_record
    if (
        current_payload.get("kind") == "five_hour_block_open"
        and desired_payload.get("kind") == "five_hour_block_open"
        and current_record.get("at") == desired_record.get("at")
    ):
        # #956: the live writer stamps ``block_start_at`` in the host's zone,
        # and a replay under another zone spells the same boundary with its
        # own offset. An opening is immutable once written; a different
        # spelling of the same instants is not a revision to supersede.
        def opening_structure(payload):
            facts = dict(payload)
            for field in (
                "block_start_at", "five_hour_resets_at",
                "first_observed_at_utc", "created_at_utc",
                "weekly_basis_captured_at_utc",
            ):
                instant = _iso_instant(facts.get(field))
                if instant is not None:
                    facts[field] = instant.isoformat()
            return facts

        if opening_structure(current_payload) == opening_structure(
                desired_payload):
            return current_record
    if (
        current_payload.get("kind") == "five_hour_block_close"
        and desired_payload.get("kind") == "five_hour_block_close"
        and current_record.get("at") == desired_record.get("at")
        and current_payload.get("is_closed") == 1
        and desired_payload.get("is_closed") == 1
    ):
        # The close event is the monetary freeze boundary. A newer ownership
        # rule or a later cache prefix may reprice its scratch projection, but
        # cannot revise the already-journaled parent/child totals. Compare the
        # closure's non-monetary evidence first so a changed boundary or final
        # reading still reaches the normal correction path.
        def structure(payload):
            money = _close_money_keys(payload)
            facts = {
                key: value for key, value in payload.items()
                if key not in money
            }
            # Live close used the display-zone offset; scratch replay writes
            # UTC. Both name one frozen boundary and must compare as an instant.
            start = facts.get("block_start_at")
            if isinstance(start, str):
                try:
                    parsed = dt.datetime.fromisoformat(
                        start.replace("Z", "+00:00"))
                except ValueError:
                    pass
                else:
                    if parsed.tzinfo is not None:
                        facts["block_start_at"] = parsed.astimezone(
                            dt.timezone.utc).isoformat()
            return facts

        if structure(current_payload) == structure(desired_payload):
            current_pricing = current_payload.get("_pricing") or {}
            desired_pricing = desired_payload.get("_pricing") or {}

            def keep_drifted():
                # #869 F3: a changed retained population cannot prove the
                # correction, but it does not falsify the recorded close
                # either. Keep this close and disclose it; never refuse the
                # family's other corrections over it.
                if drifted_ids is not None:
                    drifted_ids.add(current_record.get("id"))
                return current_record

            if (
                not current_pricing
                and current_payload.get("pricing_provenance_json") is None
                and historical_marker is not None
            ):
                # #869 F2: an unmarked historical close the retained entries
                # prove was $0 only because its model had no card. Adopt the
                # replayed parent and BOTH child sets together, under a marker
                # whose distinct basis freezes the result against later cards.
                if not _same_child_population(current_payload, desired_payload):
                    return keep_drifted()
                return _with_close_marker(desired_record, historical_marker)
            missing = current_pricing.get("unpricedModels") or []
            if missing:
                # A closed amount is frozen unless its own durable close event
                # says a model had no direct card. Verify the replay covers the
                # same token population and now has direct cards for every
                # missing model before allowing a monetary correction.
                # The retained project children do not contain a model axis.
                # With both direct and missing-card models, scratch replay
                # cannot preserve the direct model's at-close contribution in
                # every project. Leave that entire close unchanged.
                if (
                    set(missing) != _positive_token_models(current_payload)
                    or current_pricing.get("uncertainModels")
                ):
                    return current_record
                if any(
                    model in (desired_pricing.get("unpricedModels") or [])
                    for model in missing
                ):
                    return current_record
                if not _same_child_population(current_payload, desired_payload):
                    return keep_drifted()
                # #869 V4: record that this close was CORRECTED. The basis is
                # provenance only: since #875 every frozen close freezes the
                # block cost of its unchanged five-hour crossings (save a
                # #884 history-proven restoration), whatever its basis. A
                # genuinely priced close's marker looks the same otherwise.
                return _with_close_marker(desired_record, {
                    **desired_pricing, "basis": RECORDED_CLOSE_CORRECTION_BASIS,
                })
            return current_record
    if (
        current_payload.get("kind") in _ALERT_LATCHED_MILESTONE_KINDS
        and desired_payload.get("kind") == current_payload.get("kind")
        and "alerted_at" in current_payload
    ):
        # The recorded dispatch latch survives any correction of the rest of
        # the milestone (#410 Task B; five-hour milestones since #869 V3).
        merged = dict(desired_record)
        merged_payload = dict(desired_payload)
        merged_payload["alerted_at"] = current_payload["alerted_at"]
        merged["payload"] = merged_payload
        desired_record, desired_payload = merged, merged_payload
    return desired_record


#: A resolved five-hour marginal within this many USD of its reference keeps
#: the reference's exact float, so float spelling never creates a correction
#: and a second rederive stays a no-op (#875).
_MARGINAL_TOLERANCE_USD = 1e-9
#: A dependency reference that names no event. For ``reset_event_ref`` these
#: are the spellings of the pre-credit ``"0"`` segment.
_EMPTY_REFERENCE_SENTINELS = frozenset({None, 0, "0", ""})


def _milestone_segment_key(payload: Mapping) -> tuple:
    """A five-hour milestone's credit segment: account, window, reset ref.

    ``"0"`` (and every empty spelling) is the pre-credit segment.
    """
    reset_ref = payload.get("reset_event_ref")
    if reset_ref in _EMPTY_REFERENCE_SENTINELS:
        reset_ref = "0"
    return (
        payload.get("account_key"),
        payload.get("five_hour_window_key"),
        str(reset_ref),
    )


def _milestone_identity(payload: Mapping) -> dict:
    """A five-hour milestone's non-monetary identity."""
    return {
        key: value for key, value in payload.items()
        if key not in _FIVE_HOUR_MILESTONE_MONETARY_KEYS
    }


def _five_hour_block(payload: Mapping) -> tuple:
    """The ``(account_key, five_hour_window_key)`` block a payload belongs to."""
    return (payload.get("account_key"), payload.get("five_hour_window_key"))


def _usable_threshold(value):
    """A milestone threshold the marginal rule can order, or ``None``.

    Only an ``int`` qualifies. ``int()`` would read ``True``, ``1.0`` and
    ``"1"`` as threshold 1 and make a malformed row a predecessor.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _usable_amount(value) -> bool:
    """True for a finite ``int`` or ``float`` that is not a ``bool``.

    An ``int`` too large for a float is unusable rather than an error, so a
    malformed row can never abort the plan (spec §4.3).
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


#: A close correction that moves ``total_cost_usd`` by at most this many USD
#: keeps the close's money, so its block stays frozen (#875, spec §4.1).
_CLOSE_TOTAL_TOLERANCE_USD = 1e-9


def _close_keeps_its_money(current_payload: Mapping,
                           final_payload: Mapping) -> bool:
    """#875 (spec §4.1): a close correction that leaves the block's money.

    True when the current close and the one the plan leaves are both closed
    and both totals are usable (a finite ``int`` or ``float`` that is not a
    ``bool``) within 1e-9 USD of each other. The reviewed-hold helper's
    weekly-axis update is the known case: it keeps the current payload and
    replaces only the block's weekly axes. Such a correction is FROZEN like a
    kept close. A correction that closes an open block, or whose totals are
    unusable or differ by more, writes the close's money and REPLACES it.
    """
    current_total = current_payload.get("total_cost_usd")
    final_total = final_payload.get("total_cost_usd")
    return (
        current_payload.get("is_closed") == 1
        and final_payload.get("is_closed") == 1
        and _usable_amount(current_total)
        and _usable_amount(final_total)
        and abs(final_total - current_total) <= _CLOSE_TOTAL_TOLERANCE_USD
    )


def _revived_by_this_family(selected) -> bool:
    """A preserved event this family's own earlier batch retired (#426)."""
    return (
        selected is not None
        and selected.status == "tombstone"
        and str(selected.batch_id or "").startswith(_FAMILY_BATCH_PREFIX)
    )


# ── #943/#956 spec 4.4: five-hour credit companions and block parents ──────
#
# A five-hour credit (`five_hour_credit`, `fhc`) and its exact-boundary
# companion (`five_hour_credit_boundary`, `fhcb`) share one natural key, and
# the companion is read only while its `credit_revision` matches the credit,
# so a plan that acts on one without the other splits them. A five-hour
# milestone's `block_id` is a DERIVED foreign key, resolved at fold from its
# `(account_key, five_hour_window_key)`, so it depends on whatever
# materializes that window's block: the opening fact (`five_hour_block_open`,
# `fhbo`), the close fact (`five_hour_block_close`, `fhbc`), or the rebuild's
# legacy candidate rule for an unseeded open block.

_CREDIT_PAIR_KINDS = frozenset({"five_hour_credit", "five_hour_credit_boundary"})
_BLOCK_PARENT_KINDS = frozenset({"five_hour_block_open", "five_hour_block_close"})
_OPENING_ID_PREFIX = "fhbo:"


def _window_of(payload: Mapping):
    """``(account_key, int(five_hour_window_key))`` of a payload, or None."""
    window = payload.get("five_hour_window_key")
    if window is None or isinstance(window, bool):
        return None
    try:
        return (payload.get("account_key"), int(window))
    except (TypeError, ValueError):
        return None


def _credit_pair_key(payload: Mapping) -> tuple:
    """The natural key a credit and its companion share."""
    return (payload.get("account_key"), payload.get("five_hour_window_key"),
            payload.get("effective_reset_at_utc"))


def _action_payload(action, current_events: Mapping[str, Mapping]) -> Mapping:
    """An action's payload; a tombstone's comes from its current record."""
    if action.disposition == "tombstone":
        return (current_events.get(action.event_id) or {}).get("payload") or {}
    return action.payload or {}


def _opening_window(event_id: str):
    """The window of an `fhbo` id (``fhbo:<account_key>:<window_key>``)."""
    if not event_id.startswith(_OPENING_ID_PREFIX):
        return None
    account_key, _sep, window = event_id[len(_OPENING_ID_PREFIX):].rpartition(":")
    try:
        return (account_key, int(window))
    except ValueError:
        return None


def retired_openings_of(selection) -> frozenset:
    """The windows whose opening fact the effective selection tombstones.

    The rebuild never makes such a window a legacy open-block candidate
    (`_cctally_journal.retired_block_openings`, spec 4.6.3)."""
    retired = set()
    for event_id, selected in selection.by_id.items():
        if selected.status != "tombstone":
            continue
        window = _opening_window(str(event_id))
        if window is not None:
            retired.add(window)
    return frozenset(retired)


def five_hour_parent_presence(events: Mapping[str, Mapping],
                              retired_openings=frozenset()) -> frozenset:
    """The `(account_key, five_hour_window_key)` windows a rebuild of the
    effective set ``events`` materializes a block for (spec 4.4, 4.6.3).

    A window has a block when an opening or close fact names it, and, for
    each account, when it is the window of the account's latest-captured
    five-hour reading (`unixepoch(captured_at_utc) DESC, journal_id DESC`)
    and no block of it exists and its opening was not retired: the legacy
    candidate rule of `_cctally_five_hour._backfill_five_hour_blocks`. A
    reading a five-hour credit's suppression names carries no five-hour
    reading once folded (retired in place, or deleted when held), so it never
    qualifies. A weekly suppression deletes a row that carries no five-hour
    reading, which never qualifies, and a `record-credit` synthetic, which
    is counted here although the fold never sees it (#943 review R-5). The
    two differ only when that synthetic is the account's latest reading
    and lies in another window than its next surviving reading; it copies
    the window of the latest reading (`_resolve_prior_5h`), so that does
    not arise in practice."""
    present = set()
    five_hour_suppressed = set()
    for record in events.values():
        payload = (record or {}).get("payload") or {}
        kind = payload.get("kind")
        if kind in _BLOCK_PARENT_KINDS:
            window = _window_of(payload)
            if window is not None:
                present.add(window)
        elif (kind == "five_hour_credit"
              and payload.get("suppression_table", "weekly_usage_snapshots")
              == "weekly_usage_snapshots"
              and isinstance(payload.get("suppression"), list)):
            five_hour_suppressed.update(
                item for item in payload["suppression"]
                if isinstance(item, str))
    latest: dict = {}
    for event_id, record in events.items():
        payload = (record or {}).get("payload") or {}
        if (payload.get("kind") != "snapshot_accept"
                or event_id in five_hour_suppressed
                or payload.get("five_hour_percent") is None):
            continue
        window = _window_of(payload)
        instant = _iso_instant(payload.get("captured_at_utc"))
        if window is None or instant is None:
            continue
        rank = (instant.timestamp() // 1, str(event_id))
        prior = latest.get(window[0])
        if prior is None or rank > prior[0]:
            latest[window[0]] = (rank, window)
    retired = frozenset(retired_openings or ())
    for _rank, window in latest.values():
        if window not in retired:
            present.add(window)
    return frozenset(present)


def _post_plan_state(current: Mapping[str, Mapping], actions,
                     retired_openings) -> tuple[dict, frozenset]:
    """The effective set and the retired openings after ``actions``."""
    after = dict(current)
    retired = set(retired_openings or ())
    for action in actions:
        window = _opening_window(action.event_id)
        if action.disposition == "tombstone":
            after.pop(action.event_id, None)
            if window is not None:
                retired.add(window)
        else:
            after[action.event_id] = {"payload": dict(action.payload or {})}
            if window is not None:
                retired.discard(window)
    return after, frozenset(retired)


def five_hour_parent_report(current: Mapping[str, Mapping], actions,
                            retired_openings=frozenset()):
    """Parent validation over the resulting effective graph (spec 4.4).

    Returns ``(orphaned_before, lost)``: the current five-hour milestones
    whose window resolves no block BEFORE the plan (pre-existing damage,
    reported, never a reason to refuse), and the milestones that resolve a
    block before the plan and survive it without one."""
    before = five_hour_parent_presence(current, retired_openings)
    after_events, after_retired = _post_plan_state(
        current, actions, retired_openings)
    after = five_hour_parent_presence(after_events, after_retired)
    orphaned_before = set()
    lost = []
    for event_id, record in sorted(current.items()):
        payload = (record or {}).get("payload") or {}
        if payload.get("kind") != "five_hour_milestone":
            continue
        if _window_of(payload) not in before:
            orphaned_before.add(event_id)
            continue
        survivor = after_events.get(event_id)
        if survivor is None:
            continue
        if _window_of(survivor.get("payload") or {}) not in after:
            lost.append(event_id)
    return frozenset(orphaned_before), lost


def _companions_follow_kept_credits(current: Mapping[str, Mapping],
                                    desired: dict[str, Mapping]) -> None:
    """A credit the target keeps as its CURRENT record (a reviewed hold's
    dependency, :func:`_preserve_reviewed_hold_dependencies`) keeps its
    current companion with it: the companion the replay derived is bound to
    the replay's credit content, not the one kept, and a current companion
    with no desired twin would otherwise be tombstoned beside its credit."""
    companions = {}
    for source in (current, desired):
        for event_id, record in source.items():
            payload = (record or {}).get("payload") or {}
            if payload.get("kind") == "five_hour_credit_boundary":
                companions.setdefault(_credit_pair_key(payload), set()).add(
                    event_id)
    for event_id, record in current.items():
        payload = (record or {}).get("payload") or {}
        if (payload.get("kind") != "five_hour_credit"
                or desired.get(event_id) is not record):
            continue
        for companion_id in companions.get(_credit_pair_key(payload), ()):
            kept = current.get(companion_id)
            if kept is None:
                desired.pop(companion_id, None)
            else:
                desired[companion_id] = kept


# ── #884: lineage repair of five-hour milestones an earlier rederive priced ─
#
# #875 freezes the block cost of an unchanged crossing under a frozen close,
# so a later card no longer reprices it. A pre-#875 rederive may already have
# repriced one; the freeze would then keep that later card's price for good.
# The lineage index reads each milestone's admissible journal history beside
# its block's close history, proves which revision last legitimately set the
# block cost (its ANCHOR), and lets the resolver restore that amount. Any
# ambiguity keeps the current amount and is disclosed (spec §5.1–§5.7).

#: §5.2: an event's own history is uncertain.
LINEAGE_HIDDEN_REVISION = "hidden-revision"
LINEAGE_REVISION_GAP = "revision-gap"
LINEAGE_DIVERGENT_REVISION = "divergent-revision"
LINEAGE_INVALID_PAYLOAD = "invalid-payload"
LINEAGE_ORDER_INVERSION = "order-inversion"
LINEAGE_SELECTION_MISMATCH = "selection-mismatch"
#: §5.3/§5.4: the walk cannot prove the anchor.
LINEAGE_CLOSE_LINEAGE_UNCERTAIN = "close-lineage-uncertain"
LINEAGE_CLOSE_PAIRING_UNCERTAIN = "close-pairing-uncertain"
LINEAGE_REWRITE_WITHOUT_CLOSED_BOUNDARY = "rewrite-without-closed-boundary"
LINEAGE_UNPAIRED_CLOSE_CHANGE = "unpaired-close-change"

_LINEAGE_KINDS = frozenset({"five_hour_milestone", "five_hour_block_close"})


@dataclasses.dataclass(frozen=True)
class LineageOccurrence:
    """One admissible candidate of a milestone or close, summarized (§5.1).

    ``available_at`` is the journal coordinate at which it became effective:
    a rev-0 event's own sequence, or its batch's commit sequence. Milestone
    fields are ``None`` for a close and vice versa; a tombstone carries none.
    ``identity`` is :func:`_milestone_identity` without ``alerted_at``.
    ``close_view`` is an active close's canonical JSON encoding of what the
    close timeline reads (§5.2, §5.3): ``kind``, the block key, ``is_closed``
    and ``total_cost_usd``, each type-sensitive (``1``, ``1.0`` and ``true``
    differ); an absent key and an explicit ``null`` encode alike.
    """
    rev: int
    status: str
    content_hash: str
    batch_id: "str | None"
    available_at: int
    kind: "str | None"
    block: "tuple | None"
    at: object = None
    identity: "dict | None" = None
    block_cost_usd: object = None
    is_closed: object = None
    total_cost_usd: object = None
    close_view: "bytes | None" = None


@dataclasses.dataclass(frozen=True)
class Restoration:
    """One unchanged crossing's lineage answer (§5.4): ``keep``,
    ``withhold`` (fail closed) or ``restore`` to ``value``.

    ``reported`` is §5.7's predicate: a withhold counts and is disclosed only
    when the selected crossing has a repair candidate, the revisions listed in
    ``candidate_revisions``.
    """
    action: str
    value: object = None
    reason_codes: tuple = ()
    candidate_revisions: tuple = ()
    reported: bool = False


_LINEAGE_KEEP = Restoration("keep")


def _lineage_identity(payload: Mapping) -> dict:
    """§5.1: a milestone's non-monetary identity without its alert latch."""
    return {
        key: value for key, value in payload.items()
        if key not in _FIVE_HOUR_MILESTONE_MONETARY_KEYS
        and key != "alerted_at"
    }


def _divergence_key(occurrence, *, close: bool):
    """§5.2: what two occurrences of one revision must share to agree.

    A milestone's occurrences agree only on the same ``(status,
    content_hash)``. A close's agree on what §5.3 reads: a tombstone on its
    status alone, an active occurrence on its status and ``close_view``
    (compared as bytes, so no value is coerced or assumed hashable). A
    divergence confined to a close's observation fields, weekly axes,
    tokens, children or markers changes no close state or event (finding
    F1, the live writer's rev-0 re-emissions).
    """
    if not close:
        return (occurrence.status, occurrence.content_hash)
    if occurrence.status != "active":
        return (occurrence.status,)
    return (occurrence.status, occurrence.close_view)


def _history_uncertainty(occurrences, *, hidden: bool, invalid: bool,
                         selected, close: bool = False) -> tuple:
    """§5.2: the sorted reason codes of one event's own uncertain history.

    ``close`` marks a close id, whose same-revision occurrences are compared
    on the fields the close timeline reads (:func:`_divergence_key`); every
    other code, ``selection-mismatch``'s full-hash check included, is the
    same for both kinds.
    """
    codes = set()
    if hidden:
        codes.add(LINEAGE_HIDDEN_REVISION)
    if invalid:
        codes.add(LINEAGE_INVALID_PAYLOAD)
    by_rev: dict[int, list] = {}
    for occurrence in occurrences:
        by_rev.setdefault(occurrence.rev, []).append(occurrence)
    top = max(by_rev)
    upper = top if selected is None else max(top, selected[0])
    if any(rev not in by_rev for rev in range(upper + 1)):
        codes.add(LINEAGE_REVISION_GAP)
    earliest = {}
    for rev, group in by_rev.items():
        if len({_divergence_key(item, close=close) for item in group}) > 1:
            codes.add(LINEAGE_DIVERGENT_REVISION)
        earliest[rev] = min(item.available_at for item in group)
    if any(
        rev - 1 in earliest and earliest[rev] < earliest[rev - 1]
        for rev in earliest
    ):
        codes.add(LINEAGE_ORDER_INVERSION)
    if (
        selected is None or selected[0] != top
        or selected[1] not in {item.content_hash for item in by_rev[top]}
    ):
        codes.add(LINEAGE_SELECTION_MISMATCH)
    return tuple(sorted(codes))


def _first_per_revision(occurrences) -> dict:
    """``{rev: earliest occurrence}``; ``occurrences`` are sorted."""
    chain: dict[int, LineageOccurrence] = {}
    for occurrence in occurrences:
        chain.setdefault(occurrence.rev, occurrence)
    return chain


@dataclasses.dataclass(frozen=True)
class FiveHourLineage:
    """§5.1: the compact lineage of every five-hour milestone and close.

    Holds summaries only, never records or the fold. ``uncertain`` maps each
    id whose own history is uncertain (§5.2) to its reason codes.
    ``close_ids_ever_in_block`` is a candidate index; membership at a
    coordinate is decided from the winning payload there (§5.3).
    """
    milestones: Mapping[str, tuple]
    closes: Mapping[str, tuple]
    close_ids_ever_in_block: Mapping[tuple, tuple]
    hidden_ids: frozenset
    selected: Mapping[str, tuple]
    uncertain: Mapping[str, tuple]
    _block_timelines: dict = dataclasses.field(
        default_factory=dict, compare=False, repr=False)
    #: #1040: memoized journal end of the summarized occurrences.
    _coordinate_end: list = dataclasses.field(
        default_factory=list, compare=False, repr=False)

    def _block_timeline(self, block) -> tuple:
        """§5.3: K's close events, ``(coordinates, states, changed,
        live_writer)``.

        Only called when every close id of the block has a certain history,
        so each id's revision becomes effective at its earliest occurrence.
        A state is ``("ambiguous",)``, ``("absent",)``, ``("open",)`` or
        ``("closed", total)``. ``live_writer`` marks a coordinate that only
        rev-0 ``evt`` occurrences (no correction batch) make effective
        (§5.4 step 3's first live-writer close).
        """
        cached = self._block_timelines.get(block)
        if cached is not None:
            return cached
        chains = {
            event_id: _first_per_revision(self.closes[event_id])
            for event_id in self.close_ids_ever_in_block.get(block, ())
        }
        coordinates = sorted({
            occurrence.available_at
            for event_id in chains
            for occurrence in self.closes[event_id]
        })
        states = []
        changed = []
        live_writer = []
        for coordinate in coordinates:
            counted = []
            moved = False
            for chain in chains.values():
                live = [rev for rev, occurrence in chain.items()
                        if occurrence.available_at <= coordinate]
                if not live:
                    continue
                winner = chain[max(live)]
                moved = moved or winner.available_at == coordinate
                if winner.status == "active" and winner.block == block:
                    counted.append(winner)
            changed.append(moved)
            live_writer.append(all(
                occurrence.batch_id is None and occurrence.rev == 0
                for event_id in chains
                for occurrence in self.closes[event_id]
                if occurrence.available_at == coordinate
            ))
            if len(counted) > 1:
                states.append(("ambiguous",))
            elif not counted:
                states.append(("absent",))
            elif counted[0].is_closed != 1:
                states.append(("open",))
            else:
                states.append(("closed", counted[0].total_cost_usd))
        timeline = (tuple(coordinates), tuple(states), tuple(changed),
                    tuple(live_writer))
        self._block_timelines[block] = timeline
        return timeline

    @staticmethod
    def _closed_with_money(state) -> bool:
        return state[0] == "closed" and _usable_amount(state[1])

    @classmethod
    def _classify(cls, before, after, changed: bool) -> str:
        """§5.3: one close event, from K's states just before and at it."""
        if before[0] == "ambiguous" or after[0] == "ambiguous":
            return "uncertain"
        if not changed:
            return "metadata"
        if before[0] == "closed" and after[0] == "closed":
            if not (_usable_amount(before[1]) and _usable_amount(after[1])):
                return "uncertain"
            if abs(after[1] - before[1]) > _CLOSE_TOTAL_TOLERANCE_USD:
                return "monetary"
            return "metadata"
        if after[0] == "closed":
            return "monetary" if _usable_amount(after[1]) else "uncertain"
        if before[0] == "closed":
            return "monetary" if _usable_amount(before[1]) else "uncertain"
        if before[0] == after[0] == "absent":
            return "metadata"
        return "no-boundary"

    def _walk(self, chain, tail: int, selected: int, block) -> tuple:
        """§5.4 steps 1–6 over the coordinate groups from ``tail``'s
        coordinate inclusive. Returns ``(anchor, reasons)``."""
        coordinates, states, changed_flags, live_writer = \
            self._block_timeline(block)
        start = chain[tail].available_at
        revisions_at: dict[int, list] = {}
        for rev in range(tail + 1, selected + 1):
            revisions_at.setdefault(chain[rev].available_at, []).append(rev)
        events = {}
        # §5.4 step 3: an unpaired monetary event is exempt from the pending
        # mark only as K's FIRST live-writer close, so whether K was ever
        # closed before it is read over the whole timeline, before t too.
        first_live_close = {}
        closed_before = False
        for index, coordinate in enumerate(coordinates):
            before = states[index - 1] if index else ("absent",)
            if coordinate >= start:
                events[coordinate] = self._classify(
                    before, states[index], changed_flags[index])
                first_live_close[coordinate] = (
                    live_writer[index] and not closed_before
                    and before[0] in ("absent", "open")
                    and states[index][0] == "closed"
                )
            closed_before = closed_before or states[index][0] == "closed"
        anchor = tail
        reasons: set = set()
        # An unpaired monetary close event since the anchor leaves A
        # unproven for a later price-only rewrite (findings D1, R7-1).
        marked = False
        for coordinate in sorted(set(revisions_at) | set(events)):
            revisions = revisions_at.get(coordinate, [])
            changed = any(
                chain[rev].block_cost_usd != chain[rev - 1].block_cost_usd
                for rev in revisions
            )
            event = events.get(coordinate)
            if event == "monetary":
                if changed:
                    anchor = max(revisions)
                    reasons.clear()
                    marked = False
                else:
                    effective = max(
                        rev for rev in range(tail, selected + 1)
                        if chain[rev].available_at <= coordinate
                    )
                    if (chain[effective].block_cost_usd
                            != chain[anchor].block_cost_usd):
                        reasons.add(LINEAGE_UNPAIRED_CLOSE_CHANGE)
                    if not first_live_close[coordinate]:
                        marked = True
            elif event == "uncertain":
                reasons.add(LINEAGE_CLOSE_PAIRING_UNCERTAIN)
            elif changed:
                if event == "no-boundary":
                    reasons.add(LINEAGE_REWRITE_WITHOUT_CLOSED_BOUNDARY)
                else:
                    # A price-only rewrite (no close event, or a metadata
                    # one) is walked past only under a retained closed close
                    # and with no step-3 mark pending.
                    index = bisect.bisect_left(coordinates, coordinate)
                    before = states[index - 1] if index else ("absent",)
                    if not self._closed_with_money(before):
                        reasons.add(LINEAGE_REWRITE_WITHOUT_CLOSED_BOUNDARY)
                    if marked:
                        reasons.add(LINEAGE_UNPAIRED_CLOSE_CHANGE)
        return anchor, reasons

    def _withhold(self, event_id: str, reasons, current_cost,
                  anchor) -> Restoration:
        """A fail-closed answer and §5.7's report predicate."""
        occurrences = self.milestones[event_id]
        selected = self.selected.get(event_id)
        target = None
        for occurrence in occurrences:
            if (selected is not None and occurrence.status == "active"
                    and (occurrence.rev, occurrence.content_hash) == selected):
                target = occurrence
                break
        candidates = []
        if target is not None:
            by_rev: dict[int, list] = {}
            for occurrence in occurrences:
                by_rev.setdefault(occurrence.rev, []).append(occurrence)
            for rev in range(max(by_rev), -1, -1):
                if anchor is not None and rev < anchor:
                    break
                group = by_rev.get(rev)
                if not group:
                    continue  # a gap is not a boundary
                matching = [
                    occurrence for occurrence in group
                    if occurrence.status == "active"
                    and occurrence.at == target.at
                    and occurrence.identity == target.identity
                ]
                if not matching:
                    break  # a proven boundary
                if any(
                    _usable_amount(occurrence.block_cost_usd)
                    and occurrence.block_cost_usd != current_cost
                    for occurrence in matching
                ):
                    candidates.append(rev)
        return Restoration(
            "withhold",
            reason_codes=tuple(sorted(set(reasons))),
            candidate_revisions=tuple(sorted(candidates)),
            reported=bool(candidates),
        )

    def restoration(self, event_id: str,
                    current_record: Mapping) -> Restoration:
        """§5.4: the decision for one unchanged crossing in a frozen block.

        ``current_record`` is the selected record; its ``block_cost_usd`` is
        the comparison base. In order: ``keep`` without recorded history;
        ``withhold`` when the event's own history is uncertain; ``withhold``
        when the walk collected a reason; ``keep`` when the anchor's amount
        equals the current one or is unusable; else ``restore`` to the
        anchor's parsed ``block_cost_usd``, copied as is.
        """
        occurrences = self.milestones.get(event_id)
        if not occurrences:
            return _LINEAGE_KEEP
        current_cost = (current_record.get("payload") or {}).get(
            "block_cost_usd")
        own = self.uncertain.get(event_id)
        if own:
            return self._withhold(event_id, own, current_cost, None)
        chain = _first_per_revision(occurrences)
        selected = max(chain)
        if chain[selected].status != "active":
            return _LINEAGE_KEEP
        life_start = 0
        for rev in range(selected, -1, -1):
            if chain[rev].status != "active":
                life_start = rev + 1
                break
        tail = life_start
        for rev in range(life_start + 1, selected + 1):
            if (chain[rev].at != chain[rev - 1].at
                    or chain[rev].identity != chain[rev - 1].identity):
                tail = rev
        block = chain[selected].block
        if any(close_id in self.uncertain
               for close_id in self.close_ids_ever_in_block.get(block, ())):
            # §5.3: no close event of K can prove anything; A stays at t.
            return self._withhold(
                event_id, {LINEAGE_CLOSE_LINEAGE_UNCERTAIN}, current_cost,
                tail)
        anchor, reasons = self._walk(chain, tail, selected, block)
        if reasons:
            return self._withhold(event_id, reasons, current_cost, anchor)
        value = chain[anchor].block_cost_usd
        if not _usable_amount(value) or value == current_cost:
            return _LINEAGE_KEEP
        return Restoration("restore", value=value)

    def extended(self, *, batch_id: "str | None" = None,
                 batch_actions: Sequence[Mapping] = (),
                 plain: Sequence[Mapping] = ()) -> "FiveHourLineage":
        """#1040 (spec §5.4): the lineage as it reads after ``batch_actions``
        (one completed correction batch, completion's action form) and then
        ``plain`` (revision-0 events) are appended at the journal's end.

        Read-only: a new instance with an empty timeline cache. The batch
        becomes effective at one coordinate after every existing one, each
        plain event at the following coordinates in order. Every candidate
        is built and hashed by the selector's own constructors, so each
        touched id's occurrences, selected pair (its highest revision; a tie
        keeps the earlier occurrence) and history uncertainty are what
        :func:`build_five_hour_lineage` reads over a journal that really
        holds the records: a revision written above an admission-hidden id
        stays ``hidden-revision``, one above an inert revision leaves a
        ``revision-gap``. The close index gains every block a touched close
        id's new occurrence names.
        """
        if not self._coordinate_end:
            self._coordinate_end.append(max(
                (occurrence.available_at
                 for table in (self.milestones, self.closes)
                 for items in table.values() for occurrence in items),
                default=-1,
            ))
        coordinate = self._coordinate_end[0] + 1
        added: dict[str, list] = {}
        if batch_actions:
            name = batch_id or "lineage-extended"
            records = _lib_journal.make_correction_batch(
                batch_id=name, family="lineage-extended", at="extended",
                actions=[dict(action) for action in batch_actions])
            for record in records[1:-1]:
                candidate = _lib_journal._candidate_from_correction(
                    record, coordinate)
                added.setdefault(candidate.event_id, []).append(candidate)
            coordinate += 1
        for record in plain:
            candidate = _lib_journal._candidate_from_evt(record, coordinate)
            added.setdefault(candidate.event_id, []).append(candidate)
            coordinate += 1
        if not added:
            return dataclasses.replace(
                self, _block_timelines={}, _coordinate_end=[])
        milestones = dict(self.milestones)
        closes = dict(self.closes)
        selected = dict(self.selected)
        uncertain = dict(self.uncertain)
        ever = {block: set(ids)
                for block, ids in self.close_ids_ever_in_block.items()}
        for event_id, candidates in added.items():
            existing = list(self.closes.get(event_id)
                            or self.milestones.get(event_id) or ())
            identities: dict = {}
            for occurrence in existing:
                if occurrence.identity is not None:
                    identities["identity"] = occurrence.identity
            fresh = []
            for candidate in candidates:
                occurrence, _kind, _unhashable = _lineage_occurrence(
                    candidate.record, rev=candidate.rev,
                    status=candidate.status,
                    content_hash=candidate.content_hash,
                    batch_id=candidate.batch_id,
                    available_at=candidate.sequence, identities=identities)
                fresh.append(occurrence)
            items = existing + fresh
            kinds = {occurrence.kind for occurrence in items
                     if occurrence.status == "active"}
            if not existing and not (kinds & _LINEAGE_KINDS):
                continue  # not a five-hour milestone or close
            pair = selected.get(event_id)
            for candidate in candidates:
                if pair is None or candidate.rev > pair[0]:
                    pair = (candidate.rev, candidate.content_hash)
            selected[event_id] = pair
            unhashable = any(
                occurrence.status == "active"
                and occurrence.kind == "five_hour_block_close"
                and occurrence.block is None
                for occurrence in items)
            items, codes = _finalize_lineage_id(
                event_id, items, kinds=kinds, unhashable=unhashable,
                selected=pair, hidden=event_id in self.hidden_ids)
            if codes:
                uncertain[event_id] = codes
            else:
                uncertain.pop(event_id, None)
            if "five_hour_block_close" in kinds:
                closes[event_id] = items
                for occurrence in items:
                    if (occurrence.kind == "five_hour_block_close"
                            and occurrence.block is not None):
                        ever.setdefault(occurrence.block, set()).add(event_id)
            if "five_hour_milestone" in kinds:
                milestones[event_id] = items
        return FiveHourLineage(
            milestones=milestones,
            closes=closes,
            close_ids_ever_in_block={
                block: tuple(sorted(ids)) for block, ids in ever.items()},
            hidden_ids=self.hidden_ids,
            selected=selected,
            uncertain=uncertain,
        )


def _lineage_occurrence(record, *, rev: int, status: str, content_hash: str,
                        batch_id, available_at: int, identities: dict):
    """§5.1: one admissible candidate summarized as a :class:`LineageOccurrence`.

    Returns ``(occurrence, kind, unhashable)``: ``kind`` is the active
    payload's kind (``None`` for a tombstone or a non-object payload) and
    ``unhashable`` marks a close whose block key indexes no block (§5.2,
    finding D6). ``identities`` shares one identity object per run of equal
    milestone identities of an id.
    """
    fields: dict = {}
    kind = None
    unhashable = False
    if status == "active":
        payload = (record or {}).get("payload")
        kind = payload.get("kind") if isinstance(payload, dict) else None
        if kind == "five_hour_milestone":
            identity = _lineage_identity(payload)
            prior = identities.get("identity")
            if prior is not None and prior == identity:
                identity = prior  # one shared object per run of equals
            identities["identity"] = identity
            fields = dict(
                block=_five_hour_block(payload),
                at=record.get("at"), identity=identity,
                block_cost_usd=payload.get("block_cost_usd"),
            )
        elif kind == "five_hour_block_close":
            block = _five_hour_block(payload)
            try:
                hash(block)
            except TypeError:
                # §5.2 (finding D6): a block key that indexes no block
                # makes the id ``invalid-payload``; the plan goes on.
                unhashable = True
                block = None
            fields = dict(
                block=block,
                is_closed=payload.get("is_closed"),
                total_cost_usd=payload.get("total_cost_usd"),
                close_view=_canonical_bytes([
                    kind, payload.get("account_key"),
                    payload.get("five_hour_window_key"),
                    payload.get("is_closed"),
                    payload.get("total_cost_usd"),
                ]),
            )
    fields.setdefault("block", None)
    return LineageOccurrence(
        rev=rev, status=status, content_hash=content_hash, batch_id=batch_id,
        available_at=available_at, kind=kind, **fields,
    ), kind, unhashable


def _finalize_lineage_id(event_id: str, items: list, *, kinds, unhashable: bool,
                         selected, hidden: bool) -> tuple:
    """§5.1/§5.2: one id's occurrences in canonical order and its own
    history uncertainty. Sorts ``items`` in place; returns
    ``(tuple(items), codes)``."""
    items.sort(key=lambda item: (
        item.rev, item.available_at, item.status, item.content_hash,
        item.batch_id or "",
    ))
    codes = _history_uncertainty(
        items, hidden=hidden,
        invalid=len(kinds) != 1 or unhashable,
        selected=selected,
        close=set(kinds) == {"five_hour_block_close"},
    )
    return tuple(items), codes


def build_five_hour_lineage(fold, *, selection,
                            physical_revisions) -> FiveHourLineage:
    """§5.1: one pass over the selector fold's admissible candidates.

    ``fold`` is the :class:`SelectorFold` ``resolve_effective_events`` built
    (its ``candidates`` hold every rev-0 event and every action of each
    completed, untainted batch); ``physical_revisions`` maps an id to its
    highest admission-hidden revision. Every candidate of an id some active
    candidate places in a five-hour milestone or close is kept, identical
    duplicates in different batches included.
    """
    wanted = set()
    for candidate in fold.candidates:
        if candidate.status == "active" and candidate.record is not None:
            payload = candidate.record.get("payload")
            if isinstance(payload, dict) and payload.get("kind") in \
                    _LINEAGE_KINDS:
                wanted.add(candidate.event_id)
    commits: dict[str, int] = {}
    kinds: dict[str, set] = {}
    occurrences: dict[str, list] = {}
    identities: dict[str, dict] = {}
    unhashable_ids: set = set()
    for candidate in fold.candidates:
        event_id = candidate.event_id
        if event_id not in wanted:
            continue
        if candidate.batch_id is None:
            available_at = candidate.sequence
        else:
            available_at = commits.get(candidate.batch_id)
            if available_at is None:
                available_at = fold.markers[candidate.batch_id]["commit"][3]
                commits[candidate.batch_id] = available_at
        occurrence, kind, unhashable = _lineage_occurrence(
            candidate.record, rev=candidate.rev, status=candidate.status,
            content_hash=candidate.content_hash, batch_id=candidate.batch_id,
            available_at=available_at,
            identities=identities.setdefault(event_id, {}),
        )
        if candidate.status == "active":
            kinds.setdefault(event_id, set()).add(kind)
        if unhashable:
            unhashable_ids.add(event_id)
        occurrences.setdefault(event_id, []).append(occurrence)
    hidden_ids = frozenset(physical_revisions or ())
    milestones: dict[str, tuple] = {}
    closes: dict[str, tuple] = {}
    close_ids_ever_in_block: dict[tuple, set] = {}
    selected: dict[str, tuple] = {}
    uncertain: dict[str, tuple] = {}
    for event_id, items in occurrences.items():
        event_kinds = kinds.get(event_id, set())
        winner = selection.by_id.get(event_id)
        if winner is not None:
            selected[event_id] = (winner.rev, winner.content_hash)
        items, codes = _finalize_lineage_id(
            event_id, items, kinds=event_kinds,
            unhashable=event_id in unhashable_ids,
            selected=selected.get(event_id),
            hidden=event_id in hidden_ids,
        )
        if codes:
            uncertain[event_id] = codes
        if "five_hour_block_close" in event_kinds:
            closes[event_id] = items
            for item in items:
                if (item.kind == "five_hour_block_close"
                        and item.block is not None):
                    close_ids_ever_in_block.setdefault(
                        item.block, set()).add(event_id)
        if "five_hour_milestone" in event_kinds:
            milestones[event_id] = items
    return FiveHourLineage(
        milestones=milestones,
        closes=closes,
        close_ids_ever_in_block={
            block: tuple(sorted(ids))
            for block, ids in close_ids_ever_in_block.items()
        },
        hidden_ids=hidden_ids,
        selected=selected,
        uncertain=uncertain,
    )


@dataclasses.dataclass(frozen=True)
class WithheldMilestone:
    """One reported fail-closed milestone of a withheld block (§5.7)."""
    event_id: str
    current_revision: int
    candidate_revisions: tuple
    reason_codes: tuple


@dataclasses.dataclass(frozen=True)
class WithheldBlock:
    """One five-hour block whose milestone repair was withheld (§5.7)."""
    account_key: object
    five_hour_window_key: object
    close_event_ids: tuple
    milestones: tuple

    def to_dict(self) -> dict:
        return {
            "accountKey": self.account_key,
            "fiveHourWindowKey": self.five_hour_window_key,
            "closeEventIds": list(self.close_event_ids),
            "milestones": [
                {
                    "eventId": milestone.event_id,
                    "currentRevision": milestone.current_revision,
                    "candidateRevisions": list(milestone.candidate_revisions),
                    "reasonCodes": list(milestone.reason_codes),
                }
                for milestone in self.milestones
            ],
        }


def _withheld_blocks(withheld: Mapping, current: Mapping[str, Mapping],
                     actions, selection) -> tuple:
    """§5.7: group reported withholds by block, sorted, with the block's
    close ids in the plan's final state (retained, acted-on or preserved
    records; a revival is an acted-on record)."""
    if not withheld:
        return ()
    final = dict(current)
    for action in actions:
        if action.disposition == "tombstone":
            final.pop(action.event_id, None)
        else:
            final[action.event_id] = {"payload": action.payload or {}}
    close_ids: dict[tuple, set] = {}
    for event_id, record in final.items():
        payload = (record or {}).get("payload") or {}
        if payload.get("kind") == "five_hour_block_close":
            close_ids.setdefault(_five_hour_block(payload), set()).add(
                event_id)
    grouped: dict[tuple, list] = {}
    for event_id, decision in withheld.items():
        payload = (current.get(event_id) or {}).get("payload") or {}
        winner = selection.by_id.get(event_id)
        grouped.setdefault(_five_hour_block(payload), []).append(
            WithheldMilestone(
                event_id=event_id,
                current_revision=None if winner is None else int(winner.rev),
                candidate_revisions=tuple(sorted(
                    decision.candidate_revisions)),
                reason_codes=tuple(sorted(set(decision.reason_codes))),
            ))
    blocks = [
        WithheldBlock(
            account_key=block[0], five_hour_window_key=block[1],
            close_event_ids=tuple(sorted(close_ids.get(block, ()))),
            milestones=tuple(sorted(items, key=lambda item: item.event_id)),
        )
        for block, items in grouped.items()
    ]
    blocks.sort(key=lambda item: (
        "" if item.account_key is None else str(item.account_key),
        str(item.five_hour_window_key),
    ))
    return tuple(blocks)


def _resolve_closed_block_milestones(
    current: Mapping[str, Mapping], desired: Mapping[str, Mapping],
    preserved: Mapping[str, Mapping], frozen_blocks, replaced_blocks, *,
    selection, lineage: "FiveHourLineage | None" = None,
    withheld: "dict | None" = None,
) -> dict[str, dict]:
    """#875: target records of the five-hour milestones of closed blocks.

    A block is classified by the close the plan's final state holds for it
    (spec §4.1), by its money rather than by whether the plan acts on it. A
    FROZEN close keeps the money it already had: a kept close, including a
    forced-conflict reaffirmation; preserved history the plan retains or
    revives; or a correction of a closed close that leaves its usable total
    within 1e-9 USD (:func:`_close_keeps_its_money`), such as the reviewed
    hold's weekly-axis update. Under it, an existing crossing whose ``at``
    and non-monetary identity are both unchanged keeps its current
    ``block_cost_usd`` across any card edit, and a crossing whose ``at`` or
    identity differs is a corrected crossing that takes replay's block cost.

    #884 (spec §5.5): with ``lineage``, such an unchanged crossing instead
    takes its history-proven anchor amount when an earlier rederive repriced
    it (:meth:`FiveHourLineage.restoration`). When its history is ambiguous
    it keeps the current amount, and a reported withhold is recorded in
    ``withheld`` as ``{event_id: Restoration}``. No other role is restored,
    and restoration never changes a block's frozen or replaced class.
    A REPLACED close is one this plan adds, or corrects so that it writes the
    close's money (it closes an open block, or its totals are unusable or
    differ by more), and every matched milestone under it is a corrected
    crossing. A block holding a frozen close and a replaced one is frozen.
    Replay additions take replay's block cost under either. The milestones of
    every other block follow replay and are not returned.

    A present marginal is its final block cost minus the final block cost of
    its predecessor: the highest lower threshold in the same (account,
    window, reset_event_ref) segment of the plan's FINAL state. The final
    state is every milestone the plan leaves active: these resolved records,
    replay's milestones of the other blocks, and retained or preserved
    current milestones. Tombstones are excluded. Block costs are settled
    before any marginal, so the definition is not circular. A value within
    1e-9 USD of its reference (the current marginal, or the desired one for
    an addition) keeps the reference's bytes. A reference that is not a
    finite number only disables that snap; it never aborts the plan.

    Marginal presence comes from the CURRENT record for an unchanged
    crossing: replay never reproduces preserved ``b:`` predecessors, and a
    reviewed hold can remove a desired predecessor before the diff. It comes
    from the desired record for a corrected crossing or an addition. A
    candidate keeps its chosen marginal (the current one for an unchanged
    crossing, the desired one otherwise) rather than inventing a value when
    it has no usable predecessor, or when its own threshold or block cost is
    unusable. Its block cost is already settled and is never reset there, so
    a restored amount survives (#884 spec §5.6). Only an ``int`` threshold
    occupies a position, and only a finite amount is usable. A row whose
    threshold is usable but whose block cost is not still occupies its
    threshold, so a successor right above it fails closed and never skips to
    a lower row.

    A second plan is a no-op for a block whose close it takes no action on.
    That close is frozen in the next plan, kept or preserved, whatever role
    it had in this one. Every crossing this plan wrote from replay now
    carries the ``at`` and identity of the raw replay records, and a record
    that reconciliation or a hold carries into ``desired`` keeps the applied
    identity and resolved values. So the next plan sees unchanged crossings
    whose current block cost and presence are the values this plan wrote,
    and it resolves the same marginals against the same final predecessors.

    Returns ``{event_id: record}`` for every matched milestone and every
    replay addition of a frozen or replaced block. The ``alerted_at`` latch
    is carried before the identity comparison, exactly as the ordinary path
    carries it.
    """
    settled_blocks = frozenset(frozen_blocks) | frozenset(replaced_blocks)
    resolved: dict[str, dict] = {}
    roles: dict[str, str] = {}
    final: dict[str, Mapping] = {}
    for event_id in sorted(set(current) | set(desired) | set(preserved)):
        current_record = current.get(event_id)
        desired_record = desired.get(event_id)
        preserved_record = preserved.get(event_id)
        kind_payload = (
            (desired_record or current_record or preserved_record).get(
                "payload") or {}
        )
        if kind_payload.get("kind") != "five_hour_milestone":
            continue
        if preserved_record is not None:
            # Held out of the diff (#426): retained as it stands, or revived
            # when this family's own earlier batch retired it.
            if current_record is not None:
                final[event_id] = current_record
            elif _revived_by_this_family(selection.by_id.get(event_id)):
                final[event_id] = preserved_record
            continue
        block = _five_hour_block(kind_payload)
        if block not in settled_blocks:
            # An open block, or one whose close the plan tombstones: replay
            # decides these milestones, exactly as it did before #875.
            if desired_record is not None:
                final[event_id] = desired_record
            continue
        if current_record is not None and desired_record is not None:
            carried = _preserve_non_derivable_state(
                current_record, desired_record)
            current_payload = current_record.get("payload") or {}
            payload = dict(carried.get("payload") or {})
            # A moved ``at`` is a corrected crossing like a changed identity:
            # copying replay's marginal for it would leave the next plan to
            # re-resolve it against its final predecessor (spec §1.3).
            if (
                block in frozen_blocks
                and current_record.get("at") == desired_record.get("at")
                and _milestone_identity(current_payload)
                == _milestone_identity(payload)
            ):
                payload["block_cost_usd"] = current_payload.get(
                    "block_cost_usd")
                roles[event_id] = "unchanged"
                if lineage is not None:
                    # #884 (spec §5.5): a proven historical amount replaces
                    # one an earlier rederive repriced; an ambiguous history
                    # keeps the current amount and is disclosed.
                    decision = lineage.restoration(event_id, current_record)
                    if decision.action == "restore":
                        payload["block_cost_usd"] = decision.value
                    elif (decision.action == "withhold" and decision.reported
                          and withheld is not None):
                        withheld[event_id] = decision
            else:
                roles[event_id] = "corrected"
            record = {**carried, "payload": payload}
        elif desired_record is not None:
            roles[event_id] = "added"
            record = {**desired_record,
                      "payload": dict(desired_record.get("payload") or {})}
        else:
            # Current only and not preserved: the plan tombstones it.
            continue
        resolved[event_id] = record
        final[event_id] = record

    # Every row with a usable threshold occupies it, usable cost or not.
    occupied: dict[tuple, dict[int, list[str]]] = {}
    for event_id, record in final.items():
        payload = record.get("payload") or {}
        threshold = _usable_threshold(payload.get("percent_threshold"))
        if threshold is not None:
            occupied.setdefault(
                _milestone_segment_key(payload), {}
            ).setdefault(threshold, []).append(event_id)

    def predecessor_cost(payload):
        """The final block cost at the nearest lower threshold, or None."""
        rows = occupied.get(_milestone_segment_key(payload), {})
        threshold = _usable_threshold(payload.get("percent_threshold"))
        lower = [value for value in rows if value < threshold]
        if not lower:
            return None
        nearest = rows[max(lower)]
        costs = {
            event_id: (final[event_id].get("payload") or {}).get(
                "block_cost_usd")
            for event_id in nearest
        }
        if not all(_usable_amount(cost) for cost in costs.values()):
            return None  # fails closed: never skips past an unusable row
        return costs[max(nearest)]

    for event_id, record in resolved.items():
        payload = record["payload"]
        current_payload = (current.get(event_id) or {}).get("payload") or {}
        desired_payload = (desired.get(event_id) or {}).get("payload") or {}
        chosen = (
            current_payload if roles[event_id] == "unchanged"
            else desired_payload
        )
        if chosen.get("marginal_cost_usd") is None:
            payload["marginal_cost_usd"] = None
            continue
        prior = None
        if (
            _usable_threshold(payload.get("percent_threshold")) is not None
            and _usable_amount(payload.get("block_cost_usd"))
        ):
            prior = predecessor_cost(payload)
        if prior is None:
            # #884 (spec §5.6): only the marginal falls back. The block cost
            # is already the chosen record's, or a restored amount.
            payload["marginal_cost_usd"] = chosen.get("marginal_cost_usd")
            continue
        value = float(payload["block_cost_usd"]) - float(prior)
        reference = (
            current_payload if event_id in current else desired_payload
        ).get("marginal_cost_usd")
        if (
            _usable_amount(reference)
            and abs(value - reference) <= _MARGINAL_TOLERANCE_USD
        ):
            value = reference
        payload["marginal_cost_usd"] = value
    return resolved


# ── #1040: derivation completion keeps closed five-hour money frozen ───────
#
# Derivation completion journals, for every id the gap acts on (its with-gap
# and without-gap candidates differ), the with-gap candidate. For a closed
# five-hour block that candidate carries the money the scratch replay
# computed at the binary's CURRENT card, so a metadata-only gap repriced the
# frozen block (spec §1–§3). The rules below decide what completion appends
# for the closes and milestones of every touched block instead (spec §5).

COMPLETION_FROZEN = "FROZEN"
COMPLETION_REPLACED = "REPLACED"
COMPLETION_ADDED = "ADDED"
COMPLETION_OPEN = "OPEN"


@dataclasses.dataclass(frozen=True)
class CompletionClose:
    """#1040 (spec §5.2): one close's proposed target and class.

    ``target`` is T (``None`` keeps nothing to append, or a tombstone on the
    OPEN path); ``frozen_form`` is T₀, defined for a close whose retained and
    with-gap records are both closed, else ``None``.
    """
    block_class: str
    target: "Mapping | None"
    frozen_form: "Mapping | None" = None


def _closed_record(record: "Mapping | None") -> bool:
    return (record is not None
            and (record.get("payload") or {}).get("is_closed") == 1)


def _total_moves(before: "Mapping", after: "Mapping") -> bool:
    """Both totals usable and more than 1e-9 USD apart."""
    left = (before.get("payload") or {}).get("total_cost_usd")
    right = (after.get("payload") or {}).get("total_cost_usd")
    return (_usable_amount(left) and _usable_amount(right)
            and abs(right - left) > _CLOSE_TOTAL_TOLERANCE_USD)


def _is_missing_card_correction(preserved, current, desired) -> bool:
    """§5.2: a preserved result that is neither D nor C and moves C's total.

    ``_with_close_marker`` builds a #869 missing-card correction as a new
    record under the same structure, so identity tells it apart from a kept
    (``current``) or structurally different (``desired``) result.
    """
    return (preserved is not current and preserved is not desired
            and _total_moves(current, preserved))


def classify_completion_close(
    current: "Mapping | None", desired: "Mapping | None",
    reference: "Mapping | None", *, acted: bool, has_physical: bool,
    several_close_ids: bool,
) -> CompletionClose:
    """#1040 (spec §5.2): the proposed close target T and the block class.

    ``current`` is C (the selected active close, or ``None``), ``desired`` D
    and ``reference`` R. The rules apply in order:

    1. not acted on: T = C, FROZEN when C is closed, else OPEN;
    2. D absent or not closed, or C present and not closed: OPEN (today's
       path; no closed money exists to keep). OPEN is this close's class:
       the block is OPEN only when every close of it is (spec §5.2 rule 2,
       "no closed close in the final state");
    3. D closed, no C: ADDED (plain revision 0) without a physical revision,
       else a REPLACED revival;
    4. C and D closed: T₀ first (``overlay(D, C)`` when preservation returns
       D, else C), then 4.1 a gap-caused population change, 4.2 a #869
       missing-card correction P, 4.3 the same-round P₂ = preserve(T₀, D),
       4.4 FROZEN with T = T₀.

    A block naming several close ids is never REPLACED: rules 4.1–4.3 yield
    T₀ and FROZEN, and a revival proposes D under FROZEN, whose lineage check
    decides whether it can land.
    """
    if not acted:
        return CompletionClose(
            COMPLETION_FROZEN if _closed_record(current) else COMPLETION_OPEN,
            current)
    if not _closed_record(desired) or (
            current is not None and not _closed_record(current)):
        return CompletionClose(COMPLETION_OPEN, desired)
    if current is None:
        if not has_physical:
            return CompletionClose(COMPLETION_ADDED, desired)
        if several_close_ids:
            return CompletionClose(COMPLETION_FROZEN, desired)
        return CompletionClose(COMPLETION_REPLACED, desired)
    preserved = _preserve_non_derivable_state(current, desired)
    frozen = (close_money_overlay(desired, current)
              if preserved is desired else current)
    if several_close_ids:
        return CompletionClose(COMPLETION_FROZEN, frozen, frozen)
    current_payload = current.get("payload") or {}
    desired_payload = desired.get("payload") or {}
    if (
        _closed_record(reference)
        and _usable_amount(current_payload.get("total_cost_usd"))
        and _usable_amount(desired_payload.get("total_cost_usd"))
        and close_population(current_payload)
        == close_population(reference.get("payload") or {})
        and close_population(desired_payload)
        != close_population(current_payload)
        and _total_moves(current, desired)
    ):
        return CompletionClose(COMPLETION_REPLACED, desired, frozen)
    if _is_missing_card_correction(preserved, current, desired):
        return CompletionClose(COMPLETION_REPLACED, preserved, frozen)
    if _semantic_event(frozen) != _semantic_event(current):
        second = _preserve_non_derivable_state(frozen, desired)
        if _is_missing_card_correction(second, frozen, desired):
            return CompletionClose(COMPLETION_REPLACED, second, frozen)
    return CompletionClose(COMPLETION_FROZEN, frozen, frozen)

#: The milestone fields a FROZEN or ADDED block's acted crossing keeps from
#: its retained record (spec §5.3): its money and the block tokens it was
#: priced from.
_COMPLETION_MILESTONE_KEPT_KEYS = (
    "block_cost_usd", "marginal_cost_usd",
    *(f"block_{key}" for key in _BLOCK_TOKEN_KEYS),
)

COMPLETION_TREATMENTS = ("frozen", "replaced", "marginal", "none", "main")


def _milestone_overlay(desired: Mapping, current: Mapping) -> dict:
    """D with C's money and block tokens (a key C lacks is removed)."""
    payload = dict(desired.get("payload") or {})
    current_payload = current.get("payload") or {}
    for key in _COMPLETION_MILESTONE_KEPT_KEYS:
        if key in current_payload:
            payload[key] = current_payload[key]
        else:
            payload.pop(key, None)
    return {**desired, "payload": payload}


def completion_block_milestones(
    *, treatment: str, block_class: str, current: Mapping[str, Mapping],
    with_gap: Mapping[str, Mapping], acted: frozenset, retired: frozenset,
    lineage: FiveHourLineage, selection,
    fixed: "Mapping[str, Mapping] | None" = None,
) -> dict[str, dict]:
    """#1040 (spec §5.3, §5.4): one block's milestone targets.

    ``current`` holds the block's selected active milestones (the whole
    journal's, so predecessors before a trusted frontier are included) and
    ``with_gap`` the block's with-gap milestone candidates. ``retired`` are
    the acted milestones with a retained record and no with-gap one: today's
    path tombstones them, so they are no predecessor in any final chain.
    ``fixed`` are records of ids the planner does not own that today's path
    leaves in this block: they stand in the final chain as given and are
    never returned. Returns every resolved record that differs semantically
    from its retained record or has none.

    A **required addition** is an acted with-gap id absent from
    ``selection.by_id`` (spec §5.4): no admitted decision, active or
    tombstoned, answers it. An acted with-gap id without C that the
    selection does know (a tombstoned milestone, revived) is an optional
    rewrite. ``treatment``:

    * ``"frozen"``: an acted crossing with C takes D with C's money and
      block tokens; when that is a corrected crossing (its ``at`` or
      identity differs) its block cost is the lineage's restoration value
      when :meth:`FiveHourLineage.restoration` says ``restore``. An acted id
      without C takes D (required or revived), every other id C.
    * ``"replaced"`` (REPLACED blocks only): an id the with-gap replay
      carries takes D, acted or not; a current id it does not carry takes C.
    * ``"marginal"``: every existing id keeps C; required additions take D;
      only the marginals are resolved over the final chain.
    * ``"none"``: the required additions alone, their marginals resolved
      over the chain whose existing ids are C.
    * ``"main"``: each acted id's record as today's completion path appends
      it (``_preserve_non_derivable_state(C, D)``, or D), nothing else and
      no marginal resolution.

    The kernel is :func:`_resolve_closed_block_milestones` with the block
    FROZEN (``replaced`` treatment: REPLACED) and no lineage.
    """
    if treatment not in COMPLETION_TREATMENTS:
        raise ValueError(f"unknown milestone treatment {treatment!r}")
    if treatment == "replaced" and block_class != COMPLETION_REPLACED:
        raise ValueError("the replaced treatment needs a REPLACED block")
    fixed = dict(fixed or {})
    if treatment == "main":
        out = {}
        for event_id in sorted(acted & set(with_gap)):
            desired = with_gap[event_id]
            existing = current.get(event_id)
            record = (desired if existing is None
                      else _preserve_non_derivable_state(existing, desired))
            if existing is None or (_semantic_event(record)
                                    != _semantic_event(existing)):
                out[event_id] = dict(record)
        return out
    desired: dict[str, Mapping] = {}
    for event_id, existing in current.items():
        if event_id in retired:
            continue
        candidate = with_gap.get(event_id)
        if treatment == "replaced" and candidate is not None:
            desired[event_id] = candidate
        elif (treatment == "frozen" and candidate is not None
              and event_id in acted):
            record = _milestone_overlay(candidate, existing)
            existing_payload = existing.get("payload") or {}
            if (record.get("at") != existing.get("at")
                    or _lineage_identity(record["payload"])
                    != _lineage_identity(existing_payload)):
                decision = lineage.restoration(event_id, existing)
                if decision.action == "restore":
                    record["payload"]["block_cost_usd"] = decision.value
            desired[event_id] = record
        else:
            desired[event_id] = existing
    required = set()
    for event_id, candidate in with_gap.items():
        if event_id in current or event_id not in acted:
            continue
        if event_id not in selection.by_id:
            required.add(event_id)
        elif treatment in ("marginal", "none"):
            continue  # an optional revival: only F, K and R rewrite
        desired[event_id] = candidate
    desired.update(fixed)
    if not desired:
        return {}
    block = _five_hour_block(
        (next(iter(desired.values())).get("payload") or {}))
    replaced = treatment == "replaced"
    resolved = _resolve_closed_block_milestones(
        # A fixed record is today's: it stands in the chain at its own money.
        {event_id: record for event_id, record in current.items()
         if event_id not in fixed},
        desired, {},
        frozenset() if replaced else frozenset({block}),
        frozenset({block}) if replaced else frozenset(),
        selection=selection, lineage=None,
    )
    out = {}
    for event_id in sorted(resolved):
        if event_id in fixed:
            continue
        if treatment == "none" and event_id not in required:
            continue
        existing = current.get(event_id)
        record = resolved[event_id]
        if existing is None or (_semantic_event(record)
                                != _semantic_event(existing)):
            out[event_id] = record
    return out

@dataclasses.dataclass(frozen=True)
class BlockDecision:
    """#1040 (spec §5.4): one touched block's lineage-checked decision.

    ``landed`` names the candidate whose records land (``"class"``, ``"F"``,
    ``"P"``, ``"K"``, ``"M"`` or ``"A"``); ``violations`` maps every
    candidate the decision simulated to its violation keys.
    """
    block: tuple
    block_class: str
    component: tuple
    landed: str
    violations: Mapping[str, frozenset]


@dataclasses.dataclass(frozen=True)
class CompletionFiveHourPlan:
    """#1040: what derivation completion appends for touched blocks.

    ``targets`` maps an id to the record to append (each differs from its
    retained record or has none); ``handled`` are the five-hour ids whose
    every record lies in a decided block, retirements and the acted closes
    left to today's path excluded, which today's per-id path must not append
    again (``targets`` is a subset); ``decisions`` are sorted by block.
    """
    targets: Mapping[str, Mapping]
    handled: frozenset
    decisions: tuple


#: §5.4: the candidates of each block class, in decision order.
_COMPLETION_CANDIDATE_ORDER = {
    COMPLETION_FROZEN: ("class", "K", "M", "A"),
    COMPLETION_REPLACED: ("class", "F", "K", "M", "A"),
    COMPLETION_ADDED: ("class", "P", "M", "A"),
}
#: §5.4: each later candidate's milestone treatment.
_COMPLETION_CANDIDATE_TREATMENT = {
    "F": "frozen", "P": "main", "K": "frozen", "M": "marginal", "A": "none",
}
_RESTORATION_RANK = {"keep": 0, "restore": 1, "withhold": 2}


def _selected_milestone_view(lineage: FiveHourLineage, event_id: str) -> dict:
    """The selected active occurrence's block cost, as the record
    :meth:`FiveHourLineage.restoration` reads (``{}`` when there is none)."""
    pair = lineage.selected.get(event_id)
    for occurrence in lineage.milestones.get(event_id, ()):
        if (pair is not None and occurrence.status == "active"
                and (occurrence.rev, occurrence.content_hash) == pair):
            return {"payload": {"block_cost_usd": occurrence.block_cost_usd}}
    return {"payload": {}}


def _violation_keys(before: FiveHourLineage, after: FiveHourLineage,
                    milestone_ids, records: Mapping[str, Mapping]) -> frozenset:
    """#1040 (spec §5.4): how a candidate makes #884 decisions worse.

    For each milestone, its decision in ``after`` against its decision in
    ``before``; a milestone ``before`` does not know counts as ``keep`` with
    no reason. ``records`` are the candidate's records by id; a milestone it
    does not name, and every ``before`` decision, reads the lineage's
    selected occurrence. Keys: ``(id, "action", a)`` when the action rises
    to ``a`` (``keep`` < ``restore`` < ``withhold``), ``(id, "reason",
    code)`` per gained reason code, and ``(id, "moved")`` when both are
    ``restore`` to different values.
    """
    keys = set()
    for event_id in sorted(milestone_ids):
        old = before.restoration(
            event_id, _selected_milestone_view(before, event_id))
        record = records.get(event_id)
        new = after.restoration(
            event_id, record if record is not None
            else _selected_milestone_view(after, event_id))
        if _RESTORATION_RANK[new.action] > _RESTORATION_RANK[old.action]:
            keys.add((event_id, "action", new.action))
        for code in set(new.reason_codes) - set(old.reason_codes):
            keys.add((event_id, "reason", code))
        if (old.action == new.action == "restore"
                and old.value != new.value):
            keys.add((event_id, "moved"))
    return frozenset(keys)


def _hashable_block(payload: Mapping):
    block = _five_hour_block(payload)
    try:
        hash(block)
    except TypeError:
        return None
    return block


def plan_completion_five_hour(
    *, current: Mapping[str, Mapping], with_gap: Mapping[str, Mapping],
    without_gap: Mapping[str, Mapping], acted: frozenset,
    physical: Mapping[str, int], selection, lineage: FiveHourLineage,
    retirements: Sequence[Mapping],
) -> CompletionFiveHourPlan:
    """#1040 (spec §5.1–§5.5): completion's five-hour targets, pure.

    ``current`` holds the selected active five-hour records of the whole
    journal; ``with_gap`` / ``without_gap`` are the round's candidates by
    id (any kind) and ``acted`` the ids whose two candidates differ;
    ``physical`` maps an id to its highest physical revision (selected,
    admission-excluded or inert); ``retirements`` are the tombstone actions
    today's path appends, which land whatever is chosen and so ride along in
    every simulation.

    A block is touched when an acted close or milestone names it. Each
    touched block's closes are classified (:func:`classify_completion_close`).
    A block is OPEN, and keeps today's path, when no close it decides is
    closed in the final state; in a block that stays decided an acted close
    of rule 2 still keeps today's path. The planner owns an id only when
    every block its retained, with-gap and without-gap records name is
    decided (``handled``); it never targets another id, and every
    simulation carries today's record of each acted id it leaves to that
    path, like the retirements. Every decided block builds its whole
    close-and-milestone candidates, each carrying the block's required
    additions (records whose id the selection does not know), and the
    candidates of a component of blocks that share a close or milestone id
    are simulated through :meth:`FiveHourLineage.extended` with the append
    policy's coordinates. A component with one touched block lands the
    first candidate whose violations are contained in its additions' alone
    and strictly exceed no other candidate's; a class proposal without
    violations lands at once. Several touched blocks choose likewise with
    each other's additions carried, and their union lands only when it adds
    nothing beyond all their additions together; otherwise each lands its
    additions alone.
    """
    def payload_of(record):
        return (record or {}).get("payload") or {}

    def five_hour(record):
        return (record is not None
                and payload_of(record).get("kind") in _LINEAGE_KINDS)

    def by_block(records):
        out: dict = {}
        for event_id, record in records.items():
            if five_hour(record):
                block = _hashable_block(payload_of(record))
                if block is not None:
                    out.setdefault(block, {})[event_id] = record
        return out

    current_by_block = by_block(current)
    with_by_block = by_block(with_gap)
    without_by_block = by_block(without_gap)
    touched = set()
    for event_id in acted:
        for record in (with_gap.get(event_id), current.get(event_id)):
            if five_hour(record):
                block = _hashable_block(payload_of(record))
                if block is not None:
                    touched.add(block)

    # Every block an id's retained, with-gap or without-gap record names
    # (an unusable block key stands for a block nothing decides).
    unusable_block = object()
    blocks_of_id: dict = {}
    for table in (current, with_gap, without_gap):
        for event_id, record in table.items():
            if five_hour(record):
                block = _hashable_block(payload_of(record))
                blocks_of_id.setdefault(event_id, set()).add(
                    unusable_block if block is None else block)

    def todays_record(event_id):
        """The record today's per-id path leaves active for ``event_id``."""
        existing = current.get(event_id)
        if event_id not in acted:
            return existing
        desired = with_gap.get(event_id)
        if desired is None:
            return None  # a retirement tombstone, or nothing
        if existing is None:
            return desired
        return _preserve_non_derivable_state(existing, desired)

    def classify_blocks(not_owned):
        """§5.2 per touched block: its class and the closes the planner
        decides. A block is OPEN only when no close it decides is closed in
        the final state; an acted close of rule 2 (and an acted close the
        planner does not own) keeps today's path."""
        classes, owned_closes, todays_closes = {}, {}, set()
        for block in sorted(touched, key=repr):
            close_ids = sorted({
                event_id for records in (
                    current_by_block.get(block, {}),
                    with_by_block.get(block, {}),
                    without_by_block.get(block, {}))
                for event_id, record in records.items()
                if payload_of(record).get("kind") == "five_hour_block_close"
            })
            several = len(close_ids) > 1
            closes = {}
            for close_id in close_ids:
                close = classify_completion_close(
                    current.get(close_id), with_gap.get(close_id),
                    without_gap.get(close_id), acted=close_id in acted,
                    has_physical=close_id in physical,
                    several_close_ids=several)
                if close_id in acted and (
                        close.block_class == COMPLETION_OPEN
                        or close_id in not_owned):
                    todays_closes.add(close_id)
                elif close.block_class != COMPLETION_OPEN:
                    closes[close_id] = close
            if not closes:
                classes[block] = COMPLETION_OPEN
                continue
            kinds = {close.block_class for close in closes.values()}
            if len(closes) == 1:
                classes[block] = next(iter(kinds))
            elif kinds == {COMPLETION_ADDED}:
                classes[block] = COMPLETION_ADDED
            else:
                classes[block] = COMPLETION_FROZEN
            owned_closes[block] = closes
        return classes, owned_closes, todays_closes

    # An id whose records reach a block the planner does not decide stays
    # on today's path (the planner never writes it); deciding fewer blocks
    # only adds such ids, so this settles.
    not_owned: set = set()
    while True:
        classes, owned_closes, todays_closes = classify_blocks(not_owned)
        decided_blocks = set(owned_closes)
        reaching = {
            event_id for event_id, blocks in blocks_of_id.items()
            if blocks & decided_blocks and not blocks <= decided_blocks}
        if reaching <= not_owned:
            break
        not_owned |= reaching
    not_owned |= todays_closes

    candidates: dict = {}
    retired_ids: set = set()
    for block in sorted(owned_closes, key=repr):
        closes = owned_closes[block]
        block_class = classes[block]
        cur = current_by_block.get(block, {})
        wg = with_by_block.get(block, {})
        milestones_current = {
            event_id: record for event_id, record in cur.items()
            if payload_of(record).get("kind") == "five_hour_milestone"}
        milestones_with = {
            event_id: record for event_id, record in wg.items()
            if payload_of(record).get("kind") == "five_hour_milestone"
            and event_id not in not_owned}
        retired = frozenset(
            event_id for event_id in milestones_current
            if event_id in acted and with_gap.get(event_id) is None)
        retired_ids.update(retired)
        # Milestones of this block the planner does not own: today's record
        # stands in the chain when it stays here, else it leaves the chain.
        fixed: dict = {}
        leaving = set()
        for event_id in sorted(not_owned):
            if block not in blocks_of_id.get(event_id, ()):
                continue
            record = todays_record(event_id)
            if (record is not None and payload_of(record).get("kind")
                    == "five_hour_milestone"
                    and _hashable_block(payload_of(record)) == block):
                fixed[event_id] = record
            elif event_id in milestones_current:
                leaving.add(event_id)
        retired_chain = retired | frozenset(leaving)

        def candidate(name, *, closes=closes, block_class=block_class,
                      milestones_current=milestones_current,
                      milestones_with=milestones_with,
                      retired_chain=retired_chain, fixed=fixed):
            records: dict = {}
            for close_id, close in closes.items():
                existing = current.get(close_id)
                desired = with_gap.get(close_id)
                if name == "class":
                    target = close.target
                elif name == "F":
                    target = (close.frozen_form if existing is not None
                              else desired if close_id in acted else None)
                elif name == "P":
                    target = desired if close_id in acted else None
                else:
                    target = None
                if (close_id in acted and desired is not None
                        and close_id not in selection.by_id):
                    target = desired  # a required addition
                if target is not None and (
                        existing is None
                        or _semantic_event(target) != _semantic_event(existing)):
                    records[close_id] = dict(target)
            treatment = (
                ("replaced" if block_class == COMPLETION_REPLACED
                 else "frozen")
                if name == "class" else _COMPLETION_CANDIDATE_TREATMENT[name])
            records.update(completion_block_milestones(
                treatment=treatment, block_class=block_class,
                current=milestones_current, with_gap=milestones_with,
                acted=acted, retired=retired_chain, lineage=lineage,
                selection=selection, fixed=fixed))
            return records

        candidates[block] = {
            name: candidate(name)
            for name in _COMPLETION_CANDIDATE_ORDER[block_class]}

    decided = sorted(candidates, key=repr)
    # The planner owns an id only when every block its records name is
    # decided; today's path keeps every other id (spec §5.1).
    handled = {
        event_id for event_id, blocks in blocks_of_id.items()
        if blocks <= decided_blocks and event_id not in not_owned
        and event_id not in retired_ids}
    if not decided:
        return CompletionFiveHourPlan(
            targets={}, handled=frozenset(handled), decisions=())
    # Today's records of the acted ids the planner leaves to that path in a
    # decided block's reach: they land whatever is chosen, so every
    # candidate's simulation carries them (like the retirements).
    todays_records: dict = {}
    for event_id in sorted(not_owned & acted):
        if not blocks_of_id.get(event_id, set()) & decided_blocks:
            continue
        record = todays_record(event_id)
        existing = current.get(event_id)
        if record is not None and (
                existing is None
                or _semantic_event(record) != _semantic_event(existing)):
            todays_records[event_id] = dict(record)

    # §5.4 components: blocks sharing a close or milestone id, over the
    # lineage's occurrences and every candidate's records.
    id_blocks: dict = {}
    block_ids: dict = {}

    def link(event_id, block):
        if block is None:
            return
        id_blocks.setdefault(event_id, set()).add(block)
        block_ids.setdefault(block, set()).add(event_id)

    for table in (lineage.milestones, lineage.closes):
        for event_id, items in table.items():
            for occurrence in items:
                if occurrence.status == "active" and occurrence.block is not None:
                    try:
                        hash(occurrence.block)
                    except TypeError:
                        continue
                    link(event_id, occurrence.block)
    for block in decided:
        for records in candidates[block].values():
            for event_id, record in records.items():
                link(event_id, _hashable_block(payload_of(record)))
    for event_id, record in todays_records.items():
        link(event_id, _hashable_block(payload_of(record)))
    components: dict = {}
    for start in decided:
        if start in components:
            continue
        component: set = set()
        frontier = [start]
        while frontier:
            block = frontier.pop()
            if block in component:
                continue
            component.add(block)
            for event_id in block_ids.get(block, ()):
                frontier.extend(id_blocks.get(event_id, ()) - component)
        frozen_component = tuple(sorted(component, key=repr))
        for block in component:
            components[block] = frozen_component

    order_index = {event_id: index for index, event_id in enumerate(with_gap)}
    retirement_blocks = []
    for action in retirements:
        record = current.get(action.get("id"))
        retirement_blocks.append(
            (action, _hashable_block(payload_of(record))
             if five_hour(record) else None))

    def landed_records(choices):
        out: dict = {}
        for block, name in choices.items():
            out.update(candidates[block][name])
        return out

    targets: dict = {}
    decisions: list = []
    done: set = set()
    for start in decided:
        component = components[start]
        if start in done:
            continue
        members = set(component)
        touched_here = [block for block in component if block in candidates]
        done.update(touched_here)
        milestone_ids = {
            event_id for block in component
            for event_id in block_ids.get(block, ())
            if event_id in lineage.milestones
            or any(payload_of(records.get(event_id)).get("kind")
                   == "five_hour_milestone"
                   for records in candidates.get(block, {}).values())
        }
        retired_actions = [dict(action) for action, block in retirement_blocks
                           if block in members]
        todays_here = {
            event_id: record for event_id, record in todays_records.items()
            if blocks_of_id.get(event_id, set()) & members
            or _hashable_block(payload_of(record)) in members}
        milestone_ids |= {
            event_id for event_id, record in todays_here.items()
            if payload_of(record).get("kind") == "five_hour_milestone"}
        cache: dict = {}

        def violations(records, *, retired_actions=retired_actions,
                       milestone_ids=milestone_ids, cache=cache,
                       todays_here=todays_here):
            records = {**todays_here, **records}
            key = _canonical_bytes([
                [event_id, _semantic_event(records[event_id])]
                for event_id in sorted(records)])
            if key in cache:
                return cache[key]
            actions, plain = [], []
            for event_id in sorted(records):
                record = records[event_id]
                existing = current.get(event_id)
                if existing is not None and (_semantic_event(existing)
                                             == _semantic_event(record)):
                    continue
                if event_id in physical:
                    actions.append({
                        "action": "replace", "id": event_id,
                        "rev": physical[event_id] + 1,
                        "at": str(record.get("at")),
                        "payload": dict(record.get("payload") or {}),
                    })
                else:
                    plain.append(record)
            actions.extend(retired_actions)
            if not actions and not plain:
                # Nothing would be appended: every decision stays as it is.
                cache[key] = frozenset()
                return cache[key]
            actions.sort(key=lambda action: action["id"])
            plain.sort(key=lambda record: order_index.get(
                record.get("id"), len(order_index)))
            after = lineage.extended(batch_actions=actions, plain=plain)
            result = _violation_keys(lineage, after, milestone_ids, records)
            cache[key] = result
            return result

        def choose(block, carried):
            order = _COMPLETION_CANDIDATE_ORDER[classes[block]]
            own = candidates[block]
            found = {"class": violations({**carried, **own["class"]})}
            if not found["class"]:
                return "class", found
            for name in order[1:]:
                found[name] = violations({**carried, **own[name]})
            ceiling = found["A"]
            for name in order:
                if found[name] <= ceiling and not any(
                        found[other] < found[name] for other in order):
                    return name, found
            return "A", found  # unreachable: A always qualifies

        if len(touched_here) == 1:
            [block] = touched_here
            name, found = choose(block, {})
            choices = {block: name}
            found_by_block = {block: found}
        else:
            choices, found_by_block = {}, {}
            for block in touched_here:
                carried: dict = {}
                for other in touched_here:
                    if other != block:
                        carried.update(candidates[other]["A"])
                choices[block], found_by_block[block] = choose(block, carried)
            alone = landed_records({block: "A" for block in touched_here})
            if not violations(landed_records(choices)) <= violations(alone):
                choices = {block: "A" for block in touched_here}
        targets.update(landed_records(choices))
        for block in touched_here:
            decisions.append(BlockDecision(
                block=block, block_class=classes[block], component=component,
                landed=choices[block],
                violations=dict(found_by_block[block])))
    decisions.sort(key=lambda decision: repr(decision.block))
    if not set(targets) <= handled:
        # Both the planner and today's path would write these ids.
        raise RuntimeError(
            "#1040 planner targets outside the ids it owns: "
            f"{sorted(set(targets) - handled)}")
    return CompletionFiveHourPlan(
        targets={event_id: targets[event_id] for event_id in sorted(targets)},
        handled=frozenset(handled),
        decisions=tuple(decisions),
    )

_SNAPSHOT_IDENTITY_FIELDS = (
    "account_key", "week_start_date", "week_start_at", "week_end_at",
    "weekly_percent", "five_hour_percent", "five_hour_window_key",
    "weekly_observation_held", "source",
)
_SNAPSHOT_DEPENDENT_KINDS = frozenset({
    "percent_milestone", "five_hour_milestone",
})
_MILESTONE_BASE_DEPENDENCIES = {
    "usage_snapshot_ref": "snapshot_accept",
    "cost_snapshot_ref": "weekly_cost_snapshot",
}


# ── Reviewed weekly decisions (#797/#823/#840 spec 4.7, Q5, D4, U2) ─────────
#
# Op kind `claude_weekly_observation_decision_v2` (and the previous release's
# `claude_weekly_observation_decision`) carries one reviewed manifest. Payload
# schema 1 lists `decisions`, schema 2 `weekly_axis_decisions` (and snapshot
# identity decisions), both of `hold` / `accept`. Schema 3 adds `retract`
# WITHOUT a new projection: `weekly_axis_decisions` projects each retraction as
# an ordinary `hold` (the previous release's completion, which recognizes only
# a literal `hold`, then sees a weekly exclusion) and the canonical
# `weekly_axis_retractions` list — sorted, unique, each naming a same-op
# projected hold — gives it its causal meaning. This is the ONE strict decoder
# every current consumer of a retained payload uses.

REVIEWED_WEEKLY_SCHEMA_VERSIONS = (1, 2, 3)
REVIEWED_WEEKLY_DISPOSITIONS = ("hold", "accept", "retract")
_REVIEWED_WEEKLY_IDENTITY_KEYS = frozenset({
    "acceptedObservationId", "replayObservationId", "disposition",
})


@dataclasses.dataclass(frozen=True)
class ReviewedWeeklyDecision:
    """One decoded reviewed weekly decision payload: its schema version, the
    disposition per observation (``hold``, ``accept`` or ``retract``, in
    payload order), and its snapshot identity decisions."""

    version: int
    dispositions: Mapping[str, str]
    identity_decisions: tuple


def decode_reviewed_weekly_payload(payload) -> ReviewedWeeklyDecision:
    """Strictly decode one retained reviewed weekly decision payload, or raise
    :class:`RederiveConflict`."""
    if not isinstance(payload, Mapping):
        raise RederiveConflict("malformed retained reviewed weekly decision op")
    version = payload.get("schema_version")
    if type(version) is not int or version not in REVIEWED_WEEKLY_SCHEMA_VERSIONS:
        raise RederiveConflict("malformed retained reviewed weekly decision op")
    decisions = (payload.get("decisions") if version == 1
                 else payload.get("weekly_axis_decisions"))
    identity = ([] if version == 1
                else payload.get("snapshot_identity_decisions"))
    if (not isinstance(decisions, list) or not isinstance(identity, list)
            or not decisions and not identity):
        raise RederiveConflict("malformed retained reviewed weekly decision op")
    dispositions: dict = {}
    for item in decisions:
        if (not isinstance(item, dict)
                or set(item) != {"observationId", "disposition"}
                or not isinstance(item["observationId"], str)
                or not item["observationId"]
                or item["disposition"] not in ("hold", "accept")):
            raise RederiveConflict(
                "malformed retained reviewed weekly decision entry")
        obs_id = item["observationId"]
        if obs_id in dispositions:
            raise RederiveConflict(
                f"unknown or duplicate reviewed weekly observation: {obs_id}")
        dispositions[obs_id] = item["disposition"]
    retractions = payload.get("weekly_axis_retractions")
    if version < 3:
        if "weekly_axis_retractions" in payload:
            raise RederiveConflict(
                "malformed retained reviewed weekly decision op")
    else:
        if (not isinstance(retractions, list)
                or any(not isinstance(r, str) or not r for r in retractions)
                or retractions != sorted(set(retractions))):
            raise RederiveConflict(
                "reviewed weekly retractions must be a canonical list")
        for obs_id in retractions:
            if dispositions.get(obs_id) != "hold":
                raise RederiveConflict(
                    "reviewed weekly retraction references no same-op hold: "
                    + obs_id)
            dispositions[obs_id] = "retract"
    seen: set = set()
    for item in identity:
        if (not isinstance(item, dict)
                or set(item) != _REVIEWED_WEEKLY_IDENTITY_KEYS
                or not all(isinstance(item.get(key), str) and item[key]
                           for key in ("acceptedObservationId",
                                       "replayObservationId"))
                or item.get("disposition") not in ("preserve", "rederive")
                or item["acceptedObservationId"] == item["replayObservationId"]
                or item["acceptedObservationId"] in seen
                or item["replayObservationId"] in seen):
            raise RederiveConflict(
                "malformed retained snapshot identity decision")
        seen.update((item["acceptedObservationId"], item["replayObservationId"]))
    return ReviewedWeeklyDecision(version, dispositions, tuple(identity))


def effective_reviewed_weekly_dispositions(payloads) -> dict:
    """The effective disposition per observation over ``payloads`` in journal
    order: the latest logical decision wins."""
    effective: dict = {}
    for payload in payloads:
        effective.update(decode_reviewed_weekly_payload(payload).dispositions)
    return effective


def reviewed_weekly_payload(*, version: int, decisions, base: Mapping) -> dict:
    """The persisted payload of one reviewed manifest: ``base`` (kind, bound,
    reason and the other shared fields) plus ``decisions`` as
    ``[{"observationId", "disposition"}]``, with each retraction projected as
    an ordinary `hold` and listed in `weekly_axis_retractions` (schema 3)."""
    payload = dict(base)
    projected = sorted(
        ({"observationId": item["observationId"],
          "disposition": ("hold" if item["disposition"] == "retract"
                          else item["disposition"])}
         for item in decisions),
        key=lambda item: item["observationId"])
    if version == 1:
        payload["decisions"] = projected
    else:
        payload["weekly_axis_decisions"] = projected
    if version >= 3:
        payload["weekly_axis_retractions"] = sorted(
            item["observationId"] for item in decisions
            if item["disposition"] == "retract")
    return payload


def _milestone_dependencies(kind: str) -> dict[str, str]:
    dependencies = dict(_MILESTONE_BASE_DEPENDENCIES)
    dependencies["reset_event_ref"] = (
        "week_reset" if kind == "percent_milestone" else "five_hour_credit"
    )
    return dependencies

_BLOCK_WEEKLY_AXIS_FIELDS = frozenset({
    "seven_day_pct_at_block_start",
    "seven_day_pct_at_block_end",
    "crossed_seven_day_reset",
})


def _observation_identity(record: Mapping):
    """Comparable reading, only when every discriminator is retained."""
    payload = record.get("payload") or {}
    if payload.get("kind") != "snapshot_accept":
        return None
    if any(field not in payload for field in _SNAPSHOT_IDENTITY_FIELDS):
        return None
    reading = {
        key: value for key, value in payload.items()
        if key not in {"captured_at_utc", "payload_json"}
    }
    if "payload_json" in payload:
        try:
            raw = json.loads(payload["payload_json"])
        except (TypeError, ValueError):
            raw = payload["payload_json"]
        if isinstance(raw, dict):
            raw = dict(raw)
            raw.pop("capturedAt", None)
            raw.pop("captured_at", None)
        reading["payload_json"] = raw
    return _canonical_bytes(reading)


def _iso_instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None


def _raw_supports_snapshot(raw: Mapping, event: Mapping, *, held: bool) -> bool:
    """Bind a journaled acceptance to its exact retained origin observation."""
    raw_payload = raw.get("payload") or {}
    event_payload = event.get("payload") or {}
    if (
        raw.get("t") != "obs" or raw.get("provider") != "claude"
        or raw.get("src") != "record-usage"
        or event.get("id") != f"sa:{raw.get('id')}"
        or event_payload.get("kind") != "snapshot_accept"
        or raw.get("account") != event_payload.get("account_key")
        or raw.get("at") != event.get("at")
        or raw_payload.get("captured_at") != event_payload.get("captured_at_utc")
        or raw_payload.get("source") != event_payload.get("source")
        or raw_payload.get("five_hour_percent")
        != event_payload.get("five_hour_percent")
        or raw_payload.get("five_hour_resets_at")
        != event_payload.get("five_hour_resets_at")
    ):
        return False
    try:
        encoded = json.loads(event_payload["payload_json"])
    except (KeyError, TypeError, ValueError):
        return False
    if not isinstance(encoded, dict) or (
        encoded.get("source") != raw_payload.get("source")
        or encoded.get("capturedAt") != raw_payload.get("captured_at")
        or encoded.get("fiveHourPercent")
        != raw_payload.get("five_hour_percent")
    ):
        return False
    if held:
        return (
            event_payload.get("weekly_observation_held") == 1
            and encoded.get("weeklyObservationHeld") is True
            and encoded.get("rawWeeklyPercent")
            == raw_payload.get("weekly_percent")
            and encoded.get("weeklyPercent")
            == event_payload.get("weekly_percent")
        )
    return (
        event_payload.get("weekly_observation_held") in (None, 0)
        and encoded.get("weeklyObservationHeld") is not True
        and encoded.get("weeklyPercent") == raw_payload.get("weekly_percent")
        == event_payload.get("weekly_percent")
    )


def _same_acceptance_transition(
    old: Mapping, new: Mapping, old_raw: Mapping, new_raw: Mapping,
    between: Iterable[Mapping], *,
    allow_intervening_same_account_observations: bool = False,
) -> bool:
    """Recognize a replay-only candidate ahead of one retained acceptance.

    The old non-held event remains the durable decision. The held candidate
    carries a lower raw weekly reading into that same effective percentage;
    it is not an equivalent raw observation. This check uses the exact two
    retained origins and their order, with no elapsed-time or source-blind
    pairing. If any causal discriminator differs, the normal diff remains.
    """
    old_payload = old.get("payload") or {}
    new_payload = new.get("payload") or {}
    old_raw_payload = old_raw.get("payload") or {}
    new_raw_payload = new_raw.get("payload") or {}
    if not (
        _raw_supports_snapshot(old_raw, old, held=False)
        and old_raw.get("account") == new_raw.get("account")
        and _iso_instant(new.get("at")) is not None
        and _iso_instant(old.get("at")) is not None
        and _iso_instant(new["at"]) < _iso_instant(old["at"])
    ):
        return False
    axes = (
        "account_key", "week_start_date", "week_end_date",
        "week_start_at", "week_end_at", "weekly_percent",
        "five_hour_percent", "five_hour_window_key", "page_url",
    )
    if any(old_payload.get(axis) != new_payload.get(axis) for axis in axes):
        return False
    old_reset = _iso_instant(old_payload.get("five_hour_resets_at"))
    new_reset = _iso_instant(new_payload.get("five_hour_resets_at"))
    if (old_reset is None) != (new_reset is None):
        return False
    if old_reset is not None and abs(
        (old_reset - new_reset).total_seconds()
    ) > 1:
        return False
    try:
        weekly_reset_gap = abs(
            float(old_raw_payload["resets_at"])
            - float(new_raw_payload["resets_at"])
        )
    except (KeyError, TypeError, ValueError):
        return False
    if weekly_reset_gap > 1:
        return False

    same_raw_reading = (
        _raw_supports_snapshot(new_raw, new, held=False)
        and old_raw_payload.get("weekly_percent")
        == new_raw_payload.get("weekly_percent")
        and old_raw_payload.get("source") == new_raw_payload.get("source")
        and _observation_identity(old) == _observation_identity(new)
    )
    held_predecessor = (
        _raw_supports_snapshot(new_raw, new, held=True)
        and new_raw_payload.get("source") == "statusline"
        and isinstance(old_raw_payload.get("weekly_percent"), (int, float))
        and isinstance(new_raw_payload.get("weekly_percent"), (int, float))
        and abs(
            float(old_raw_payload["weekly_percent"])
            - float(new_raw_payload["weekly_percent"]) - 1.0
        ) < 1e-9
        and new_payload.get("weekly_percent")
        == old_raw_payload["weekly_percent"]
    )
    if not (same_raw_reading or held_predecessor):
        return False

    # The two origins must be consecutive within this account's complete
    # replay input. Any retained operator record can change a floor, account,
    # or cost dependency, and any intervening Claude observation for this
    # account can change reset/credit/clamp state even when its displayed axes
    # differ. Other providers and other Claude accounts cannot affect this
    # account's fold.
    for record in between:
        if record.get("t") == "op":
            return False
        if (
            record.get("t") == "obs"
            and record.get("provider") == "claude"
            and record.get("account") == old_raw.get("account")
            and not allow_intervening_same_account_observations
        ):
            return False
    return True


def _contains_value(value, wanted: str) -> bool:
    if value == wanted:
        return True
    if isinstance(value, Mapping):
        return any(_contains_value(item, wanted) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_value(item, wanted) for item in value)
    return False


def _contains_any_value(value, wanted: set[str]) -> bool:
    if isinstance(value, str):
        return value in wanted
    if isinstance(value, Mapping):
        return any(_contains_any_value(item, wanted) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_any_value(item, wanted) for item in value)
    return False


def _reconcile_snapshot_identities(
    current: Mapping[str, Mapping], desired: dict[str, Mapping], selection,
    raw_observations: Iterable[Mapping], snapshot_identity_decisions=(),
) -> None:
    """Keep an existing accepted decision across a replay policy change.

    A held candidate may copy an already-observed weekly percent from scratch
    state and be mistaken for the later genuine crossing that the journal
    actually accepted. The two raw weekly readings differ. Keep the selected
    decision only when exact retained origins prove their causal order, axes,
    and dependency graph; otherwise leave the ordinary correction diff intact.
    """
    # #1004 D4: any sequence (a coordinate-backed one is read, never listed).
    if not isinstance(raw_observations, Sequence):
        raw_observations = tuple(raw_observations)
    reviewed_pairs = tuple(snapshot_identity_decisions or ())
    if not raw_observations and not reviewed_pairs:
        return
    reviewed_set = set()
    identity_ids = set()
    for pair in reviewed_pairs:
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or not all(isinstance(item, str) and item for item in pair)
                or pair[0] == pair[1]
                or pair[0] in identity_ids or pair[1] in identity_ids):
            raise RederiveConflict(
                "malformed or ambiguous snapshot identity decision"
            )
        identity_ids.update(pair)
        reviewed_set.add(tuple(pair))
    for raw_id in identity_ids:
        if sum(record.get("id") == raw_id for record in raw_observations) != 1:
            raise RederiveConflict(
                "snapshot identity decision raw ids must occur exactly once: "
                + raw_id
            )
    needed_raw_ids = {
        event_id[3:] for event_id, record in current.items()
        if event_id.startswith("sa:") and event_id not in desired
        and (record.get("payload") or {}).get("kind") == "snapshot_accept"
    } | {
        event_id[3:] for event_id, record in desired.items()
        if event_id.startswith("sa:") and event_id not in current
        and event_id not in selection.by_id
        and (record.get("payload") or {}).get("kind") == "snapshot_accept"
    }
    if not needed_raw_ids:
        return
    # #1004 D4: positions only; a record is decoded again where it is read,
    # so the collection pass keeps no observation alive.
    raw_positions = {}
    repeated_raw_ids = set()
    for index, record in enumerate(raw_observations):
        raw_id = record.get("id")
        if (
            record.get("t") != "obs"
            or record.get("provider") != "claude"
            or raw_id not in needed_raw_ids
        ):
            continue
        if raw_id in raw_positions:
            repeated_raw_ids.add(raw_id)
        else:
            raw_positions[raw_id] = index
    # A repeated physical raw id has no unique order position. Leave its
    # accepted decision to the normal diff instead of choosing an occurrence.
    for raw_id in repeated_raw_ids:
        del raw_positions[raw_id]
    unmatched_current = {
        event_id: record for event_id, record in current.items()
        if event_id not in desired
        and event_id.startswith("sa:")
        and event_id[3:] in raw_positions
        and (record.get("payload") or {}).get("kind") == "snapshot_accept"
    }
    unmatched_desired = {
        event_id: record for event_id, record in desired.items()
        if event_id not in current
        and event_id not in selection.by_id
        and event_id.startswith("sa:")
        and event_id[3:] in raw_positions
        and (record.get("payload") or {}).get("kind") == "snapshot_accept"
    }
    candidates: dict[str, list[str]] = {}
    for old_id, old in unmatched_current.items():
        old_index = raw_positions[old_id[3:]]
        old_raw = raw_observations[old_index]
        candidates[old_id] = []
        for new_id, new in unmatched_desired.items():
            new_index = raw_positions[new_id[3:]]
            if (
                new_index < old_index
                and _same_acceptance_transition(
                    old, new, old_raw, raw_observations[new_index],
                    raw_observations[new_index + 1:old_index],
                    allow_intervening_same_account_observations=(
                        (old_id[3:], new_id[3:]) in reviewed_set
                    ),
                )
            ):
                candidates[old_id].append(new_id)

    consumed_reviewed = set()
    for old_id, matches in sorted(candidates.items()):
        if len(matches) != 1:
            continue
        new_id = matches[0]
        if sum(new_id in group for group in candidates.values()) != 1:
            continue
        old = unmatched_current[old_id]
        new = unmatched_desired[new_id]
        # Implicit sync-week events are keyed by the originating raw
        # observation too. A percent milestone can reference both the usage
        # snapshot and the cost snapshot from that observation. Preserve the
        # cost identity only when its complete priced content agrees; the two
        # capture bounds may differ with the accepted origins.
        old_cost_prefix = f"wcs:{old_id[3:]}:"
        new_cost_prefix = f"wcs:{new_id[3:]}:"
        cost_pairs = {}
        for old_cost_id, old_cost in current.items():
            if not old_cost_id.startswith(old_cost_prefix):
                continue
            new_cost_id = new_cost_prefix + old_cost_id[len(old_cost_prefix):]
            new_cost = desired.get(new_cost_id)
            if new_cost is None or new_cost_id in current:
                continue
            old_cost_payload = old_cost.get("payload") or {}
            new_cost_payload = new_cost.get("payload") or {}
            if (
                old_cost_payload.get("kind") != "weekly_cost_snapshot"
                or new_cost_payload.get("kind") != "weekly_cost_snapshot"
            ):
                continue
            def priced_content(payload):
                return {
                    key: value for key, value in payload.items()
                    if key not in {"captured_at_utc", "range_end_iso"}
                }
            if priced_content(old_cost_payload) == priced_content(new_cost_payload):
                cost_pairs[new_cost_id] = (old_cost_id, old_cost)
        dependent_updates = {}
        safe = True
        for event_id in set(current) | set(desired):
            now = current.get(event_id)
            proposed = desired.get(event_id)
            now_payload = (now or {}).get("payload") or {}
            proposed_payload = (proposed or {}).get("payload") or {}
            refers_old = _contains_value(now_payload, old_id) or any(
                _contains_value(now_payload, old_cost_id)
                for old_cost_id, _ in cost_pairs.values()
            )
            refers_new = _contains_value(proposed_payload, new_id) or any(
                _contains_value(proposed_payload, new_cost_id)
                for new_cost_id in cost_pairs
            )
            if not (refers_old or refers_new):
                continue
            if (
                now is None or proposed is None
                or now_payload.get("kind") not in _SNAPSHOT_DEPENDENT_KINDS
                or proposed_payload.get("kind") != now_payload.get("kind")
                or now_payload.get("usage_snapshot_ref") != old_id
                or proposed_payload.get("usage_snapshot_ref") != new_id
                or any(
                    now_payload.get(field) != proposed_payload.get(field)
                    for field in (
                        "account_key", "week_start_date",
                        "five_hour_window_key", "reset_event_ref",
                        "percent_threshold",
                    )
                )
                or _contains_value(
                    {k: v for k, v in proposed_payload.items()
                     if k != "usage_snapshot_ref"}, new_id,
                )
            ):
                safe = False
                break
            new_cost_ref = proposed_payload.get("cost_snapshot_ref")
            if new_cost_ref in cost_pairs:
                old_cost_ref = cost_pairs[new_cost_ref][0]
                if now_payload.get("cost_snapshot_ref") != old_cost_ref:
                    safe = False
                    break
            elif new_cost_ref != now_payload.get("cost_snapshot_ref"):
                safe = False
                break
            # A crossing is a retained fact of the selected acceptance,
            # including observed weekly axis, capture clock and money. The
            # scratch candidate's ref/clock/cost belong to the rejected held
            # origin. Keep the whole selected dependent event.
            dependent_updates[event_id] = now
        if not safe:
            continue
        del desired[new_id]
        desired[old_id] = old
        for new_cost_id, (old_cost_id, old_cost) in cost_pairs.items():
            del desired[new_cost_id]
            desired[old_cost_id] = old_cost
        desired.update(dependent_updates)
        pair = (old_id[3:], new_id[3:])
        if pair in reviewed_set:
            consumed_reviewed.add(pair)

    unused = reviewed_set - consumed_reviewed
    if unused:
        accepted, replay = sorted(unused)[0]
        raise RederiveConflict(
            "snapshot identity decision failed automatic account, window, "
            "axis, binding, dependency, or uniqueness checks: "
            f"{accepted} -> {replay}"
        )


def _suppression_deletes_snapshot(record: Mapping, *, weekly: bool,
                                  five_hour: bool) -> bool:
    """Whether the stale-replica suppressions naming one snapshot delete it
    (#825/#832 spec 4.7 revisions 6 and 7), as the journal appliers
    materialize them. A five-hour credit retires an unheld row's five-hour
    reading in place and deletes a held row; a weekly suppression makes a row
    that carries its own five-hour reading weekly-held and deletes a row that
    keeps no evidence on either axis. Both retirements together delete the
    row, in either order."""
    payload = record.get("payload") or {}
    held = payload.get("weekly_observation_held") == 1
    carries_five_hour = (
        payload.get("five_hour_percent") is not None
        and payload.get("five_hour_resets_at") is not None
        and payload.get("source") != "record-credit"
    )
    if weekly and five_hour:
        return True
    if five_hour:
        return held
    if weekly:
        return not carries_five_hour
    return False


def _without_snapshot_suppression(payload: Mapping) -> Mapping:
    """``payload`` without its `weekly_usage_snapshots` stale-replica
    suppression list. Since spec 4.7 revision 7 (fix round K16b) a
    suppression that names a reviewed-held snapshot can leave it
    materialized (a weekly credit keeps a row carrying its own five-hour
    reading weekly-held), and that list is then a retirement effect on the
    snapshot, not a reference that depends on it."""
    if ("suppression" not in payload
            or payload.get("suppression_table", "weekly_usage_snapshots")
            != "weekly_usage_snapshots"):
        return payload
    return {key: value for key, value in payload.items() if key != "suppression"}


def _retraction_unsupported_resets(
    current: Mapping[str, Mapping], desired: Mapping[str, Mapping],
    retracted_snapshot_ids, raw_observations: Iterable[Mapping] = (),
) -> frozenset:
    """#797/#823/#840 spec 4.7 (D4): the current `week_reset` events the
    corrected replay no longer supports BECAUSE of a retraction — absent from
    ``desired``, and whose causal predecessor was a retracted observation.

    The predecessor is read from the raw journal observation stream
    (``raw_observations``), not from the current graph: the latest
    weekly-bearing Claude observation of the reset's account (attributed as
    the replay attributes it, `_cctally_record._pipeline_claude_usage`)
    captured strictly before the reset's originating low observation (whose
    identity the reset follows, `_lib_journal` `week_reset_identity_parts`),
    or before its `detected_at_utc` when it has no origin; equal instants
    break by journal order. Whether the replay accepted that observation,
    held it or stored no snapshot for it does not matter: a stale replay an
    earlier reviewed `hold` turned into a held snapshot, or never accepted,
    is still the reading the reset was detected against, and the latest
    unheld snapshot before the origin is then an unrelated genuine reading
    (review of 79847ac5a, D1). Exact identity, never a percentage
    comparison. A reset the corrected replay still produces (alternate
    support, review F4) is in ``desired`` and is never here."""
    retracted = frozenset(retracted_snapshot_ids or ())
    if not retracted:
        return frozenset()
    weekly: dict = {}
    captured: dict = {}
    for index, record in enumerate(raw_observations or ()):
        payload = record.get("payload") or {}
        observation_id = record.get("id")
        if (record.get("t") != "obs" or record.get("provider") != "claude"
                or "resets_at" not in payload
                or payload.get("weekly_percent") is None
                or not isinstance(observation_id, str) or not observation_id):
            continue
        instant = _iso_instant(payload.get("captured_at") or record.get("at"))
        if instant is None:
            continue
        account = record.get("account") or _lib_accounts.UNATTRIBUTED
        weekly.setdefault(account, []).append((instant, index, observation_id))
        captured.setdefault(observation_id, instant)
    unsupported = set()
    for event_id, record in current.items():
        payload = record.get("payload") or {}
        if payload.get("kind") != "week_reset" or event_id in desired:
            continue
        origin = payload.get("origin_observation_id")
        origin = origin if isinstance(origin, str) and origin else None
        origin_payload = (
            (current.get(f"sa:{origin}") or {}).get("payload") or {}
            if origin is not None else {})
        at = ((captured.get(origin) if origin is not None else None)
              or _iso_instant(origin_payload.get("captured_at_utc"))
              or _iso_instant(payload.get("detected_at_utc")))
        if at is None:
            continue
        prior = [item for item in weekly.get(payload.get("account_key"), ())
                 if item[0] < at and item[2] != origin]
        if prior and f"sa:{max(prior)[2]}" in retracted:
            unsupported.add(event_id)
    return frozenset(unsupported)


def _retraction_scope(current: Mapping[str, Mapping], retracted_snapshot_ids,
                      unsupported_resets) -> frozenset:
    """The event ids whose reviewed actions a retraction itself causes and
    the reviewed delta therefore always carries (spec 4.7: explicit
    retractions survive the baseline/reviewed plan intersection): the
    retracted snapshots, the resets they leave unsupported, and every
    current milestone citing either."""
    scope = set(retracted_snapshot_ids or ()) | set(unsupported_resets or ())
    if not scope:
        return frozenset()
    for event_id, record in current.items():
        payload = record.get("payload") or {}
        if payload.get("kind") not in _SNAPSHOT_DEPENDENT_KINDS:
            continue
        if (payload.get("usage_snapshot_ref") in scope
                or payload.get("reset_event_ref") in scope):
            scope.add(event_id)
    return frozenset(scope)


def _preserve_reviewed_hold_dependencies(
    current: Mapping[str, Mapping], desired: dict[str, Mapping],
    reviewed_weekly_hold_ids=(), historical_close_markers=None,
    unsupported_resets=frozenset(),
) -> None:
    """Keep frozen five-hour facts reached from exact reviewed holds.

    An exact weekly-axis hold must not erase the same observation's retained
    five-hour crossing. Milestones are historical facts, so ``desired``
    receives their whole accepted payload, and the usage/cost reference graph
    is closed with the exact currently selected events. Under a frozen or a
    replaced close (#875), :func:`_resolve_closed_block_milestones` still
    settles such a milestone's marginal against its final predecessor; its
    block cost stays the accepted one. A closed block may update only its
    weekly axes; its five-hour close boundary, reading, money, tokens and
    children stay the selected frozen fact, so that update keeps the close's
    money and its block stays frozen (spec §4.1). Any unfamiliar reference
    shape refuses rather than being guessed or rewritten.

    #797/#823/#840 spec 4.7 (D4): ``reviewed_weekly_hold_ids`` names the
    ordinary holds only — a retraction is never preserved — and a held
    milestone whose reset a retraction left unsupported
    (``unsupported_resets``) is not kept: keeping it would restore that
    reset, and the retraction's tombstone wins over the dangling milestone,
    which then follows the corrected replay.
    """
    snapshot_ids = {
        f"sa:{raw_id}" for raw_id in reviewed_weekly_hold_ids
        if isinstance(raw_id, str) and raw_id
    }
    if not snapshot_ids:
        return
    weekly_named: set = set()
    five_hour_named: set = set()
    for record in (*current.values(), *desired.values()):
        payload = record.get("payload") or {}
        suppression = payload.get("suppression")
        if suppression is None:
            continue
        if not isinstance(suppression, list) or any(
            not isinstance(item, str) for item in suppression
        ):
            raise RederiveConflict(
                "reviewed weekly hold suppression has an unfamiliar shape: "
                + str(record.get("id"))
            )
        named = snapshot_ids & set(suppression)
        if (payload.get("suppression_table", "weekly_usage_snapshots")
                != "weekly_usage_snapshots"):
            continue
        if payload.get("kind") == "five_hour_credit":
            five_hour_named.update(named)
        else:
            weekly_named.update(named)
    # #825/#832 spec 4.7 revision 7 (*Versioning and replay*, fix round
    # K16b): suppression-list membership no longer implies whole-snapshot
    # deletion. Each axis's stale-replica suppression retires its own axis
    # (`_cctally_journal._apply_reset_with_suppression`), so a snapshot is
    # deleted only where the materialization deletes it.
    suppressed_snapshot_ids = {
        snapshot_id for snapshot_id in weekly_named | five_hour_named
        if _suppression_deletes_snapshot(
            (desired.get(snapshot_id) or current.get(snapshot_id) or {}),
            weekly=snapshot_id in weekly_named,
            five_hour=snapshot_id in five_hour_named)
    }
    if suppressed_snapshot_ids:
        for event_id, record in (*current.items(), *desired.items()):
            payload = record.get("payload") or {}
            if (payload.get("kind") in _SNAPSHOT_DEPENDENT_KINDS
                    and payload.get("usage_snapshot_ref")
                    in suppressed_snapshot_ids):
                raise RederiveConflict(
                    "reviewed weekly hold suppression would leave a dependent "
                    "milestone: " + event_id
                )
        # A retained reset or credit suppression is the historical fact that
        # this snapshot was deleted. Make that destructive effect explicit in
        # the target graph instead of resurrecting the held observation's
        # snapshot while silently retaining the suppression beside it.
        for snapshot_id in suppressed_snapshot_ids:
            desired.pop(snapshot_id, None)
        snapshot_ids -= suppressed_snapshot_ids
    for event_id, record in current.items():
        payload = record.get("payload") or {}
        usage_ref = payload.get("usage_snapshot_ref")
        if usage_ref not in snapshot_ids:
            if _contains_any_value(_without_snapshot_suppression(payload),
                                   snapshot_ids):
                raise RederiveConflict(
                    "reviewed weekly hold dependency has an unfamiliar shape: "
                    + event_id
                )
            continue
        if payload.get("kind") not in _SNAPSHOT_DEPENDENT_KINDS:
            raise RederiveConflict(
                "reviewed weekly hold dependency has an unfamiliar kind: "
                + event_id
            )
        if payload.get("reset_event_ref") in unsupported_resets:
            continue
        snapshot = current.get(usage_ref)
        if ((snapshot or {}).get("payload") or {}).get("kind") != "snapshot_accept":
            raise RederiveConflict(
                "reviewed weekly hold dependency lacks its accepted snapshot: "
                + event_id
            )
        desired[event_id] = record
        desired[usage_ref] = snapshot
        for field, expected_kind in _milestone_dependencies(
            payload.get("kind")
        ).items():
            if field == "usage_snapshot_ref":
                continue
            reference = payload.get(field)
            if reference in _EMPTY_REFERENCE_SENTINELS:
                continue
            dependency = current.get(reference)
            if ((dependency or {}).get("payload") or {}).get("kind") \
                    != expected_kind:
                raise RederiveConflict(
                    f"reviewed weekly hold dependency lacks its {field}: "
                    f"{event_id}"
                )
            desired[reference] = dependency

    # The reviewed replay can introduce a milestone for an accepted reset
    # while the exact snapshot it cites is independently held. Close those
    # newly introduced edges against the currently selected graph too; looking
    # only at current milestones leaves the new event pointing at a snapshot
    # the same plan tombstones.
    for event_id, record in tuple(desired.items()):
        payload = record.get("payload") or {}
        usage_ref = payload.get("usage_snapshot_ref")
        if usage_ref not in snapshot_ids:
            if _contains_any_value(_without_snapshot_suppression(payload),
                                   snapshot_ids):
                raise RederiveConflict(
                    "reviewed weekly hold dependency has an unfamiliar shape: "
                    + event_id
                )
            continue
        if payload.get("kind") not in _SNAPSHOT_DEPENDENT_KINDS:
            raise RederiveConflict(
                "reviewed weekly hold dependency has an unfamiliar kind: "
                + event_id
            )
        snapshot = current.get(usage_ref)
        if ((snapshot or {}).get("payload") or {}).get("kind") \
                != "snapshot_accept":
            # A fresh replay can derive a crossing from the same observation
            # whose weekly axis the review holds. With no selected snapshot to
            # preserve, that crossing has no valid historical dependency and
            # must leave the reviewed target with the held snapshot.
            desired.pop(event_id, None)
            continue
        desired[usage_ref] = snapshot
        for field, expected_kind in _milestone_dependencies(
            payload.get("kind")
        ).items():
            if field == "usage_snapshot_ref":
                continue
            reference = payload.get(field)
            if reference in _EMPTY_REFERENCE_SENTINELS:
                continue
            dependency = current.get(reference)
            if ((dependency or {}).get("payload") or {}).get("kind") \
                    != expected_kind:
                raise RederiveConflict(
                    f"reviewed weekly hold dependency lacks its {field}: "
                    f"{event_id}"
                )
            desired[reference] = dependency

    held_windows = set()
    for snapshot_id in snapshot_ids:
        # #885: only final support may freeze a close. A current snapshot
        # absent from this dependency-closed target is retired by this plan.
        payload = (desired.get(snapshot_id) or {}).get("payload") or {}
        if payload.get("kind") != "snapshot_accept":
            continue
        account_key = payload.get("account_key")
        window_key = payload.get("five_hour_window_key")
        if account_key is not None and window_key is not None:
            held_windows.add((account_key, window_key))
    for event_id, record in current.items():
        payload = record.get("payload") or {}
        if (
            payload.get("kind") != "five_hour_block_close"
            or (payload.get("account_key"), payload.get("five_hour_window_key"))
            not in held_windows
        ):
            continue
        proposed = desired.get(event_id)
        if proposed is None:
            desired[event_id] = record
            continue
        proposed_payload = proposed.get("payload") or {}
        if (
            proposed_payload.get("kind") != "five_hour_block_close"
            or proposed_payload.get("account_key") != payload.get("account_key")
            or proposed_payload.get("five_hour_window_key")
            != payload.get("five_hour_window_key")
        ):
            raise RederiveConflict(
                "reviewed weekly hold block has an unfamiliar shape: "
                + event_id
            )
        merged_payload = dict(payload)
        for field in _BLOCK_WEEKLY_AXIS_FIELDS:
            if field in proposed_payload:
                merged_payload[field] = proposed_payload[field]
        merged = dict(record)
        merged["payload"] = merged_payload
        marker = (historical_close_markers or {}).get(event_id)
        if marker is not None and not _same_child_population(
            payload, proposed_payload,
        ):
            # Hold merging must not hide a failed population proof from the
            # close resolver. Leave the unmarked frozen fact uncertain.
            historical_close_markers.pop(event_id, None)
            marker = None
        if (marker is not None and not payload.get("_pricing")
                and payload.get("pricing_provenance_json") is None):
            # #885: the proof already checked replay's retained population.
            # Compose its marker with the hold's money-preserving weekly-axis
            # correction now, rather than waiting for structure to match on
            # the next invocation. The hold still keeps every monetary byte.
            merged = _with_close_marker(merged, marker)
        desired[event_id] = merged


@dataclasses.dataclass(frozen=True)
class PlanAction:
    disposition: str
    event_id: str
    revision: int
    at: str
    payload: "dict | None"
    payload_hash: str
    kind: str

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
    preserved_event_count: int = 0
    uncertain_cost_fact_count: int = 0
    action_counts_by_event_kind: Mapping[str, Mapping[str, int]] = dataclasses.field(
        default_factory=dict
    )
    #: #797/#823/#840 spec 4.7: the event ids a retraction itself acts on
    #: (`_retraction_scope`). Not part of the plan body: the retractions are
    #: already in the config fingerprint through the decision op's payload.
    retraction_event_ids: frozenset = frozenset()
    #: #956 spec 4.6.3: the windows whose opening fact the selection this plan
    #: was built over tombstones. Not part of the plan body: it is a property
    #: of the journal prefix the plan already names. A filtered plan
    #: (:func:`causal_delta_plan`) evaluates parent presence with it.
    retired_openings: frozenset = frozenset()
    #: #943/#956 spec 4.4: the current five-hour milestones whose window
    #: resolves no block before the plan (pre-existing damage). Reported,
    #: never a reason to refuse, and not part of the plan body.
    orphaned_milestone_ids: frozenset = frozenset()
    #: #884 spec §5.7: the five-hour blocks whose milestone repair the
    #: lineage withheld (:class:`WithheldBlock`). A diagnostic, outside the
    #: plan body; its count reaches ``uncertain_cost_fact_count``.
    withheld_milestone_repairs: tuple = ()

    def _body(self) -> dict:
        body = {
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
            "preservedEventCount": self.preserved_event_count,
            "payloadHashes": sorted(action.payload_hash for action in self.actions),
            "actions": [action.to_dict() for action in self.actions],
        }
        if self.uncertain_cost_fact_count:
            body["uncertainCostFactCount"] = self.uncertain_cost_fact_count
        return body

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
                            preserved_events: Iterable[Mapping],
                            conflicted_event_ids=frozenset(),
                            raw_observations=(),
                            snapshot_identity_decisions=(),
                            reviewed_weekly_hold_ids=(),
                            reviewed_weekly_retraction_ids=(),
                            uncertain_cost_fact_ids=(),
                            historical_close_markers=None,
                            physical_revisions=None,
                            milestone_lineage=None,
                            enforce_guard=True) -> RederivePlan:
    """Diff current effective events against one scratch-derived desired set.

    ``preserved_events`` (#426) names the owned events the re-derivation cannot
    reach — see :func:`preserved_history`. They are held OUT of the diff, so
    the "current but not desired" branch below can never retire history the
    replay was never able to reproduce. The keyword is deliberately required:
    defaulting it to empty is exactly the data-loss bug it exists to prevent.
    A preserved event is only ever acted on to make it durable again —
    re-affirmed at ``rev + 1`` when its group is quarantined (#374), or revived
    at ``rev + 1`` when THIS family's own earlier batch tombstoned it.

    ``conflicted_event_ids`` (#374) names the event ids this family owns whose
    same-revision group the selector QUARANTINED at the winning revision. They
    force a ``supersede`` at ``selected.rev + 1`` **even when the provisional
    winner already equals the desired state** — without that, the equality
    branch below returns ``retain`` and the rev-0 conflict survives in the
    append-only journal forever, which is precisely the case the first design
    called already-correct. The resulting correction is a semantic no-op in
    content and a revision advance in effect, and the revision advance is what
    suppresses the rev-0 group under the selector's revision filter.

    ``historical_close_markers`` (#869) maps an unmarked close's event id to
    the planner's retained-entry proof that its $0 came from a missing card.
    ``uncertain_cost_fact_ids`` names the non-close monetary events the caller
    already judged unprovable; closed blocks are judged here, against the
    state this plan leaves behind.

    #875: the milestones of a block whose final close is frozen (kept,
    preserved history retained or revived, or corrected without moving its
    money) or replaced (added, or corrected in a way that writes its money)
    are decided by :func:`_resolve_closed_block_milestones` before the diff,
    the same way closes are decided before milestones.

    #884: ``milestone_lineage`` (:func:`build_five_hour_lineage`, built from
    the same selection's fold) lets an unchanged crossing of a frozen block
    regain the amount its history proves an earlier rederive repriced, and
    fails closed otherwise: each reported withheld block counts once in
    ``uncertain_cost_fact_count`` and is listed in
    ``withheld_milestone_repairs``. ``None`` reproduces #875's plan exactly;
    a missing lineage is a compatibility default, never evidence."""
    conflicted_event_ids = frozenset(conflicted_event_ids or ())
    historical_close_markers = dict(historical_close_markers or {})
    # #825/#832 fix round A (Codex fix-round-1 Q2): the highest revision of
    # every physical record admission hides (an excluded legacy event or
    # correction action). A new revision is allocated above it, so the
    # previous release never reads a same-revision conflict.
    physical_revisions = dict(physical_revisions or {})
    desired = _desired_by_id(desired_events)
    current = {
        event_id: selected.record
        for event_id, selected in selection.by_id.items()
        if selected.status == "active"
        and selected.record is not None
        and _is_owned_event(selected.record)
    }
    # #1004 D4: any sequence; a coordinate-backed one is never listed.
    if not isinstance(raw_observations, Sequence):
        raw_observations = tuple(raw_observations)
    _reconcile_snapshot_identities(
        current, desired, selection, raw_observations,
        snapshot_identity_decisions,
    )
    # #797/#823/#840 spec 4.7 (D4): `reviewed_weekly_hold_ids` are the
    # ordinary holds; a retraction is excluded from preservation, and the
    # resets it leaves unsupported are never restored by a held milestone.
    retracted_snapshot_ids = frozenset(
        f"sa:{raw_id}" for raw_id in reviewed_weekly_retraction_ids or ()
        if isinstance(raw_id, str) and raw_id)
    unsupported_resets = _retraction_unsupported_resets(
        current, desired, retracted_snapshot_ids,
        raw_observations=raw_observations)
    retraction_event_ids = _retraction_scope(
        current, retracted_snapshot_ids, unsupported_resets)
    _preserve_reviewed_hold_dependencies(
        current, desired, reviewed_weekly_hold_ids, historical_close_markers,
        unsupported_resets=unsupported_resets,
    )
    # #943 spec 4.4: an `fhcb` follows its `fhc` by natural key.
    _companions_follow_kept_credits(current, desired)
    retained_event_count = sum(
        1 for selected in selection.by_id.values()
        if selected.status == "active"
        and selected.record is not None
        and not _is_owned_event(selected.record)
    )
    preserved = {}
    for record in preserved_events:
        event_id = record.get("id")
        if isinstance(event_id, str) and event_id and event_id not in desired:
            preserved[event_id] = record
    counts = {"retain": 0, "supersede": 0, "tombstone": 0, "add": 0}
    counts_by_kind = {
        kind: dict.fromkeys(counts, 0)
        for kind in sorted(_EVT_CLASSIFICATIONS)
    }
    actions: list[PlanAction] = []
    drifted_close_ids: set = set()

    def _kind(record):
        return (record.get("payload") or {}).get("kind")

    # Every close is decided before any milestone, so a five-hour milestone can
    # follow the decision its block's close actually received (#869).
    close_decisions = {}
    for event_id in sorted(set(current) & set(desired)):
        if (
            _kind(current[event_id]) == "five_hour_block_close"
            and _kind(desired[event_id]) == "five_hour_block_close"
        ):
            close_decisions[event_id] = _preserve_non_derivable_state(
                current[event_id], desired[event_id],
                historical_marker=historical_close_markers.get(event_id),
                drifted_ids=drifted_close_ids,
            )
    # #875 (spec §4.1): classify every block by the closed close the plan's
    # FINAL state holds for it, whatever its pricing basis. The class depends
    # on that close's MONEY, the block total its milestones must agree with,
    # never on whether the plan acts on it: kept and preserved are transient
    # roles (a hold can inject a preserved close, and the next plan can see
    # it preserved again), so both are frozen, a revival is frozen although
    # the plan writes it, and so is a correction that leaves the total where
    # it was. A frozen close freezes the block cost of its unchanged
    # crossings; a corrected crossing under it still takes replay's.
    frozen_close_blocks = set()
    replaced_close_blocks = set()
    for event_id, decision in close_decisions.items():
        payload = decision.get("payload") or {}
        if payload.get("is_closed") != 1:
            continue
        if decision is current[event_id] or _close_keeps_its_money(
            current[event_id].get("payload") or {}, payload,
        ):
            # Kept, including a forced-conflict reaffirmation, which only
            # re-writes this same content at a higher revision; or a
            # money-preserving correction, such as the reviewed hold's
            # weekly-axis update (spec §1.7).
            frozen_close_blocks.add(_five_hour_block(payload))
        else:
            # The plan writes this close's money: it closes a block that was
            # open, or it moves or cannot compare the total.
            replaced_close_blocks.add(_five_hour_block(payload))
    for event_id in set(desired) - set(current):
        payload = desired[event_id].get("payload") or {}
        if (payload.get("kind") == "five_hour_block_close"
                and payload.get("is_closed") == 1):
            replaced_close_blocks.add(_five_hour_block(payload))  # an add
    for event_id, record in preserved.items():
        if event_id in current:
            final_close = current[event_id]
        elif _revived_by_this_family(selection.by_id.get(event_id)):
            final_close = record
        else:
            continue  # stays retired: the final state holds no such close
        payload = final_close.get("payload") or {}
        if (payload.get("kind") == "five_hour_block_close"
                and payload.get("is_closed") == 1):
            frozen_close_blocks.add(_five_hour_block(payload))
    # Frozen wins (spec §4.1): a block whose final state holds a frozen close
    # and a replaced one is frozen. The causal block closure applies the same
    # rule (:func:`_replaced_block_closure`).
    replaced_close_blocks -= frozen_close_blocks
    withheld: dict = {}
    milestone_decisions = _resolve_closed_block_milestones(
        current, desired, preserved, frozen_close_blocks,
        replaced_close_blocks, selection=selection,
        lineage=milestone_lineage, withheld=withheld,
    )

    for event_id in sorted(set(current) | set(desired) | set(preserved)):
        current_record = current.get(event_id)
        desired_record = milestone_decisions.get(
            event_id, desired.get(event_id))
        selected = selection.by_id.get(event_id)
        preserved_record = preserved.get(event_id)
        if preserved_record is not None:
            # Un-re-derivable history: retain it, or restore it when this
            # family's own earlier plan retired it (#426).
            revive = _revived_by_this_family(selected)
            reaffirm = (
                selected is not None
                and selected.status == "active"
                and event_id in conflicted_event_ids
            )
            if not (revive or reaffirm):
                counts["retain"] += 1
                counts_by_kind[(preserved_record.get("payload") or {})["kind"]][
                    "retain"
                ] += 1
                continue
            source = (
                selected.record if reaffirm else preserved_record
            )
            revision = int(selected.rev) + 1
            disposition = "supersede"
            at = str(source["at"])
            payload = dict(source.get("payload") or {})
        elif current_record is not None and desired_record is not None:
            if event_id in close_decisions:
                desired_record = close_decisions[event_id]
            elif event_id not in milestone_decisions:
                desired_record = _preserve_non_derivable_state(
                    current_record, desired_record,
                )
            if (
                _semantic_event(current_record) == _semantic_event(desired_record)
                and event_id not in conflicted_event_ids
            ):
                counts["retain"] += 1
                counts_by_kind[(desired_record.get("payload") or {})["kind"]][
                    "retain"
                ] += 1
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
        floor = physical_revisions.get(event_id)
        if floor is not None and revision <= floor:
            revision = floor + 1
        counts[disposition] += 1
        kind_record = (
            current_record if disposition == "tombstone"
            else desired_record if preserved_record is None
            else preserved_record
        )
        kind = (kind_record.get("payload") or {})["kind"]
        counts_by_kind[kind][disposition] += 1
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
            kind=kind,
        ))

    # #943/#956 spec 4.4: parent validation over the resulting effective
    # graph. Every five-hour milestone that resolves a block before the plan
    # must still resolve one after it. The diff above already tombstones a
    # current milestone the desired replay lacks, so a survivor without its
    # parent is one the replay itself keeps (or preserved history): refuse
    # rather than leave it parentless.
    retired_openings = retired_openings_of(selection)
    orphaned_before, lost = five_hour_parent_report(
        current, actions, retired_openings)
    if lost:
        raise RederiveConflict(
            "a five-hour block removal would orphan a retained milestone: "
            + ", ".join(lost[:10]))
    # #869 F7/Q9: a close counts only while its cost stays unprovable in the
    # state this plan leaves behind — not a provably priced close, not one this
    # plan corrects, and not one an earlier correction already proved.
    uncertain_closes = {
        event_id for event_id, record in current.items()
        if _kind(record) == "five_hour_block_close"
        and close_cost_is_uncertain(record.get("payload") or {})
    }
    for action in actions:
        if action.kind != "five_hour_block_close":
            continue
        if action.disposition == "tombstone":
            uncertain_closes.discard(action.event_id)
        elif close_cost_is_uncertain(action.payload or {}):
            uncertain_closes.add(action.event_id)
        else:
            uncertain_closes.discard(action.event_id)
    uncertain_ids = (
        set(uncertain_cost_fact_ids or ()) | uncertain_closes
        | drifted_close_ids
    )
    # #884 spec §5.7: a withheld block counts once, by its smallest close id,
    # and never beside a close of its own that already counts.
    withheld_blocks = _withheld_blocks(withheld, current, actions, selection)
    for block in withheld_blocks:
        if any(close_id in uncertain_ids for close_id in block.close_event_ids):
            continue
        # The synthetic key is defensive only: a frozen block always has a
        # close in the plan's final state, so it is unreachable.
        uncertain_ids.add(
            min(block.close_event_ids) if block.close_event_ids
            else ("withheld-block", block.account_key,
                  block.five_hour_window_key))
    plan = RederivePlan(
        family=FAMILY,
        journal_high_water=journal_high_water,
        cache_fingerprint=cache_fingerprint,
        config_fingerprint=config_fingerprint,
        counts=counts,
        actions=tuple(actions),
        retained_event_count=retained_event_count,
        preserved_event_count=len(preserved),
        uncertain_cost_fact_count=len(uncertain_ids),
        action_counts_by_event_kind=counts_by_kind,
        retraction_event_ids=retraction_event_ids,
        retired_openings=retired_openings,
        orphaned_milestone_ids=orphaned_before,
        withheld_milestone_repairs=withheld_blocks,
    )
    if enforce_guard:
        enforce_week_reset_add_burst_guard(plan)
    return plan


def _replaced_block_closure(
    included: list, reviewed_actions, current_events: Mapping[str, Mapping],
) -> list:
    """#875 (spec §4.5): keep a five-hour block whose close R replaces whole.

    The B/R filter drops a close correction both plans make identically, yet
    a milestone under that close can still differ between them, for instance
    when R's hold keeps its predecessor whole while B reprices it. Keeping
    only the milestone would reprice it under a close the subset leaves as it
    stands (spec §1.5). So whenever ``included`` holds a five-hour milestone
    action whose block has a reviewed close action that REPLACES the close,
    the subset takes that close action and every reviewed action on the
    block's milestones, whether or not they survived the filter.

    The classification is §4.1's, derived from R's actions and
    ``current_events`` alone. A close action replaces when it adds a closed
    close, or supersedes to a closed record whose current record is open,
    whose totals are unusable, or whose totals differ by more than 1e-9 USD.
    A block R holds FROZEN never triggers, even beside such an action
    (frozen wins): a closed current close R takes no action on (kept, or
    preserved history retained), a revival (a supersede with no active
    current record), a forced-conflict reaffirmation (a supersede to the
    current content, whatever its total), or a money-preserving correction.
    A tombstone carries no payload; its block is read from its current
    record. Only reviewed actions are ever added, each once. The reverse
    direction also applies (#883): an included replacing close brings its
    milestone actions, even when B and R make those actions identically.
    """
    def block(action):
        if action.disposition == "tombstone":
            record = current_events.get(action.event_id) or {}
            return _five_hour_block(record.get("payload") or {})
        return _five_hour_block(action.payload or {})

    acted_ids = {action.event_id for action in reviewed_actions}
    frozen_blocks = set()
    for event_id, record in current_events.items():
        payload = record.get("payload") or {}
        if (
            event_id not in acted_ids
            and payload.get("kind") == "five_hour_block_close"
            and payload.get("is_closed") == 1
        ):
            frozen_blocks.add(_five_hour_block(payload))
    replacing: dict[tuple, set] = {}
    for action in reviewed_actions:
        payload = action.payload or {}
        if (
            action.kind != "five_hour_block_close"
            or action.disposition not in {"add", "supersede"}
            or payload.get("is_closed") != 1
        ):
            continue
        if action.disposition == "supersede":
            now = current_events.get(action.event_id)
            if (
                now is None  # a revival of preserved history
                # A reaffirmation re-writes the kept content, whatever its
                # total, exactly as the resolver's ``decision is current``.
                or _semantic_event(now) == _semantic_event({
                    "id": action.event_id, "at": action.at,
                    "payload": payload,
                })
                or _close_keeps_its_money(now.get("payload") or {}, payload)
            ):
                frozen_blocks.add(block(action))
                continue
        replacing.setdefault(block(action), set()).add(action.event_id)
    for key in frozen_blocks:
        replacing.pop(key, None)
    triggered = {
        block(action) for action in included
        if action.kind in {"five_hour_milestone", "five_hour_block_close"}
        and block(action) in replacing
    }
    if not triggered:
        return included
    close_ids = set().union(*(replacing[key] for key in triggered))
    expanded = list(included)
    included_ids = {action.event_id for action in included}
    for action in reviewed_actions:
        if action.event_id in included_ids:
            continue
        if (
            action.event_id in close_ids
            or (action.kind == "five_hour_milestone"
                and block(action) in triggered)
        ):
            expanded.append(action)
            included_ids.add(action.event_id)
    return expanded


def _monetary_chain_closure(
    included: list, reviewed_actions, current_events: Mapping[str, Mapping],
) -> list:
    """#883: retain the reviewed final predecessor basis of causal marginals.

    An action can change a row's occupied threshold, segment or block cost.
    The nearest-lower threshold group is then a dependency of every present
    marginal above it, in both the current and reviewed final populations.
    Filtering can otherwise leave behind a retired or moved successor with
    its old marginal. Changed rows entering or leaving either interval are
    dependencies too: keeping one would select the wrong predecessor.
    Include dependencies in both directions, even when a
    successor needs no action itself. A marginal-only correction therefore
    brings a changed predecessor, but cannot propagate through its unchanged
    block cost to unrelated siblings. Null marginals have no monetary edge.

    Only reviewed actions are added. Frozen crossing costs have already been
    settled by the full planner, so closing these edges never reprices them.
    """
    final = dict(current_events)
    for action in reviewed_actions:
        if action.disposition == "tombstone":
            final.pop(action.event_id, None)
        else:
            final[action.event_id] = {"payload": action.payload or {}}

    def monetary_position(record):
        payload = (record or {}).get("payload") or {}
        if payload.get("kind") != "five_hour_milestone":
            return None
        threshold = _usable_threshold(payload.get("percent_threshold"))
        if threshold is None:
            return None
        return (_milestone_segment_key(payload), threshold,
                payload.get("block_cost_usd"))

    changed = {
        action.event_id for action in reviewed_actions
        if action.kind == "five_hour_milestone"
        and monetary_position(current_events.get(action.event_id))
        != monetary_position(final.get(action.event_id))
    }
    if not changed:
        return included
    def occupied_thresholds(events, event_ids):
        occupied: dict[tuple, dict[int, set[str]]] = {}
        for event_id in event_ids:
            position = monetary_position(events.get(event_id))
            if position is not None:
                segment, threshold, _cost = position
                occupied.setdefault(segment, {}).setdefault(
                    threshold, set()).add(event_id)
        return occupied

    edges: dict[str, set[str]] = {}
    for population, other in ((final, current_events), (current_events, final)):
        occupied = occupied_thresholds(population, population)
        changed_other = occupied_thresholds(other, changed)
        for event_id, record in population.items():
            payload = record.get("payload") or {}
            position = monetary_position(record)
            if (position is None or payload.get("marginal_cost_usd") is None
                    or not _usable_amount(payload.get("block_cost_usd"))):
                continue
            segment, threshold, _cost = position
            lower = [value for value in occupied[segment] if value < threshold]
            nearest = max(lower) if lower else None
            dependencies = (
                occupied[segment][nearest] & changed if nearest is not None
                else set()
            )
            for prior_threshold, prior_ids in changed_other.get(segment, {}).items():
                if (prior_threshold < threshold
                        and (nearest is None or prior_threshold >= nearest)):
                    dependencies.update(prior_ids)
            if dependencies:
                edges.setdefault(event_id, set()).update(dependencies)
                for dependency_id in dependencies:
                    edges.setdefault(dependency_id, set()).add(event_id)
    # Retained rows participate as dependency nodes too, although there is no
    # reviewed action to emit for them. Visit each monetary edge once rather
    # than rescanning the full graph for every later threshold in a chain.
    required_ids = {action.event_id for action in included}
    pending = list(required_ids)
    while pending:
        event_id = pending.pop()
        added = edges.get(event_id, set()) - required_ids
        required_ids.update(added)
        pending.extend(added)
    return [action for action in reviewed_actions
            if action.event_id in required_ids]


def _causal_snapshot_closure(actions, reviewed_by_id, current_events,
                             protected=frozenset()):
    """Close explicit references, keeping a selected dependency if needed.

    #797/#823/#840 spec 4.7 (D4): a tombstone in ``protected`` (a retraction's
    own scope) is never cancelled to keep a milestone that still names it —
    the retraction wins, so such a milestone is a conflict here (the planner
    never pins one, `_preserve_reviewed_hold_dependencies`)."""
    action_by_id = {action.event_id: action for action in actions}
    dependency_tombstones = set()
    for action in actions:
        if (action.disposition == "tombstone"
                or action.kind not in _SNAPSHOT_DEPENDENT_KINDS):
            continue
        for field, expected_kind in _milestone_dependencies(action.kind).items():
            reference = (action.payload or {}).get(field)
            if reference in _EMPTY_REFERENCE_SENTINELS:
                continue
            dependency_action = action_by_id.get(reference)
            if (dependency_action is not None
                    and dependency_action.disposition == "tombstone"):
                if reference in protected:
                    raise RederiveConflict(
                        "a retracted dependency's tombstone would be cancelled "
                        "by milestone: " + action.event_id)
                current = current_events.get(reference)
                if ((current or {}).get("payload") or {}).get("kind") \
                        != expected_kind:
                    raise RederiveConflict(
                        "causal milestone dependency cannot be retained: "
                        + action.event_id)
                dependency_tombstones.add(reference)
    actions = [action for action in actions
               if action.event_id not in dependency_tombstones]
    target_events = dict(current_events)
    for action in actions:
        if action.disposition == "tombstone":
            target_events.pop(action.event_id, None)
        else:
            target_events[action.event_id] = {"payload": action.payload or {}}
    # Appending to this list intentionally visits newly required dependencies.
    for action in actions:
        if (action.disposition == "tombstone"
                or action.kind not in _SNAPSHOT_DEPENDENT_KINDS):
            continue
        for field, expected_kind in _milestone_dependencies(action.kind).items():
            reference = (action.payload or {}).get(field)
            if reference in _EMPTY_REFERENCE_SENTINELS:
                continue
            target = target_events.get(reference)
            if ((target or {}).get("payload") or {}).get("kind") == expected_kind:
                continue
            dependency = reviewed_by_id.get(reference)
            if (dependency is None or dependency.disposition == "tombstone"
                    or dependency.kind != expected_kind
                    or (dependency.payload or {}).get("kind") != expected_kind):
                raise RederiveConflict(
                    "causal milestone dependency is missing: " + action.event_id)
            actions.append(dependency)
            target_events[reference] = {"payload": dependency.payload or {}}
    return actions


def _retained_reference_closure(actions, reviewed_by_id, current_events):
    """#797/#823/#840 spec 4.7: unchanged retained references are validated
    as well as proposed actions. A current milestone the delta does not act
    on but which names an event the delta tombstones takes its reviewed
    action; with none, the plan would leave it dangling and refuses."""
    acted = {action.event_id for action in actions}
    tombstoned = {action.event_id for action in actions
                  if action.disposition == "tombstone"}
    if not tombstoned:
        return actions
    for event_id, record in sorted(current_events.items()):
        payload = (record or {}).get("payload") or {}
        kind = payload.get("kind")
        if event_id in acted or kind not in _SNAPSHOT_DEPENDENT_KINDS:
            continue
        if not any(payload.get(field) in tombstoned
                   for field in _milestone_dependencies(kind)):
            continue
        reviewed = reviewed_by_id.get(event_id)
        if reviewed is None:
            raise RederiveConflict(
                "a retained milestone would name a tombstoned event: "
                + event_id)
        actions.append(reviewed)
        acted.add(event_id)
    return actions


def _five_hour_parent_closure(included: list, reviewed_actions,
                              current_events: Mapping[str, Mapping],
                              retired_openings=frozenset()) -> list:
    """#943/#956 spec 4.4: the reviewed actions an included action needs.

    * Derived-FK parent closure: an included five-hour milestone add or
      supersede resolves its `block_id` from its window at fold, so when the
      graph the included actions leave behind materializes no block for
      that window, it keeps the reviewed plan's parent-materializing action
      for the window (an opening or close add or supersede), even when that
      action is common to the baseline. A window whose block the resulting
      graph already materializes (a current close or opening the plan keeps)
      needs no action: pulling a baseline-common close correction in there
      would rewrite a close the #875 frozen-block rule keeps as it stands.
    * A credit and its boundary companion travel together: an included
      action on either brings the reviewed action on the other.
    * A seed never outlives a removed block: an included close tombstone
      brings the reviewed opening tombstone of its window (the reviewed plan
      tombstones the opening only when its replay has no block there).

    Only reviewed actions are ever added, each once."""
    parents: dict = {}
    opening_tombstones: dict = {}
    pairs: dict = {}
    for action in reviewed_actions:
        payload = _action_payload(action, current_events)
        if action.kind in _BLOCK_PARENT_KINDS:
            window = _window_of(payload)
            if action.disposition in {"add", "supersede"}:
                parents.setdefault(window, []).append(action)
            elif action.kind == "five_hour_block_open":
                opening_tombstones.setdefault(window, []).append(action)
        elif action.kind in _CREDIT_PAIR_KINDS:
            pairs.setdefault(_credit_pair_key(payload), []).append(action)
    expanded = list(included)
    included_ids = {action.event_id for action in included}
    present = None
    for action in included:
        payload = _action_payload(action, current_events)
        needed = []
        if (action.kind == "five_hour_milestone"
                and action.disposition != "tombstone"):
            if present is None:
                after_events, after_retired = _post_plan_state(
                    current_events, included, retired_openings)
                present = five_hour_parent_presence(
                    after_events, after_retired)
            if _window_of(payload) not in present:
                needed = parents.get(_window_of(payload), [])
        elif action.kind in _CREDIT_PAIR_KINDS:
            needed = pairs.get(_credit_pair_key(payload), [])
        elif (action.kind == "five_hour_block_close"
              and action.disposition == "tombstone"):
            needed = opening_tombstones.get(_window_of(payload), [])
        for dependency in needed:
            if dependency.event_id not in included_ids:
                expanded.append(dependency)
                included_ids.add(dependency.event_id)
    return expanded


def _parent_validation_completion(included: list, reviewed_by_id,
                                  current_events: Mapping[str, Mapping],
                                  retired_openings) -> list:
    """#943/#956 spec 4.4: a filtered plan whose block removal would orphan
    an unchanged, retained milestone takes that milestone's reviewed
    tombstone (the desired replay lacks it). A survivor with no reviewed
    tombstone is left for the final validation, which refuses."""
    _orphaned_before, lost = five_hour_parent_report(
        current_events, included, retired_openings)
    if not lost:
        return included
    expanded = list(included)
    included_ids = {action.event_id for action in included}
    for event_id in lost:
        reviewed = reviewed_by_id.get(event_id)
        if (reviewed is not None and reviewed.disposition == "tombstone"
                and event_id not in included_ids):
            expanded.append(reviewed)
            included_ids.add(event_id)
    return expanded


def causal_delta_plan(
    baseline: RederivePlan, reviewed: RederivePlan, *,
    current_events: Mapping[str, Mapping],
    authorized_week_reset_origins=frozenset(),
) -> RederivePlan:
    """Return only C->R actions whose B and R target states differ.

    Both inputs are diffs from the same effective graph C. Therefore an action
    with the same correction shape in both plans is baseline drift, while a
    reviewed-only or changed action is caused by the proposed operator record.
    A baseline-only action means the reviewed target returns to C and requires
    no correction action.

    #875 (spec §4.5): a five-hour block whose close R replaces, by §4.1's
    money rule, is applied whole whenever the subset changes one of its
    milestones, and a block R holds frozen never is; see
    :func:`_replaced_block_closure`. Monetary predecessor/successor edges and
    snapshot references are closed with it until the final graph stabilizes.
    """
    if (
        baseline.journal_high_water != reviewed.journal_high_water
        or baseline.cache_fingerprint != reviewed.cache_fingerprint
    ):
        raise RederiveConflict(
            "baseline and reviewed plans do not share stable inputs"
        )
    baseline_by_id = {
        action.event_id: action.to_correction_action()
        for action in baseline.actions
    }
    reviewed_by_id = {
        action.event_id: action for action in reviewed.actions
    }
    authorized_week_reset_origins = frozenset(
        authorized_week_reset_origins or ())
    retraction_scope = frozenset(reviewed.retraction_event_ids or ())
    actions = [
        action for action in reviewed.actions
        if (
            baseline_by_id.get(action.event_id)
            != action.to_correction_action()
            or (
                action.kind == "week_reset"
                and action.disposition != "tombstone"
                and (action.payload or {}).get("origin_observation_id")
                in authorized_week_reset_origins
            )
            # #797/#823/#840 spec 4.7: an explicit retraction survives the
            # baseline/reviewed intersection.
            or action.event_id in retraction_scope
        )
    ]
    # Close block, monetary and explicit-reference edges to a fixed point.
    # A dependency introduced by one phase must be seen by every other phase.
    while True:
        previous_ids = {action.event_id for action in actions}
        actions = _replaced_block_closure(
            actions, reviewed.actions, current_events)
        actions = _monetary_chain_closure(
            actions, reviewed.actions, current_events)
        actions = _causal_snapshot_closure(
            actions, reviewed_by_id, current_events,
            protected=retraction_scope)
        if retraction_scope:
            actions = _retained_reference_closure(
                actions, reviewed_by_id, current_events)
        # #943/#956 spec 4.4: over the final filtered set, so a parent or a
        # companion the B/R filter dropped as baseline-common returns.
        actions = _five_hour_parent_closure(
            actions, reviewed.actions, current_events,
            reviewed.retired_openings)
        actions = _parent_validation_completion(
            actions, reviewed_by_id, current_events,
            reviewed.retired_openings)
        if {action.event_id for action in actions} == previous_ids:
            break
    # Parent validation over the resulting effective graph (spec 4.4).
    orphaned_before, lost = five_hour_parent_report(
        current_events, actions, reviewed.retired_openings)
    if lost:
        raise RederiveConflict(
            "a five-hour block removal would orphan a retained milestone: "
            + ", ".join(lost[:10]))
    included_ids = {action.event_id for action in actions}
    actions = tuple(
        action for action in reviewed.actions
        if action.event_id in included_ids
    )
    removed = [action for action in reviewed.actions if action not in actions]
    counts = dict(reviewed.counts)
    counts_by_kind = {
        kind: dict(values)
        for kind, values in reviewed.action_counts_by_event_kind.items()
    }
    for action in removed:
        counts[action.disposition] -= 1
        counts_by_kind[action.kind][action.disposition] -= 1
        if action.disposition != "add":
            counts["retain"] += 1
            counts_by_kind[action.kind]["retain"] += 1
    return RederivePlan(
        family=reviewed.family,
        journal_high_water=reviewed.journal_high_water,
        cache_fingerprint=reviewed.cache_fingerprint,
        config_fingerprint=reviewed.config_fingerprint,
        counts=counts,
        actions=actions,
        retained_event_count=reviewed.retained_event_count,
        preserved_event_count=reviewed.preserved_event_count,
        uncertain_cost_fact_count=reviewed.uncertain_cost_fact_count,
        action_counts_by_event_kind=counts_by_kind,
        retraction_event_ids=reviewed.retraction_event_ids,
        retired_openings=reviewed.retired_openings,
        orphaned_milestone_ids=orphaned_before,
        withheld_milestone_repairs=reviewed.withheld_milestone_repairs,
    )


def week_reset_add_burst_guard(plan: RederivePlan):
    """Return the unconditional reset-add guard finding for one plan."""
    # The guard checks the completed deterministic action set, not candidate
    # observations or retained resets. A second reset of one physical window
    # remains admissible; three additions for the same exact evidence do not.
    grouped: dict[tuple, int] = {}
    for action in plan.actions:
        payload = action.payload or {}
        if action.disposition != "add" or payload.get("kind") != "week_reset":
            continue
        key = (
            payload.get("account_key"),
            payload.get("new_week_end_at"),
            payload.get("observed_pre_credit_pct"),
        )
        if any(value is None for value in key):
            continue
        grouped[key] = grouped.get(key, 0) + 1
    violations = [
        {
            "accountKey": account_key,
            "newWeekEndAt": week_end,
            "observedPreCreditPct": percent,
            "addCount": count,
        }
        for (account_key, week_end, percent), count in grouped.items()
        if count > 2
    ]
    violations.sort(key=lambda item: (
        str(item["accountKey"]), str(item["newWeekEndAt"]),
        repr(item["observedPreCreditPct"]),
    ))
    if violations:
        return {
            "code": "week-reset-add-burst",
            "limit": 2,
            "violations": violations,
        }
    return None


def enforce_week_reset_add_burst_guard(plan: RederivePlan) -> None:
    finding = week_reset_add_burst_guard(plan)
    if finding is not None:
        raise RederivePlanGuardConflict(plan, finding)
