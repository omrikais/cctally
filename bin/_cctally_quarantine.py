"""Outcome quarantine glue (#825/#832 spec 5.6; #948): the journal-order fold
over records, its persistence in the disposable stats index, the writers'
release protocol and the read side `doctor` and the statusline reducer
consume.

Every consumer folds the SAME records in the SAME canonical journal order:

* live ingest (`_cctally_journal._run_cycle`) over each cycle's decoded batch,
  excluded records included, with the pipeline hooks of a record running only
  after the fold has advanced through it;
* `db rebuild --db stats` over the whole pinned prefix, inline in its read
  pass;
* the `db rederive` scratch replay, over the planner's record list, whose
  excluded slots carry the original record for exactly this purpose;
* the derivation-completion scratch replay, from the predecessor checkpoint.

So triggers, episodes, bounds, coverage, witnesses and releases are a
function of the journal alone (#948 spec 6.11).

What each record contributes:

* an EXCLUDED record — any type, any family — is a trigger at its coordinate
  (a new generation); an excluded Claude reading, event or credit also widens
  the "excluded" bounds, which apply to every account (spec 6.10: the
  excluded record is its own cohort, with no 95 s wait);
* a current `legacy_spool_activity` op is a trigger. Its `inventory-incomplete`
  reason, and a cutover or revision op recording an inventory failure without
  `inventoryEpisode`, open an incomplete-inventory episode; a revision op
  naming the open episode continues it. Any other reason, a
  `legacy_spool_inventory_completion` op naming the open episode, or a
  revision op that completed its inventory closes it (spec 6.3);
* an admitted Claude rate-limit reading is checked for revocation, gated per
  axis, evaluated as a witness for each paused covered axis, and absorbed into
  its account's bounds; an UNSTAMPED admitted reading (journal history) is
  absorbed into the "excluded" scope;
* a bound authoritative OAuth reading is a per-axis control witness
  (`is_control_witness`), which an `outcome_authority_completion` must name;
* an `outcome_axis_release` opens the axis it names when it is valid;
* a manual `record-credit` revokes its account's weekly release; a
  correction, reviewed decision or protocol resolution revokes every release
  and voids every coverage (administrative records never erase a trigger);
* admitted snapshot, reset and credit events widen their scope's bounds.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import math

import _cctally_core
import _lib_account_binding
import _lib_account_protocol
import _lib_accounts
import _lib_journal
import _lib_outcome_quarantine as Q

#: The `src` values of a Claude rate-limit observation; must equal
#: `_cctally_record._CLAUDE_OBS_SRCS` (pinned by a test).
CLAUDE_OBS_SRCS = frozenset(
    {"statusline", "hook-tick", "refresh-usage", "record-usage"})

#: Administrative record kinds that can change strict dependencies. They never
#: erase a trigger; they revoke every release and void every coverage at their
#: position (spec 6.4, 6.9). Every version of each counts — the previous
#: release's exact-key kinds AND the versioned kinds the current binary emits.
_ADMIN_OP_KINDS = frozenset({
    "claude_weekly_observation_decision",
    _lib_account_protocol.WEEKLY_OBSERVATION_DECISION_V2_KIND,
}) | _lib_journal.PROTOCOL_RESOLUTION_KINDS
_ADMIN_RECORD_TYPES = frozenset({"correction", "correction_batch"})

TRIGGER_EXCLUDED = Q.TRIGGER_KIND_EXCLUDED
TRIGGER_LEGACY_SPOOL = Q.TRIGGER_KIND_LEGACY_SPOOL
LEGACY_ACTIVITY_CAUSE = Q.CAUSE_LEGACY_ACTIVITY
INVENTORY_INCOMPLETE_CAUSE = Q.CAUSE_INVENTORY_INCOMPLETE

#: The `legacy_spool_activity` reasons that close an open episode: every
#: journaled complete scan (spec 6.3).
_CLOSING_REASONS = frozenset((
    _lib_account_protocol.INVENTORY_COMPLETE_REASON, "changed",
    "baseline-unavailable"))

_FIVE_HOUR_FLOOR = 600
#: The recording pipeline honors a producer's planned five-hour key only
#: within three jitter floors of the raw reset (`_pipeline_claude_usage`).
_PLANNED_KEY_TOLERANCE = 3 * _FIVE_HOUR_FLOOR

#: The bounds-table rows that are not per-axis summaries.
_META_SCOPE = "*"
_META_AXIS = "meta"
_CONTROL_AXIS = "control"


def _epoch(value) -> "int | None":
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def normalize_week_end(epoch: int) -> int:
    """The pipeline's week-boundary normalization of an epoch."""
    return int(_cctally_core._normalize_week_boundary_dt(
        dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc)).timestamp())


def _week_window(resets_at: int):
    end = _cctally_core._normalize_week_boundary_dt(
        dt.datetime.fromtimestamp(int(resets_at), tz=dt.timezone.utc))
    start = end - dt.timedelta(days=7)
    return start.date().isoformat(), int(start.timestamp()), int(end.timestamp())


def is_claude_reading(rec) -> bool:
    if not isinstance(rec, dict) or rec.get("t") != "obs":
        return False
    if rec.get("provider") != "claude" or rec.get("src") not in CLAUDE_OBS_SRCS:
        return False
    payload = rec.get("payload")
    return (isinstance(payload, dict) and "resets_at" in payload
            and "weekly_percent" in payload)


def _record_account(rec) -> str:
    return rec.get("account") or _lib_accounts.UNATTRIBUTED


def _publication_kinds(payload) -> dict:
    """``{axis: provenance kind}`` of a status-line publication."""
    provenance = payload.get("publication")
    if not isinstance(provenance, dict):
        return {}
    out = {}
    for axis in Q.AXES:
        source = provenance.get(axis)
        if isinstance(source, dict):
            out[axis] = source.get("kind")
    return out


