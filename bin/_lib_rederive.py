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
import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping


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
            facts = {
                key: value for key, value in payload.items()
                if not key.startswith("total_")
                and key not in {"_models", "_projects"}
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
            return current_record
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


_SNAPSHOT_IDENTITY_FIELDS = (
    "account_key", "week_start_date", "week_start_at", "week_end_at",
    "weekly_percent", "five_hour_percent", "five_hour_window_key",
    "weekly_observation_held", "source",
)
_SNAPSHOT_DEPENDENT_KINDS = frozenset({
    "percent_milestone", "five_hour_milestone",
})
_EMPTY_REFERENCE_SENTINELS = frozenset({None, 0, "0", ""})
_MILESTONE_BASE_DEPENDENCIES = {
    "usage_snapshot_ref": "snapshot_accept",
    "cost_snapshot_ref": "weekly_cost_snapshot",
}


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
    if not isinstance(raw_observations, (list, tuple)):
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
            raw_positions[raw_id] = (index, record)
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
        old_index, old_raw = raw_positions[old_id[3:]]
        candidates[old_id] = []
        for new_id, new in unmatched_desired.items():
            new_index, new_raw = raw_positions[new_id[3:]]
            if (
                new_index < old_index
                and _same_acceptance_transition(
                    old, new, old_raw, new_raw,
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


def _preserve_reviewed_hold_dependencies(
    current: Mapping[str, Mapping], desired: dict[str, Mapping],
    reviewed_weekly_hold_ids=(),
) -> None:
    """Keep frozen five-hour facts reached from exact reviewed holds.

    An exact weekly-axis hold must not erase the same observation's retained
    five-hour crossing. Milestones are historical facts, so retain their whole
    accepted payload and close the usage/cost reference graph with the exact
    currently selected events. A closed block may update only its weekly axes;
    its five-hour close boundary, reading, money, tokens and children stay the
    selected frozen fact. Any unfamiliar reference shape refuses rather than
    being guessed or rewritten.
    """
    snapshot_ids = {
        f"sa:{raw_id}" for raw_id in reviewed_weekly_hold_ids
        if isinstance(raw_id, str) and raw_id
    }
    if not snapshot_ids:
        return
    suppressed_snapshot_ids = set()
    for record in (*current.values(), *desired.values()):
        suppression = (record.get("payload") or {}).get("suppression")
        if suppression is None:
            continue
        if not isinstance(suppression, list) or any(
            not isinstance(item, str) for item in suppression
        ):
            raise RederiveConflict(
                "reviewed weekly hold suppression has an unfamiliar shape: "
                + str(record.get("id"))
            )
        suppressed_snapshot_ids.update(snapshot_ids & set(suppression))
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
            if _contains_any_value(payload, snapshot_ids):
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
            if _contains_any_value(payload, snapshot_ids):
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
        payload = (current.get(snapshot_id) or {}).get("payload") or {}
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
    action_counts_by_event_kind: Mapping[str, Mapping[str, int]] = dataclasses.field(
        default_factory=dict
    )

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
            "preservedEventCount": self.preserved_event_count,
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
                            preserved_events: Iterable[Mapping],
                            conflicted_event_ids=frozenset(),
                            raw_observations=(),
                            snapshot_identity_decisions=(),
                            reviewed_weekly_hold_ids=(),
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
    _reconcile_snapshot_identities(
        current, desired, selection, raw_observations,
        snapshot_identity_decisions,
    )
    _preserve_reviewed_hold_dependencies(
        current, desired, reviewed_weekly_hold_ids,
    )
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

    for event_id in sorted(set(current) | set(desired) | set(preserved)):
        current_record = current.get(event_id)
        desired_record = desired.get(event_id)
        selected = selection.by_id.get(event_id)
        preserved_record = preserved.get(event_id)
        if preserved_record is not None:
            # Un-re-derivable history: retain it, or restore it when this
            # family's own earlier plan retired it (#426).
            revive = (
                selected is not None
                and selected.status == "tombstone"
                and str(selected.batch_id or "").startswith(_FAMILY_BATCH_PREFIX)
            )
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
            desired_record = _preserve_non_derivable_state(
                current_record, desired_record
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

    plan = RederivePlan(
        family=FAMILY,
        journal_high_water=journal_high_water,
        cache_fingerprint=cache_fingerprint,
        config_fingerprint=config_fingerprint,
        counts=counts,
        actions=tuple(actions),
        retained_event_count=retained_event_count,
        preserved_event_count=len(preserved),
        action_counts_by_event_kind=counts_by_kind,
    )
    if enforce_guard:
        enforce_week_reset_add_burst_guard(plan)
    return plan


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
        )
    ]
    action_by_id = {action.event_id: action for action in actions}
    dependency_tombstones = set()
    for action in actions:
        if (action.disposition == "tombstone"
                or action.kind not in _SNAPSHOT_DEPENDENT_KINDS):
            continue
        payload = action.payload or {}
        for field, expected_kind in _milestone_dependencies(
            action.kind
        ).items():
            reference = payload.get(field)
            if reference in _EMPTY_REFERENCE_SENTINELS:
                continue
            dependency_action = action_by_id.get(reference)
            if (dependency_action is not None
                    and dependency_action.disposition == "tombstone"):
                current = current_events.get(reference)
                if ((current or {}).get("payload") or {}).get("kind") \
                        != expected_kind:
                    raise RederiveConflict(
                        "causal milestone dependency cannot be retained: "
                        + action.event_id
                    )
                dependency_tombstones.add(reference)
    if dependency_tombstones:
        actions = [
            action for action in actions
            if action.event_id not in dependency_tombstones
        ]

    target_events = dict(current_events)
    for action in actions:
        if action.disposition == "tombstone":
            target_events.pop(action.event_id, None)
        else:
            target_events[action.event_id] = {"payload": action.payload or {}}
    for action in actions:
        if (action.disposition == "tombstone"
                or action.kind not in _SNAPSHOT_DEPENDENT_KINDS):
            continue
        payload = action.payload or {}
        for field, expected_kind in _milestone_dependencies(
            action.kind
        ).items():
            reference = payload.get(field)
            if reference in _EMPTY_REFERENCE_SENTINELS:
                continue
            target = target_events.get(reference)
            if ((target or {}).get("payload") or {}).get("kind") \
                    != expected_kind:
                dependency = reviewed_by_id.get(reference)
                if (
                    dependency is None
                    or dependency.disposition == "tombstone"
                    or dependency.kind != expected_kind
                    or (dependency.payload or {}).get("kind") != expected_kind
                ):
                    raise RederiveConflict(
                        "causal milestone dependency is missing: "
                        + action.event_id
                    )
                actions.append(dependency)
                target_events[reference] = {
                    "payload": dependency.payload or {},
                }
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
        action_counts_by_event_kind=counts_by_kind,
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
