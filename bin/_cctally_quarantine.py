"""Outcome quarantine glue (#825/#832, spec 5.6): the journal-order fold over
records, its persistence in the disposable stats index, and the read side
`doctor` and the statusline reducer consume.

Every consumer folds the SAME records in the SAME canonical journal order:

* live ingest (`_cctally_journal._run_cycle`) over each cycle's decoded batch,
  excluded records included, with the pipeline hooks of a record running only
  after the fold has advanced through it;
* `db rebuild --db stats` over the whole pinned prefix, inline in its read
  pass;
* the `db rederive` scratch replay, over the planner's record list, whose
  excluded slots carry the original record for exactly this purpose.

So triggers, envelopes and certificates are a function of the journal alone.

What each record contributes:

* an EXCLUDED record — any type, any family — is a trigger at its coordinate;
  an excluded Claude reading, event or credit also widens the "excluded"
  envelope, which applies to every account (the account an excluded record
  names never narrows anything);
* a current `legacy_spool_activity` op is a trigger, and it leaves an
  unresolved spool marker that withholds every account's accounting
  certificate; nothing clears it (spec revision 3, section 5.6 item 1(b)).
  Spec revision 5: its `inventory-incomplete` reason, and a cutover op that
  recorded its inventory as incomplete, are such triggers too;
* an admitted Claude rate-limit reading is certified, checked for continuing
  validity, and absorbed into its account's envelope; an UNSTAMPED admitted
  reading (journal history) is absorbed into the "excluded" scope, because its
  account is whatever an older binary resolved;
* a bound two-axis authoritative OAuth reading is the journal half of a
  control witness (`is_control_witness`); drop eligibility and doctor also
  require the account's reconciled transition to record its commit;
* a manual `record-credit` revokes its account's accounting certificate before
  its effects; a correction, reviewed decision or protocol resolution revokes
  every account's (administrative records never erase a trigger);
* admitted snapshot, reset and credit events widen their account's envelope.
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
#: erase a trigger; they revoke every accounting certificate at their position
#: (spec 5.6 item 8). Every version of each counts — the previous release's
#: exact-key kinds AND the versioned kinds the current binary emits — so the
#: repair kinds come from the journal's own shared set.
_ADMIN_OP_KINDS = frozenset({
    "claude_weekly_observation_decision",
    _lib_account_protocol.WEEKLY_OBSERVATION_DECISION_V2_KIND,
}) | _lib_journal.PROTOCOL_RESOLUTION_KINDS
_ADMIN_RECORD_TYPES = frozenset({"correction", "correction_batch"})

TRIGGER_EXCLUDED = "excluded"
TRIGGER_LEGACY_SPOOL = Q.TRIGGER_KIND_LEGACY_SPOOL
#: `report` row ``spoolMarkerCause`` values (spec section 8): which approved
#: wording an unresolved spool marker takes.
LEGACY_ACTIVITY_CAUSE = "legacy-spool-activity"
INVENTORY_INCOMPLETE_CAUSE = "inventory-incomplete"

_FIVE_HOUR_FLOOR = 600
#: The recording pipeline honors a producer's planned five-hour key only
#: within three jitter floors of the raw reset (`_pipeline_claude_usage`).
_PLANNED_KEY_TOLERANCE = 3 * _FIVE_HOUR_FLOOR

#: Why a reading whose planned key the pipeline would reject never witnesses.
INELIGIBLE_INVALID_PLANNED_KEY = "invalid-planned-key"


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


def _week_window(resets_at: int):
    end = _cctally_core._normalize_week_boundary_dt(
        dt.datetime.fromtimestamp(int(resets_at), tz=dt.timezone.utc))
    start = end - dt.timedelta(days=7)
    return start.date().isoformat(), int(start.timestamp()), int(end.timestamp())


def _provenance_ineligibility(payload) -> "str | None":
    provenance = payload.get("publication")
    if not isinstance(provenance, dict):
        return None
    for axis in ("sevenDay", "fiveHour"):
        source = provenance.get(axis)
        if not isinstance(source, dict):
            continue
        kind = source.get("kind")
        if kind == "projection":
            return "projection-fill"
        if kind == "retained_low":
            return "retained-low"
    return None


def is_claude_reading(rec) -> bool:
    if not isinstance(rec, dict) or rec.get("t") != "obs":
        return False
    if rec.get("provider") != "claude" or rec.get("src") not in CLAUDE_OBS_SRCS:
        return False
    payload = rec.get("payload")
    return (isinstance(payload, dict) and "resets_at" in payload
            and "weekly_percent" in payload)


def reading_from_record(rec, *, held_ids=frozenset()) -> "Q.Reading | None":
    """The `Reading` one Claude rate-limit observation contributes, or None.

    Pure over the record: the week window is the normalized boundary the
    pipeline derives, the five-hour key is the producer's planned
    account-local key (a stamped record only, as the pipeline honors it,
    within the pipeline's 1,800 s tolerance of the raw reset) or the pure
    canonical floor, and the stored capture text is the payload's capture
    stamp verbatim (the pipeline's `captured_at_utc`). A planned key outside
    that tolerance is a corrupt plan: it is not honored, and the reading can
    never be a witness."""
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
    key = None
    invalid_plan = False
    if raw5 is not None:
        planned = payload.get("five_hour_window_key")
        if stamped and planned is not None:
            if (type(planned) is int
                    and abs(planned - raw5) <= _PLANNED_KEY_TOLERANCE):
                key = planned
            else:
                invalid_plan = True
        if key is None:
            key = raw5 - raw5 % _FIVE_HOUR_FLOOR
    two_axis = (raw5 is not None
                and payload.get("five_hour_percent") is not None)
    ineligible = _provenance_ineligibility(payload)
    if ineligible is None and invalid_plan:
        ineligible = INELIGIBLE_INVALID_PLANNED_KEY
    if ineligible is None and rec.get("id") in held_ids:
        ineligible = "reviewed-hold"
    account = None
    if stamped:
        account = rec.get("account") or _lib_accounts.UNATTRIBUTED
    return Q.Reading(
        observation_id=str(rec.get("id")),
        account_key=account,
        captured_at_utc=str(captured),
        capture_epoch=capture_epoch,
        detection_epoch=detection,
        week_start_date=week_start_date,
        week_start_at_epoch=week_start_epoch,
        weekly_end_epoch=week_end_epoch,
        five_hour_key=key,
        raw_five_hour_reset=raw5,
        two_axis=two_axis,
        ineligible_reason=ineligible,
        stamped=stamped,
    )


def _valid_percent(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def is_control_witness(rec, *, held_ids=frozenset()) -> bool:
    """The journal half of a control witness (spec 5.6 item 7; #825/#832
    fix-round-1 Q6): an admitted, current-stamped Claude reading with
    `src="record-usage"` and payload `source="api"` whose binding validates
    (`_lib_account_binding.load`), names the observation's effective account
    and carries valid, non-null credential evidence — the OAuth-bound
    capture, which the CLI adapter's binding never has — with both valid raw
    utilization/reset axes, and no statusline publication, projection-filled
    axis, retained low, invalid planned key or reviewed hold.

    Credential evidence proves an OAuth-bound reading, not that control
    reconciliation succeeded after its append: drop eligibility and doctor
    also require the account's reconciled transition to record
    `authorityCommit == observation.id`."""
    if not is_claude_reading(rec) or not _lib_account_protocol.is_stamped(rec):
        return False
    if rec.get("src") != "record-usage":
        return False
    payload = rec["payload"]
    if payload.get("source") != "api" or "publication_id" in payload:
        return False
    try:
        binding = _lib_account_binding.load(payload.get("account_binding"))
    except (TypeError, ValueError, KeyError):
        return False
    if binding.credential_evidence is None:
        return False
    if binding.account_key != (rec.get("account") or _lib_accounts.UNATTRIBUTED):
        return False
    if not (_valid_percent(payload.get("weekly_percent"))
            and _valid_percent(payload.get("five_hour_percent"))):
        return False
    reading = reading_from_record(rec, held_ids=held_ids)
    return (reading is not None and reading.ineligible_reason is None
            and reading.two_axis and reading.week_start_date is not None
            and reading.raw_five_hour_reset is not None)


def _scope_of(rec, payload) -> str:
    """The envelope scope an admitted event or op widens: its account when a
    current binary stamped it, else the every-account scope."""
    if not _lib_account_protocol.is_stamped(rec):
        return Q.EXCLUDED_SCOPE
    return payload.get("account_key") or _lib_accounts.UNATTRIBUTED


def _absorb_event(state, scope, rec) -> None:
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    kind = payload.get("kind")
    if kind == "snapshot_accept":
        captured = payload.get("captured_at_utc")
        state.absorb_effect(
            scope,
            effective_epoch=_epoch(captured),
            week_start_date=payload.get("week_start_date"),
            five_hour_key=(payload.get("five_hour_window_key")
                           if type(payload.get("five_hour_window_key")) is int
                           else None),
            captured_at_utc=captured if isinstance(captured, str) else None,
            weekly_end_epoch=_epoch(payload.get("week_end_at")),
            raw_five_hour_reset=_epoch(payload.get("five_hour_resets_at")),
        )
    elif kind == "week_reset":
        state.absorb_effect(
            scope,
            effective_epoch=_epoch(payload.get("effective_reset_at_utc")),
            weekly_end_epoch=_epoch(payload.get("new_week_end_at")),
        )
    elif kind == "five_hour_credit":
        key = payload.get("five_hour_window_key")
        state.absorb_effect(
            scope,
            effective_epoch=_epoch(payload.get("effective_reset_at_utc")),
            five_hour_key=key if type(key) is int else None,
        )


def _is_deletion_outcome(payload) -> bool:
    kind = payload.get("kind")
    if kind == "week_reset":
        return payload.get("observed_pre_credit_pct") is not None
    if kind == "five_hour_credit":
        return True
    if kind == "weekly_credit_effects":
        return bool(payload.get("suppression"))
    return False


def _absorb_credit_op(state, scope, payload) -> None:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    captured = plan.get("captured_iso")
    state.absorb_effect(
        scope,
        effective_epoch=_max_epoch(payload.get("effective_at_utc"), captured),
        week_start_date=payload.get("week_start_date"),
        captured_at_utc=captured if isinstance(captured, str) else None,
    )


def _max_epoch(*values):
    epochs = [e for e in (_epoch(v) for v in values) if e is not None]
    return max(epochs) if epochs else None


class QuarantineFold:
    """One consumer's pass over journal records (see the module docstring).

    ``held_ids`` are the reviewed weekly holds of a rederive scratch replay
    (never witnesses). ``on_record`` returns the gate for a Claude reading's
    automatic outcomes, else None."""

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
    def on_excluded(self, rec, *, segment=None, offset=None, family=None):
        """An excluded record: a trigger at its coordinate; its readings and
        effects widen the every-account envelope."""
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
            self.state.absorb_excluded_reading(reading)
            return
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if rec.get("t") == "evt":
            _absorb_event(self.state, Q.EXCLUDED_SCOPE, rec)
        elif rec.get("t") == "op" and payload.get("kind") == "weekly_credit_floor":
            _absorb_credit_op(self.state, Q.EXCLUDED_SCOPE, payload)

    def on_record(self, rec, *, segment=None, offset=None):
        """An admitted record."""
        if not isinstance(rec, dict):
            return None
        t = rec.get("t")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if t == "obs":
            reading = reading_from_record(rec, held_ids=self.held_ids)
            if reading is None:
                return None
            gate = self.state.on_reading(reading.account_key, reading)
            if (reading.account_key is not None
                    and reading.account_key in self.pending_deletion):
                gate = Q.GateDecision(False, "pending-revocation")
            if is_control_witness(rec, held_ids=self.held_ids):
                self.state.on_control_witness(
                    reading.account_key, observation_id=reading.observation_id,
                    captured_at_utc=reading.captured_at_utc)
            return gate
        if t == "op":
            kind = payload.get("kind")
            if kind == _lib_account_protocol.LEGACY_SPOOL_ACTIVITY_KIND:
                if _lib_account_protocol.is_stamped(rec):
                    self.state.on_trigger(
                        kind=TRIGGER_LEGACY_SPOOL,
                        reason=str(payload.get("reason") or "changed"),
                        segment=segment, offset=offset, at=rec.get("at"))
            elif kind == _lib_account_protocol.CUTOVER_OP_KIND:
                # Spec revision 5 (section 7, *Before the initial cutover*):
                # a cutover that could not complete its legacy spool
                # inventory records the uncertainty in itself, and is the
                # same unresolved-marker trigger an `inventory-incomplete`
                # op is, at the cutover's own position.
                if (_lib_account_protocol.is_stamped(rec)
                        and rec.get("id") == _lib_account_protocol.CUTOVER_OP_ID
                        and payload.get("legacySpoolInventoryFailure")
                        is not None):
                    self.state.on_trigger(
                        kind=TRIGGER_LEGACY_SPOOL,
                        reason=_lib_account_protocol.INVENTORY_INCOMPLETE_REASON,
                        segment=segment, offset=offset, at=rec.get("at"))
            elif kind == "weekly_credit_floor":
                # A manual `record-credit` stays available as the operator's
                # assertion, and revokes the account's certificates BEFORE its
                # dependency-changing effects run (this fold precedes the
                # hooks). Its synthetic snapshot is an event, never a witness.
                scope = _scope_of(rec, payload)
                if scope == Q.EXCLUDED_SCOPE:
                    self.state.revoke_all_accounting()
                    for account_key in sorted(self.state.accounts):
                        self.state.revoke_control(account_key)
                else:
                    self.state.revoke_accounting(scope)
                    self.state.revoke_control(scope)
                _absorb_credit_op(self.state, scope, payload)
            elif kind in _ADMIN_OP_KINDS:
                self.state.revoke_all_accounting()
            return None
        if t in _ADMIN_RECORD_TYPES:
            self.state.revoke_all_accounting()
            return None
        if t == "evt":
            scope = _scope_of(rec, payload)
            _absorb_event(self.state, scope, rec)
            if _is_deletion_outcome(payload):
                account = None if scope == Q.EXCLUDED_SCOPE else (
                    scope if payload.get("kind") != "weekly_credit_effects"
                    else None)
                self.state.on_deletion_outcome(account)
                if account is None:
                    self.pending_deletion.clear()
                else:
                    self.pending_deletion.discard(account)
        return None

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


def load_state(conn) -> Q.QuarantineState:
    """The persisted fold state. Missing tables fail closed (raise).

    The unresolved spool markers are reconstructed from the retained spool
    triggers alone, so a reader of the persisted index and a rebuild agree."""
    row = conn.execute(
        "SELECT MAX(seq) FROM outcome_quarantine_triggers").fetchone()
    latest = int(row[0]) if row and row[0] is not None else 0
    markers = [
        Q.spool_marker(Q.Trigger(seq, segment, offset, TRIGGER_LEGACY_SPOOL,
                                 reason, at))
        for seq, segment, offset, reason, at in conn.execute(
            "SELECT seq, segment, byte_offset, reason, at "
            "FROM outcome_quarantine_triggers WHERE kind = ? ORDER BY seq",
            (TRIGGER_LEGACY_SPOOL,))]
    envelopes = {}
    for scope, text in conn.execute(
            "SELECT scope, envelope_json FROM outcome_quarantine_envelope"):
        try:
            env = Q.Envelope.from_document(json.loads(text))
        except (TypeError, ValueError):
            env = None
        if env is None:
            raise sqlite3.DatabaseError(
                f"outcome quarantine envelope for {scope!r} is unverifiable")
        envelopes[scope] = env
    accounts = {}
    for row in conn.execute(
            "SELECT account_key, accounting_certificate_json, "
            "accounting_trigger_seq, control_certificate_json, "
            "control_trigger_seq FROM outcome_quarantine_certificates"):
        accounts[row[0]] = Q.account_from_row(row[1], row[2], row[3], row[4])
    return Q.QuarantineState(
        latest_trigger_seq=latest, envelopes=envelopes, accounts=accounts,
        non_inert_cross_period=_non_inert_cross_period(),
        spool_markers=markers)


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
    for scope in sorted(state.dirty_envelopes):
        conn.execute(
            "INSERT OR REPLACE INTO outcome_quarantine_envelope "
            "(scope, envelope_json) VALUES (?, ?)",
            (scope, json.dumps(state.envelopes[scope].to_document(),
                               sort_keys=True, separators=(",", ":"))),
        )
    for account_key in sorted(state.dirty_accounts):
        conn.execute(
            "INSERT OR REPLACE INTO outcome_quarantine_certificates "
            "(account_key, accounting_certificate_json, accounting_trigger_seq, "
            " control_certificate_json, control_trigger_seq) "
            "VALUES (?, ?, ?, ?, ?)",
            (account_key, *Q.certificate_row(state.accounts[account_key])),
        )
    state.new_triggers = []
    state.dirty_envelopes = set()
    state.dirty_accounts = set()


# --------------------------------------------------------------------------
# read side
# --------------------------------------------------------------------------

def quarantine_gate(ctx, account_key, reading=None) -> Q.GateDecision:
    """The gate for ``account_key``'s automatic outcomes on the record whose
    hooks are running. Without a fold (a direct caller) everything is allowed,
    exactly as before the quarantine existed."""
    gate = getattr(ctx, "outcome_gate", None) if ctx is not None else None
    if gate is None:
        return Q.GATE_NO_TRIGGER
    return gate


def account_status(conn, account_key: str) -> dict:
    """``{"accounting": bool, "control": bool}`` — whether each certificate is
    held — read from ``conn``'s current snapshot. Raises on a missing table."""
    state = load_state(conn)
    return {
        "triggered": state.has_trigger(),
        "accounting": not state.accounting_quarantined(account_key),
        "control": not state.control_quarantined(account_key),
        "control_certificate": (
            state.accounts[account_key].control
            if state.control_certified(account_key) else None),
    }


def control_recovered(status: dict, authority_commit) -> bool:
    """Whether control recovery is COMPLETE for one account (spec 5.6 item 7;
    fix-round-1 Q6): no trigger at all, or the account holds both
    certificates AND its reconciled control transition records the commit of
    the very observation that witnesses the control certificate. A crash
    between the observation append and the control commit therefore never
    reads as recovered. ``status`` is an `account_status` result."""
    if not status["triggered"]:
        return True
    if not (status["accounting"] and status["control"]):
        return False
    witness = status["control_certificate"]
    return (witness is not None and authority_commit is not None
            and authority_commit == witness.observation_id)


def _known_claude_accounts(conn) -> set:
    """Every Claude account the retained registry or usage history knows,
    including an existing `unattributed` usage bucket."""
    known = {row[0] for row in conn.execute(
        "SELECT account_key FROM accounts WHERE provider = 'claude'")}
    known.update(row[0] for row in conn.execute(
        "SELECT DISTINCT account_key FROM weekly_usage_snapshots "
        "WHERE account_key IS NOT NULL"))
    return known


def report(conn, *, authority_commits=None) -> dict:
    """The per-account quarantine report `doctor` renders: for every known
    account that lacks a certificate, what is paused and since when (the
    journal time of the first trigger after its last certificate).

    ``authority_commits`` maps an account to the `authorityCommit` its
    reconciled statusline control transition records (absent: none). When
    given, control counts as recovered only through `control_recovered`, the
    same predicate drop eligibility uses.

    While a spool marker is unresolved (spec revision 3) every known account
    is reported with ``unresolvedSpoolMarker`` and the report carries the
    markers (journal coordinate and `at`) as ``unresolvedSpoolMarkers``; both
    keys are present only then."""
    state = load_state(conn)
    if not state.has_trigger():
        return {"triggered": False, "accounts": []}
    known = set(state.accounts)
    known.update(scope for scope in state.envelopes
                 if scope != Q.EXCLUDED_SCOPE)
    # A trigger quarantines EVERY account, and historical unstamped readings
    # and events widen only the shared excluded scope: an account known only
    # from history has no recovery record or envelope of its own (#825/#832
    # fix round D, CD2-002).
    known.update(_known_claude_accounts(conn))
    rows = []

    def _since(seq):
        """The first trigger after the account's last certificate; for a
        certificate revoked without a new trigger (backdated input, a manual
        credit, an administrative record), the latest trigger."""
        found = conn.execute(
            "SELECT at FROM outcome_quarantine_triggers WHERE seq > ? "
            "ORDER BY seq LIMIT 1", (int(seq or 0),)).fetchone()
        if found is None:
            found = conn.execute(
                "SELECT at FROM outcome_quarantine_triggers "
                "ORDER BY seq DESC LIMIT 1").fetchone()
        return None if found is None else found[0]

    # Spec revision 5 (section 8): a marker only incomplete inventories left
    # is worded as such; any marker an observed legacy change left keeps the
    # revision 3 wording, which names the observed cause.
    # Fix round K12 (K11R-4): the anchoring op that ends an uncertain
    # cutover's episode is part of that same inventory uncertainty.
    cause = (INVENTORY_INCOMPLETE_CAUSE
             if state.spool_markers and all(
                 m.get("reason") in _lib_account_protocol.INVENTORY_REASONS
                 for m in state.spool_markers)
             else LEGACY_ACTIVITY_CAUSE)
    for account_key in sorted(known):
        rec = state.accounts.get(account_key) or Q.AccountRecovery()
        accounting = state.accounting_certified(account_key)
        control = state.control_certified(account_key)
        if control and authority_commits is not None:
            control = (authority_commits.get(account_key)
                       == rec.control.observation_id)
        if accounting and control:
            continue
        row = {
            "account": account_key,
            "accountingPaused": not accounting,
            "controlPaused": not control,
            "accountingSince": None if accounting else _since(
                rec.accounting_trigger_seq),
            "controlSince": None if control else _since(
                rec.control_trigger_seq),
        }
        if state.has_unresolved_spool_marker():
            row["unresolvedSpoolMarker"] = True
            row["spoolMarkerCause"] = cause
        rows.append(row)
    result = {"triggered": True, "accounts": rows,
              "latestTriggerSeq": state.latest_trigger_seq}
    if state.has_unresolved_spool_marker():
        result["unresolvedSpoolMarkers"] = [
            {"segment": m["segment"], "offset": m["offset"], "at": m["at"],
             "reason": m.get("reason")}
            for m in state.spool_markers]
    return result