def _observed_axes(rec) -> frozenset:
    """The axes the detector mask treats as readings of their source
    (`_cctally_record._detector_observation_mask`, unchanged); empty when the
    detector step is withheld."""
    import _cctally_journal
    import _cctally_record

    payload = rec["payload"]
    mask = _cctally_record._detector_observation_mask(
        payload, _cctally_journal.detector_policy_of(payload),
        account_key=_record_account(rec),
        five_hour=payload.get("five_hour_percent") is not None)
    if mask is None:
        return frozenset()
    return frozenset(axis for axis, seen in zip(Q.AXES, mask) if seen)


def _planned_key(rec, raw5) -> "tuple[int | None, bool]":
    """``(key, invalid_plan)``: the five-hour key the pipeline uses."""
    if raw5 is None:
        return None, False
    planned = rec["payload"].get("five_hour_window_key")
    if _lib_account_protocol.is_stamped(rec) and planned is not None:
        if type(planned) is int and abs(planned - raw5) <= _PLANNED_KEY_TOLERANCE:
            return planned, False
        return raw5 - raw5 % _FIVE_HOUR_FLOOR, True
    return raw5 - raw5 % _FIVE_HOUR_FLOOR, False


def witness_eligible(rec, axis: str, *, held_ids=frozenset()) -> bool:
    """Spec 6.6 *Eligibility*, separate from the detector mask and never
    changing ordinary detector behavior: ``rec`` can witness ``axis`` only
    when the detector mask classifies the axis as observed, its detector
    policy is supported, a status-line publication's provenance for the axis
    is `candidate` (never `projection` or `retained_low`), it is not a
    reviewed hold on the axis, its planned key is one the pipeline honors,
    and it is a current-stamped Claude reading (manual and synthetic records
    are never Claude readings). Direct `record-usage` and OAuth observations
    are eligible for every axis they carry."""
    if not is_claude_reading(rec) or not _lib_account_protocol.is_stamped(rec):
        return False
    import _cctally_journal

    payload = rec["payload"]
    if _cctally_journal.detector_policy_of(payload) == "unsupported":
        return False
    if axis not in _observed_axes(rec):
        return False
    kinds = _publication_kinds(payload)
    if "publication_id" in payload and kinds.get(axis) != "candidate":
        return False
    if axis == Q.WEEKLY and rec.get("id") in held_ids:
        return False
    if axis == Q.FIVE_HOUR:
        raw5 = _epoch(payload.get("five_hour_resets_at"))
        key, invalid = _planned_key(rec, raw5)
        if key is None or invalid:
            return False
    return True


def reading_from_record(rec, *, held_ids=frozenset()) -> "Q.Reading | None":
    """The `Reading` one Claude rate-limit observation contributes, or None.

    Pure over the record: the week window is the normalized boundary the
    pipeline derives, the five-hour key is the producer's planned
    account-local key (a stamped record only, as the pipeline honors it,
    within the pipeline's tolerance of the raw reset) or the pure canonical
    floor, and the stored capture text is the payload's capture stamp
    verbatim (the pipeline's `captured_at_utc`)."""
    if not is_claude_reading(rec):
        return None
    payload = rec["payload"]
    detection = _epoch(rec.get("at"))
    captured = payload.get("captured_at") or rec.get("at")
    capture_epoch = _epoch(captured)
    if detection is None or capture_epoch is None:
        return None
    try:
        week_start_date, week_start_epoch, week_end_epoch = _week_window(
            int(payload["resets_at"]))
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    stamped = _lib_account_protocol.is_stamped(rec)
    raw5 = _epoch(payload.get("five_hour_resets_at"))
    key, _invalid = _planned_key(rec, raw5)
    account = _record_account(rec) if stamped else None
    projection = frozenset(axis for axis, kind in
                           _publication_kinds(payload).items()
                           if kind == "projection")
    eligible = frozenset(axis for axis in Q.AXES
                         if witness_eligible(rec, axis, held_ids=held_ids))
    return Q.Reading(
        observation_id=str(rec.get("id")),
        account_key=account,
        capture_text=str(captured),
        capture_epoch=capture_epoch,
        detection_epoch=detection,
        week_start_date=week_start_date,
        week_start_at_epoch=week_start_epoch,
        weekly_end_epoch=week_end_epoch,
        five_hour_key=key,
        raw_five_hour_reset=raw5,
        observed_axes=_observed_axes(rec) if stamped else frozenset(Q.AXES),
        eligible_axes=eligible,
        projection_axes=projection,
        stamped=stamped,
    )


def _valid_percent(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def is_control_witness(rec, *, axis: "str | None" = None,
                       held_ids=frozenset()) -> bool:
    """The journal half of an authority completion (spec 6.4, axis-aware): an
    admitted, current-stamped Claude reading with `src="record-usage"` and
    payload `source="api"` whose binding validates, names the observation's
    effective account and carries non-null credential evidence (the OAuth-bound
    capture), and that observes ``axis`` — weekly: a valid weekly percent and
    reset; five-hour: a valid five-hour percent and raw reset under a key the
    pipeline honors — and is not a status-line publication or a reviewed hold
    on the axis. ``axis=None`` asks for both axes."""
    if axis is None:
        return all(is_control_witness(rec, axis=a, held_ids=held_ids)
                   for a in Q.AXES)
    if not is_claude_reading(rec) or not _lib_account_protocol.is_stamped(rec):
        return False
    if rec.get("src") != "record-usage":
        return False
    payload = rec["payload"]
    if (payload.get("source") != "api" or "publication_id" in payload
            or "publication" in payload):
        return False
    try:
        binding = _lib_account_binding.load(payload.get("account_binding"))
    except (TypeError, ValueError, KeyError):
        return False
    if binding.credential_evidence is None:
        return False
    if binding.account_key != _record_account(rec):
        return False
    reading = reading_from_record(rec, held_ids=held_ids)
    if reading is None:
        return False
    if axis == Q.WEEKLY:
        return (_valid_percent(payload.get("weekly_percent"))
                and reading.week_start_date is not None
                and Q.WEEKLY in reading.eligible_axes)
    return (_valid_percent(payload.get("five_hour_percent"))
            and reading.raw_five_hour_reset is not None
            and Q.FIVE_HOUR in reading.eligible_axes)


def control_axes(rec, *, held_ids=frozenset()) -> frozenset:
    return frozenset(axis for axis in Q.AXES
                     if is_control_witness(rec, axis=axis, held_ids=held_ids))


def _scope_of(rec, payload) -> str:
    """The bounds scope an admitted event or op widens: its account when a
    current binary stamped it, else the every-account scope."""
    if not _lib_account_protocol.is_stamped(rec):
        return Q.EXCLUDED_SCOPE
    return payload.get("account_key") or _lib_accounts.UNATTRIBUTED


def _absorb_event(state, scope, rec, *, excluded=False) -> None:
    """An event's contribution to its scope's bounds (spec 6.5). A
    `snapshot_accept` capture is not by itself an effect: it widens the
    structural keys and capture order only — unless the event is itself an
    excluded record, whose capture joins C7 (spec 6.5 (d))."""
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    kind = payload.get("kind")
    if kind == "snapshot_accept":
        captured = payload.get("captured_at_utc")
        text = captured if isinstance(captured, str) else None
        capture = _epoch(captured)
        state.absorb_weekly_effect(
            scope, week_start_date=payload.get("week_start_date"),
            week_end_epoch=_epoch(payload.get("week_end_at")),
            capture_epoch=capture, capture_text=text,
            effective_epoch=capture if excluded else None)
        key = payload.get("five_hour_window_key")
        if type(key) is int:
            state.absorb_five_hour_effect(
                scope, five_hour_key=key,
                raw_reset=_epoch(payload.get("five_hour_resets_at")),
                capture_epoch=capture, capture_text=text)
    elif kind == "week_reset":
        state.absorb_weekly_effect(
            scope, effective_epoch=_epoch(payload.get("effective_reset_at_utc")),
            week_end_epoch=_epoch(payload.get("new_week_end_at")))
    elif kind == "five_hour_credit":
        key = payload.get("five_hour_window_key")
        if type(key) is int:
            state.absorb_five_hour_effect(scope, five_hour_key=key)


def _deletion_axis(payload) -> "str | None":
    kind = payload.get("kind")
    if kind == "week_reset":
        return Q.WEEKLY if payload.get("observed_pre_credit_pct") is not None else None
    if kind == "five_hour_credit":
        return Q.FIVE_HOUR
    if kind == "weekly_credit_effects":
        return Q.WEEKLY if payload.get("suppression") else None
    return None


def _absorb_credit_op(state, scope, payload) -> None:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    captured = plan.get("captured_iso")
    state.absorb_weekly_effect(
        scope,
        effective_epoch=_max_epoch(payload.get("effective_at_utc"), captured),
        week_start_date=payload.get("week_start_date"),
        capture_epoch=_epoch(captured),
        capture_text=captured if isinstance(captured, str) else None,
    )


def _max_epoch(*values):
    epochs = [e for e in (_epoch(v) for v in values) if e is not None]
    return max(epochs) if epochs else None


def _coordinate(value) -> "tuple[str, int] | None":
    if (isinstance(value, dict) and isinstance(value.get("segment"), str)
            and type(value.get("offset")) is int):
        return (value["segment"], value["offset"])
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and isinstance(value[0], str) and type(value[1]) is int):
        return (value[0], value[1])
    return None


class QuarantineFold:
    """One consumer's pass over journal records (see the module docstring).

    ``held_ids`` are the reviewed weekly holds of a rederive scratch replay
    (never witnesses). ``on_record`` returns, for a Claude reading, the
    per-axis gate ``{axis: GateDecision}`` for its automatic outcomes, else
    None."""

    def __init__(self, state: Q.QuarantineState, *, held_ids=frozenset()):
        self.state = state
        self.held_ids = frozenset(held_ids)
        #: Live-only conservative withholding: accounts whose deletion outcome
        #: this pass emitted and whose event has not been folded yet. Only used
        #: while some registered cross-period read is not inert.
        self.pending_deletion: set = set()

    # -- construction / persistence --------------------------------------
    @classmethod
    def empty(cls, **kwargs) -> "QuarantineFold":
        return cls(Q.QuarantineState(
            non_inert_cross_period=_non_inert_cross_period()), **kwargs)

    @classmethod
    def load(cls, conn, **kwargs) -> "QuarantineFold":
        return cls(load_state(conn), **kwargs)

    def persist(self, conn) -> None:
        persist_state(conn, self.state)

    # -- the fold ---------------------------------------------------------
    def _spool_position(self) -> None:
        self.state.set_position(normalize_week_end)

    def on_excluded(self, rec, *, segment=None, offset=None, family=None):
        """An excluded record: a trigger at its coordinate (a new
        generation); its readings and effects widen the every-account bounds
        (spec 6.10: it is its own cohort)."""
        if family is None and isinstance(rec, dict):
            family = _lib_account_protocol.record_family(rec)
        self.state.on_trigger(
            kind=TRIGGER_EXCLUDED, reason=str(family or "unknown"),
            segment=segment, offset=offset,
            at=rec.get("at") if isinstance(rec, dict) else None)
        if not isinstance(rec, dict):
            return
        reading = reading_from_record(rec)
        if reading is not None:
            self.state.absorb_reading(Q.EXCLUDED_SCOPE, reading,
                                      possible_effect=True, excluded=True)
            return
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if rec.get("t") == "evt":
            _absorb_event(self.state, Q.EXCLUDED_SCOPE, rec, excluded=True)
        elif rec.get("t") == "op" and payload.get("kind") == "weekly_credit_floor":
            _absorb_credit_op(self.state, Q.EXCLUDED_SCOPE, payload)

    def on_record(self, rec, *, segment=None, offset=None):
        """An admitted record."""
        if not isinstance(rec, dict):
            return None
        t = rec.get("t")
        if t == "obs" and not is_claude_reading(rec):
            # #997 D3 / #948 spec 6.11: derivation-completion scratch replay
            # omits such an observation from its window, so it must not move
            # H or the expiry clock here either (one canonical fold).
            return None
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        state = self.state
        at = rec.get("at")
        # Spec 6.3 expiry: the clock instant of an observation (its capture)
        # or an op (its `at`) AFTER P; evaluated before this record can move P.
        if t == "obs":
            clock = _epoch(payload.get("captured_at") or at)
        elif t == "op":
            clock = _epoch(at)
        else:
            clock = None
        state.note_clock(clock)
        # H: the greatest admitted `at` at or before P.
        state.note_admitted(_epoch(at))
        if t == "obs":
            reading = reading_from_record(rec, held_ids=self.held_ids)
            if reading is None:
                return None
            gates = state.evaluate_reading(reading.account_key, reading)
            if (reading.account_key is not None
                    and reading.account_key in self.pending_deletion):
                gates = Q.all_axes(Q.GateDecision(False, "pending-revocation"))
            if reading.account_key is not None:
                axes = control_axes(rec, held_ids=self.held_ids)
                if axes:
                    state.on_control_witness(reading.account_key, Q.ControlWitness(
                        reading.observation_id, reading.capture_epoch, axes,
                        reading.weekly_end_epoch))
            return gates
        if t == "op":
            self._on_op(rec, payload, segment=segment, offset=offset)
            return None
        if t in _ADMIN_RECORD_TYPES:
            state.revoke_all(Q.REVOKE_ADMINISTRATIVE, at=at, void_coverage=True)
            return None
        if t == "evt":
            scope = _scope_of(rec, payload)
            _absorb_event(state, scope, rec)
            axis = _deletion_axis(payload)
            if axis is not None:
                account = None if scope == Q.EXCLUDED_SCOPE else (
                    scope if payload.get("kind") != "weekly_credit_effects"
                    else None)
                state.on_deletion_outcome(account, axis, at=at)
                if account is None:
                    self.pending_deletion.clear()
                else:
                    self.pending_deletion.discard(account)
        return None

    def _on_op(self, rec, payload, *, segment, offset) -> None:
        state = self.state
        kind = payload.get("kind")
        stamped = _lib_account_protocol.is_stamped(rec)
        at = rec.get("at")
        if kind == _lib_account_protocol.LEGACY_SPOOL_ACTIVITY_KIND:
            if not stamped:
                return
            reason = str(payload.get("reason") or "changed")
            state.on_trigger(kind=TRIGGER_LEGACY_SPOOL, reason=reason,
                             segment=segment, offset=offset, at=at)
            if reason == _lib_account_protocol.INVENTORY_INCOMPLETE_REASON:
                state.on_episode_open(segment=segment, offset=offset, at=at)
                return
            if reason in _CLOSING_REASONS:
                state.on_episode_close(segment=segment, offset=offset, at=at)
            self._spool_position()
            return
        if kind in (_lib_account_protocol.CUTOVER_OP_KIND,
                    _lib_account_protocol.REVISION_OP_KIND,
                    _lib_account_protocol.REVISION3_OP_KIND):
            expected_id = {
                _lib_account_protocol.CUTOVER_OP_KIND:
                    _lib_account_protocol.CUTOVER_OP_ID,
                _lib_account_protocol.REVISION_OP_KIND:
                    _lib_account_protocol.REVISION_OP_ID,
                _lib_account_protocol.REVISION3_OP_KIND:
                    _lib_account_protocol.REVISION3_OP_ID,
            }[kind]
            if not stamped or rec.get("id") != expected_id:
                return
            if payload.get("legacySpoolInventoryFailure") is None:
                # A boundary that completed its (expanded) inventory is a
                # journaled complete scan: it closes an open episode, as the
                # journal's inventory pass treats it.
                if state.on_episode_close(segment=segment, offset=offset, at=at):
                    self._spool_position()
                return
            episode = _coordinate(payload.get("inventoryEpisode"))
            if episode is not None and state.on_episode_continue(
                    segment=episode[0], offset=episode[1]):
                return
            state.on_trigger(
                kind=TRIGGER_LEGACY_SPOOL,
                reason=_lib_account_protocol.INVENTORY_INCOMPLETE_REASON,
                segment=segment, offset=offset, at=at)
            opener = episode if episode is not None else (segment, offset)
            state.on_episode_open(segment=opener[0], offset=opener[1], at=at)
            return
        if kind == _lib_account_protocol.INVENTORY_COMPLETION_OP_KIND:
            if stamped and state.on_episode_close(
                    segment=segment, offset=offset, at=at,
                    episode=_coordinate(payload.get("episode"))):
                self._spool_position()
            return
        if kind == _lib_account_protocol.AUTHORITY_COMPLETION_OP_KIND:
            if stamped and isinstance(payload.get("account"), str):
                state.on_authority_completion(
                    payload["account"], str(rec.get("id")),
                    generation=payload.get("generation"),
                    observation_id=payload.get("observation"),
                    axes=tuple(payload.get("axes") or ()))
            return
        if kind == _lib_account_protocol.AXIS_RELEASE_OP_KIND:
            if stamped and isinstance(payload.get("account"), str):
                state.on_release(
                    payload["account"], payload.get("axis"),
                    generation=payload.get("generation"),
                    witness_id=payload.get("witness"),
                    completion_id=payload.get("authority"),
                    release_id=str(rec.get("id")), at=at)
            return
        if kind == "weekly_credit_floor":
            # A manual `record-credit` stays available as the operator's
            # assertion, and revokes the account's weekly release BEFORE its
            # dependency-changing effects run (this fold precedes the hooks).
            # Its synthetic snapshot is an event, never a witness.
            scope = _scope_of(rec, payload)
            if scope == Q.EXCLUDED_SCOPE:
                state.revoke_all(Q.REVOKE_MANUAL_CREDIT, at=at, axes=(Q.WEEKLY,))
            else:
                state.revoke_axis(scope, Q.WEEKLY, Q.REVOKE_MANUAL_CREDIT, at=at)
            _absorb_credit_op(state, scope, payload)
            return
        if kind in _ADMIN_OP_KINDS:
            state.revoke_all(Q.REVOKE_ADMINISTRATIVE, at=at, void_coverage=True)

    def note_deletion_outcome(self, account_key) -> None:
        """The pipeline just decided a deletion outcome for ``account_key``
        whose event folds later in journal order. While a registered
        cross-period read is not inert, the account's later outcomes in this
        pass are withheld until that event revokes it."""
        if self.state.non_inert_cross_period and account_key is not None:
            self.pending_deletion.add(account_key)


def _non_inert_cross_period() -> bool:
    """True when the access-contract registry holds a cross-period read with
    no inert rule (spec 5.6 items 5 and 6)."""
    try:
        import _lib_detector_access_registry as registry
    except ImportError:
        return True
    return bool(registry.non_inert_cross_period())


def _loads(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def load_state(conn) -> Q.QuarantineState:
    """The persisted fold state. Missing tables fail closed (raise), and so
    does an unverifiable bounds document; an unverifiable axis row reads as
    paused with no coverage, never as clean (spec 6.11)."""
    latest = conn.execute(
        "SELECT seq, segment, byte_offset, kind, reason, at "
        "FROM outcome_quarantine_triggers ORDER BY seq DESC LIMIT 1").fetchone()
    trigger = None if latest is None else Q.Trigger(
        int(latest[0]), latest[1], latest[2], latest[3], latest[4], latest[5])
    episodes = [
        Q.Episode(*row) for row in conn.execute(
            "SELECT opener_segment, opener_offset, opened_at, closer_segment, "
            "closer_offset, closed_at FROM outcome_quarantine_episodes "
            "ORDER BY rowid")]
    bounds: dict = {}
    control: dict = {}
    meta = None
    for scope, axis, text in conn.execute(
            "SELECT scope, axis, bounds_json FROM outcome_quarantine_bounds"):
        doc = _loads(text)
        if axis == _META_AXIS and scope == _META_SCOPE:
            meta = doc
            continue
        if axis == _CONTROL_AXIS:
            witness = Q.ControlWitness.from_document(doc)
            if witness is not None:
                control[scope] = witness
            continue
        current = bounds.get(scope) or Q.ScopeBounds()
        if axis == Q.WEEKLY:
            value = Q.WeeklyBounds.from_document(doc)
            if value is None:
                raise sqlite3.DatabaseError(
                    f"outcome quarantine bounds for {scope!r} are unverifiable")
            bounds[scope] = Q.ScopeBounds(weekly=value, five_hour=current.five_hour)
        elif axis == Q.FIVE_HOUR:
            value = Q.FiveHourBounds.from_document(doc)
            if value is None:
                raise sqlite3.DatabaseError(
                    f"outcome quarantine bounds for {scope!r} are unverifiable")
            bounds[scope] = Q.ScopeBounds(weekly=current.weekly, five_hour=value)
    cohort = None
    expired = False
    max_at = None
    if meta is not None:
        if (not isinstance(meta, dict)
                or set(meta) != {"cohort", "expired", "maxAdmittedAt"}
                or type(meta["expired"]) is not bool
                or (meta["maxAdmittedAt"] is not None
                    and type(meta["maxAdmittedAt"]) is not int)):
            raise sqlite3.DatabaseError(
                "outcome quarantine generation state is unverifiable")
        if meta["cohort"] is not None:
            cohort = Q.Cohort.from_document(meta["cohort"])
            if cohort is None:
                raise sqlite3.DatabaseError(
                    "outcome quarantine cohort is unverifiable")
        expired = meta["expired"]
        max_at = meta["maxAdmittedAt"]
    generation = 0 if trigger is None else trigger.seq
    accounts: dict = {}
    if generation:
        for row in conn.execute(
                "SELECT account_key, axis, state, waiting_for, coverage_id, "
                "witness_id, release_id, revocation, since, recovered_at, "
                "not_before, witness_key FROM outcome_quarantine_axis_state "
                "WHERE generation = ?", (generation,)):
            if row[1] not in Q.AXES:
                continue
            rec = Q.axis_from_row(tuple(row[2:]))
            if rec is None:
                rec = Q.AxisRecovery(since=trigger.at)
            accounts.setdefault(row[0], {})[row[1]] = rec
    return Q.QuarantineState(
        generation=generation, latest_trigger=trigger, episodes=episodes,
        cohort=cohort, expired=expired, max_admitted_at=max_at,
        bounds=bounds, accounts=accounts, control=control,
        non_inert_cross_period=_non_inert_cross_period())


def _dumps(doc) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def persist_state(conn, state: Q.QuarantineState) -> None:
    """Write only what changed (inside the caller's transaction)."""
    for trig in state.new_triggers:
        conn.execute(
            "INSERT INTO outcome_quarantine_triggers "
            "(seq, segment, byte_offset, kind, reason, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (trig.seq, trig.segment, trig.offset, trig.kind, trig.reason,
             trig.at),
        )
    for episode in state.episodes:
        if episode.key not in state.dirty_episodes:
            continue
        conn.execute(
            "INSERT INTO outcome_quarantine_episodes "
            "(opener_segment, opener_offset, opened_at, closer_segment, "
            " closer_offset, closed_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(opener_segment, opener_offset) DO UPDATE SET "
            "closer_segment = excluded.closer_segment, "
            "closer_offset = excluded.closer_offset, "
            "closed_at = excluded.closed_at",
            (episode.opener_segment, episode.opener_offset, episode.opened_at,
             episode.closer_segment, episode.closer_offset, episode.closed_at),
        )
    for scope in sorted(state.dirty_bounds):
        bounds = state.bounds[scope]
        for axis, value in ((Q.WEEKLY, bounds.weekly),
                            (Q.FIVE_HOUR, bounds.five_hour)):
            conn.execute(
                "INSERT OR REPLACE INTO outcome_quarantine_bounds "
                "(scope, axis, bounds_json) VALUES (?, ?, ?)",
                (scope, axis, _dumps(value.to_document())))
    if state.dirty_meta:
        conn.execute(
            "INSERT OR REPLACE INTO outcome_quarantine_bounds "
            "(scope, axis, bounds_json) VALUES (?, ?, ?)",
            (_META_SCOPE, _META_AXIS, _dumps({
                "cohort": (None if state.cohort is None
                           else state.cohort.to_document()),
                "expired": state.expired,
                "maxAdmittedAt": state.max_admitted_at})))
    for account_key in sorted(state.dirty_control):
        witness = state.control.get(account_key)
        if witness is None:
            conn.execute(
                "DELETE FROM outcome_quarantine_bounds "
                "WHERE scope = ? AND axis = ?", (account_key, _CONTROL_AXIS))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO outcome_quarantine_bounds "
                "(scope, axis, bounds_json) VALUES (?, ?, ?)",
                (account_key, _CONTROL_AXIS, _dumps(witness.to_document())))
    if state.generation:
        for account_key in sorted(state.dirty_accounts):
            for axis in Q.AXES:
                conn.execute(
                    "INSERT OR REPLACE INTO outcome_quarantine_axis_state "
                    "(generation, account_key, axis, state, waiting_for, "
                    " coverage_id, witness_id, release_id, revocation, since, "
                    " recovered_at, not_before, witness_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (state.generation, account_key, axis,
                     *Q.axis_row(state, account_key, axis)))
    state.new_triggers = []
    state.dirty_episodes = set()
    state.dirty_bounds = set()
    state.dirty_accounts = set()
    state.dirty_control = set()
    state.dirty_meta = False


def fold_through(conn, tail_records=()) -> Q.QuarantineState:
    """The canonical fold applied to the validated index state on ``conn``
    plus ``tail_records``, the journal records past its cursor (spec 6.7:
    so a lagging index never decides a witness). Each item is ``(record,
    segment, offset)`` or ``(record, segment, offset, excluded)``. Nothing is
    persisted."""
    fold = QuarantineFold.load(conn)
    for item in tail_records:
        rec, segment, offset = item[0], item[1], item[2]
        if len(item) > 3 and item[3]:
            fold.on_excluded(rec, segment=segment, offset=offset)
        else:
            fold.on_record(rec, segment=segment, offset=offset)
    return fold.state


# --------------------------------------------------------------------------
# read side
# --------------------------------------------------------------------------

def quarantine_gate(ctx, account_key, reading=None) -> dict:
    """The per-axis gate ``{axis: GateDecision}`` for ``account_key``'s
    automatic outcomes on the record whose hooks are running. Without a fold
    (a direct caller) everything is allowed, exactly as before the quarantine
    existed. A single `GateDecision` a caller installed applies to both
    axes."""
    gate = getattr(ctx, "outcome_gate", None) if ctx is not None else None
    if gate is None:
        return Q.all_axes(Q.GATE_NO_TRIGGER)
    if isinstance(gate, Q.GateDecision) or hasattr(gate, "allowed"):
        return Q.all_axes(gate)
    return {axis: gate.get(axis, Q.GateDecision(False, "missing"))
            for axis in Q.AXES}


def drop_permitted(state: Q.QuarantineState, account_key: str, axis: str) -> bool:
    """Whether drop consensus may publish on ``axis`` of ``account_key``:
    no trigger, or the axis is released in the current generation (spec
    6.8). Journal-derived release state only."""
    return state.axis_gate(account_key, axis).allowed


def account_status(conn, account_key: str) -> dict:
    """``{"triggered", "generation", "axes": {axis: {"allowed", "state",
    "waitingFor"}}}`` read from ``conn``'s current snapshot. Raises on a
    missing table."""
    state = load_state(conn)
    return {
        "triggered": state.has_trigger(),
        "generation": state.generation,
        "axes": {
            axis: {"allowed": drop_permitted(state, account_key, axis),
                   "state": (Q.STATE_RELEASED
                             if state.released(account_key, axis)
                             or not state.has_trigger() else Q.STATE_PAUSED),
                   "waitingFor": state.waiting_for(account_key, axis)}
            for axis in Q.AXES},
    }


def _claude_last_seen(conn) -> dict:
    """The latest instant each Claude account was seen in use, as an epoch
    (#948 review F7): doctor lists only accounts seen in the last seven days.
    Pre-cutover history widens only the shared excluded scope, so the
    snapshot rows and the registry's ``last_seen_utc`` supply the instant the
    fold's per-account bounds lack."""
    seen = {}
    rows = list(conn.execute(
        "SELECT account_key, MAX(captured_at_utc) FROM weekly_usage_snapshots "
        "WHERE account_key IS NOT NULL GROUP BY account_key"))
    rows += list(conn.execute(
        "SELECT account_key, last_seen_utc FROM accounts "
        "WHERE provider = 'claude' AND last_seen_utc IS NOT NULL"))
    for account_key, text in rows:
        try:
            moment = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        epoch = int(moment.timestamp())
        seen[account_key] = max(epoch, seen.get(account_key, epoch))
    return seen


def _known_claude_accounts(conn) -> set:
    """Every Claude account the retained registry or usage history knows,
    including an existing `unattributed` usage bucket."""
    known = {row[0] for row in conn.execute(
        "SELECT account_key FROM accounts WHERE provider = 'claude'")}
    known.update(row[0] for row in conn.execute(
        "SELECT DISTINCT account_key FROM weekly_usage_snapshots "
        "WHERE account_key IS NOT NULL"))
    return known


def _episode_document(episode: "Q.Episode | None") -> "dict | None":
    if episode is None:
        return None
    return {
        "state": "open" if episode.open else "closed",
        "opener": {"segment": episode.opener_segment,
                   "offset": episode.opener_offset,
                   "at": episode.opened_at},
        "closer": (None if episode.open else {
            "segment": episode.closer_segment,
            "offset": episode.closer_offset, "at": episode.closed_at}),
    }


def report(conn, **_ignored) -> dict:
    """The per-account, per-axis quarantine report `doctor` renders (spec
    7.1). Shape::

        {"triggered": bool,
         "generation": int | None,
         "cause": "legacy-spool-activity" | "inventory-incomplete"
                  | "excluded-write" | None,
         "inventoryEpisode": None | {"state": "open" | "closed",
                                     "opener": {segment, offset, at},
                                     "closer": None | {segment, offset, at}},
         "accounts": [{"account", "cause", "paused": bool,
                       "axes": {"sevenDay" | "fiveHour": {
                           "state": "paused" | "recovered",
                           "waitingFor": "inventory" | "expiry"
                               | "usage-refresh" | "fresh-period" | None,
                           "notBefore": ISO | None,
                           "since": ISO | None,
                           "recoveredAt": ISO | None,
                           "revocation": reason | None}},
                       "lastSeenAt": ISO | None}]}

    Every known Claude account is listed while a trigger exists (recovered
    ones included, for doctor's seven-day note); ``lastSeenAt`` is its latest
    retained reading, and doctor leaves out an account idle for seven days
    (operator decision F7). ``notBefore`` is never an estimate; doctor omits
    it when null or past. Raises on a missing table (the caller reports
    "unavailable")."""
    state = load_state(conn)
    if not state.has_trigger():
        return {"triggered": False, "generation": None, "cause": None,
                "inventoryEpisode": None, "accounts": []}
    cause = Q.cause_of(state.latest_trigger,
                       _lib_account_protocol.INVENTORY_REASONS)
    known = set(state.accounts) | set(state.control)
    known.update(scope for scope in state.bounds
                 if scope not in (Q.EXCLUDED_SCOPE, Q.COHORT_SCOPE))
    known.update(_known_claude_accounts(conn))
    seen = _claude_last_seen(conn)
    rows = []
    for account_key in sorted(known):
        scope = state.bounds.get(account_key)
        last_seen = [seen.get(account_key)]
        if scope is not None:
            last_seen += [scope.weekly.k7, scope.five_hour.k5c]
        last_seen = [value for value in last_seen if value is not None]
        axes = {}
        for axis in Q.AXES:
            rec = state.axis(account_key, axis)
            released = state.released(account_key, axis)
            axes[axis] = {
                "state": Q.STATE_RELEASED if released else Q.STATE_PAUSED,
                "waitingFor": state.waiting_for(account_key, axis),
                "notBefore": Q.iso(state.not_before(account_key, axis)),
                "since": rec.since,
                "recoveredAt": rec.recovered_at if released else None,
                "revocation": rec.revocation,
            }
        # Review finding F8: the cause is the account's own. Limits that wait
        # for the spool inventory are paused by the incomplete inventory,
        # whatever trigger the generation saw last.
        waiting_inventory = any(a["waitingFor"] == Q.WAIT_INVENTORY
                                for a in axes.values())
        rows.append({
            "account": account_key,
            "cause": (Q.CAUSE_INVENTORY_INCOMPLETE if waiting_inventory
                      else cause),
            "paused": any(a["state"] == Q.STATE_PAUSED for a in axes.values()),
            "axes": axes,
            "lastSeenAt": Q.iso(max(last_seen)) if last_seen else None,
        })
    latest_episode = state.episodes[-1] if state.episodes else None
    return {"triggered": True, "generation": state.generation, "cause": cause,
            "inventoryEpisode": _episode_document(latest_episode),
            "accounts": rows}


# --------------------------------------------------------------------------
# writers: the release protocol (spec 6.7) and authority completion (6.4)
# --------------------------------------------------------------------------

def release_step(account_key: str, *, conn, tail_records=()) -> list:
    """Append the release of every axis of ``account_key`` whose latest
    X-bearing record is a qualifying witness without one, judged on the fold
    through ``tail_records`` or the index. The record path runs it twice per
    append: the resume step before the append and the live step right after
    it (whose record the fold already holds); both are this one rule. The
    caller holds the selected-state lock. Returns the appended release
    ids."""
    import _cctally_journal

    state = fold_through(conn, tail_records)
    out = []
    for axis in Q.AXES:
        witness = state.pending_witness(account_key, axis)
        if witness is None:
            continue
        rec = state.axis(account_key, axis)
        out.append(_cctally_journal.append_outcome_axis_release(
            generation=state.generation, account=account_key, axis=axis,
            witness_id=witness, authority_id=rec.coverage_id,
            now_utc=_cctally_core._command_as_of()))
    return out


def _index_covers_journal(conn) -> bool:
    """Whether the index's applied cursor is at the journal's last complete
    line (so its fold state includes every appended record)."""
    import _cctally_journal

    row = conn.execute(
        "SELECT segment, offset, applied_segment, applied_offset "
        "FROM journal_cursor WHERE id = 1").fetchone()
    if row is None or row[2] is None or row[3] is None:
        return False
    segments = _cctally_journal.list_segments()
    if not segments:
        return False
    last = segments[-1]
    path = _cctally_core.JOURNAL_DIR / last
    complete = _cctally_journal._complete_line_offset(path, path.stat().st_size)
    return str(row[2]) == last and int(row[3]) >= complete


#: Review finding F10: whether this process already logged an unavailable
#: index (`_log_unavailable_once`).
_UNAVAILABLE_LOGGED = False


def _log_unavailable_once(exc: BaseException) -> None:
    """Note an index failure `_with_covered_index` turned into "decide
    nothing" in hook-tick.log, once per process (review finding F10): a
    stalled recovery leaves a trace, and a persistent failure on the
    render path cannot write a line per render. Best-effort."""
    global _UNAVAILABLE_LOGGED
    if _UNAVAILABLE_LOGGED:
        return
    _UNAVAILABLE_LOGGED = True
    try:
        from _cctally_record import (
            _hook_log_error_detail, _hook_log_safe_free_text,
            _hook_tick_log_line, _hook_tick_log_rotate_if_needed,
        )
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        _hook_tick_log_line(
            f"{stamp} provider=claude op=outcome-quarantine-index "
            f"result=unavailable "
            f"error={_hook_log_safe_free_text(_hook_log_error_detail(exc))}")
        _hook_tick_log_rotate_if_needed()
    except Exception:  # noqa: BLE001 — logging never breaks a writer
        pass


def _with_covered_index(action):
    """Run ``action(conn)`` against a validated index snapshot that covers
    the whole journal, running a bounded authoritative catch-up when the
    index lags while it holds a trigger. Any failure — an unreadable, rebuilding or
    healing index (its deferral included: it is a `BaseException`), an
    ingest that cannot catch up — returns None: the caller writes nothing
    (fail closed; at worst recovery moves to a later period, R7) and its
    own recording proceeds exactly as it would without the quarantine. An
    `Exception` is logged once per process (`_log_unavailable_once`); a
    deferral is the index's ordinary rebuilding state and is not."""
    import _cctally_db
    import _cctally_journal
    import _cctally_store

    deferred = _cctally_db.StatsRebuildDeferred
    # Up to two catch-up ingests: the first folds the caller's own append and
    # may itself journal derived events for it (Model-A emission), which the
    # second folds. Bounded: a journal still growing after that decides
    # nothing (fail closed).
    attempts = 3
    for attempt in range(attempts):
        try:
            conn = _cctally_core.open_db()
        except deferred:  # an unavailable index decides nothing
            return None
        except Exception as exc:  # noqa: BLE001
            _log_unavailable_once(exc)
            return None
        try:
            with _cctally_store.current_stats_snapshot(conn):
                triggered = conn.execute(
                    "SELECT 1 FROM outcome_quarantine_triggers LIMIT 1"
                ).fetchone() is not None
                if not triggered:
                    return None
                if _index_covers_journal(conn):
                    return action(conn)
        except deferred:
            return None
        except Exception as exc:  # noqa: BLE001
            _log_unavailable_once(exc)
            return None
        finally:
            conn.close()
        if attempt < attempts - 1:
            try:
                _cctally_journal.run_stats_ingest(mode="authoritative")
            except deferred:
                return None
            except Exception as exc:  # noqa: BLE001
                _log_unavailable_once(exc)
                return None
    return None


def _ingest_after_append() -> None:
    import _cctally_db
    import _cctally_journal

    try:
        _cctally_journal.run_stats_ingest(mode="opportunistic")
    except (Exception, _cctally_db.StatsRebuildDeferred):  # noqa: BLE001
        pass  # the record is durable; a later ingest catches up


def run_release_step(account_key: str) -> "list | None":
    """`release_step` against a covered index, for a writer holding the
    selected-state lock. Free of any write when no trigger exists, and of
    any journal record unless a qualifying witness awaits its release.

    Returns the appended release ids, or None when nothing was decided —
    no trigger, or an unavailable index (fail closed). The record path runs
    its live step after the append only when the resume step decided
    (review finding F13): with no trigger an append opens the index once,
    and a step skipped on failure is the next append's resume step."""
    released = _with_covered_index(lambda conn: release_step(
        account_key, conn=conn))
    if released:
        _ingest_after_append()
    return released


def run_authority_completion(account_key: str, observation_id: str,
                             authority_commit: "str | None") -> "str | None":
    """Spec 6.4: after the OAuth observation, its fences and the
    selected-state reconciliation have committed, append one
    `outcome_authority_completion` listing exactly the axes this observation
    newly covers — none when it covers nothing (no trigger, already covered,
    before expiry, an open episode, an axis it does not observe)."""
    import _cctally_journal

    def action(conn):
        state = load_state(conn)
        axes = state.coverable_axes(account_key, observation_id)
        if not axes:
            return None
        return _cctally_journal.append_outcome_authority_completion(
            generation=state.generation, account=account_key,
            observation_id=observation_id, axes=tuple(axes),
            authority_commit=authority_commit or observation_id,
            now_utc=_cctally_core._command_as_of())

    appended = _with_covered_index(action)
    if appended is not None:
        _ingest_after_append()
    return appended
