"""Pure outcome quarantine and certified account recovery (#825/#832, spec 5.6).

After the account-bound cutover, an older cctally process can still write.
Its records are excluded from current accounting (spec 5.3), but excluding a
reading changes what the detectors see: even an excluded reading that would
not itself have produced an outcome can change a LATER detection. So every
excluded record and every legacy spool change is an uncertainty TRIGGER that
quarantines every Claude account: automatic in-place credits, five-hour
credits, provider-moved week-end transitions and drop consensus are withheld
until the account holds a recovery certificate.

This module is the pure half:

* `Reading` — one Claude rate-limit observation as the detectors see it.
* `Envelope` — monotone running bounds over the WHOLE journal prefix (never
  truncated at a trigger or a revocation): the greatest week-start date key,
  five-hour key, raw five-hour reset, weekly end, stored capture ordering and
  effective instant any reading or effect in that prefix could have stored.
* `certify` — the seven clauses of spec 5.6 item 4, every one evaluated, so a
  refusal names ALL failed clauses.
* `Certificate` / `continuing_validity` — a certificate is revocable: backward
  input, a clock or ordering regression, or a fallback read that reaches the
  uncertain prefix through a cross-period access not registered inert revokes
  it before any automatic outcome.
* `QuarantineState` — the deterministic fold over journal order that live
  ingest, `db rebuild --db stats` and the `db rederive` scratch replay all run,
  so triggers, envelopes and certificates are a function of the journal alone.
* The unresolved spool marker (spec revision 3, section 5.6 item 1(b); Codex
  design-fix-round-4 Q6). A `legacy_spool_activity` op records only digests,
  never a legacy value, so the candidate it stands for contributes no bound to
  any envelope, and a fresh-looking reading cannot certify against the bounds
  the envelope does hold. Every such trigger therefore persists a marker keyed
  by its journal coordinate and `at`, and while any marker exists no account —
  one first seen later included — holds an accounting certificate. Nothing
  clears a marker in this outcome (automatic recovery is follow-up #948);
  the control certificate still reconciles on its own.

Deliberately absent (the design consultation rejected each): a taint map, a
timer, and an irrevocable clean flag. Diagnostic quiet time never decides
recovery.

Pure: no I/O, no clock.
"""
from __future__ import annotations

import dataclasses
import json

#: `bin/_cctally_record.py` `_derive_5h_window_key`: the nearest
#: `five_hour_blocks` raw reset within 1,800 s anchors the window.
TOLERANCE_BLOCK_SEARCH_S = 1800
#: `bin/_lib_five_hour.py` `_FIVE_HOUR_JITTER_FLOOR_SECONDS`: the rolling
#: canonicalizer's jitter tolerance.
TOLERANCE_CANONICAL_S = 600

#: The envelope scope whose bounds apply to EVERY account: excluded readings
#: and effects under every reference interpretation (absent, or any Claude
#: account including `unattributed` and accounts first seen later), plus
#: admitted historical readings that carry no account at all.
EXCLUDED_SCOPE = "excluded"

INELIGIBLE_REASONS = (
    "projection-fill", "retained-low", "reviewed-hold", "manual-synthetic",
)
#: A witness that is not eligible at all (spec 5.6 item 4 preamble).
INELIGIBLE_SINGLE_AXIS = "single-axis"
INELIGIBLE_UNBOUND = "unbound"

REVOCATION_REASONS = (
    "backward-key", "clock-regression", "order-regression",
    "non-inert-fallback",
)

CLAUSES = (1, 2, 3, 4, 5, 6, 7)

#: The trigger kind of a current `legacy_spool_activity` op. Each one leaves
#: an unresolved spool marker (spec revision 3, section 5.6 item 1(b)).
TRIGGER_KIND_LEGACY_SPOOL = "legacy-spool-activity"
#: The gate reason while a spool marker is unresolved.
GATE_REASON_SPOOL_MARKER = "unresolved-spool-marker"


def _max(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


@dataclasses.dataclass(frozen=True)
class Reading:
    """One observation as the detector sees it.

    ``captured_at_utc`` is the value the snapshot row stores in
    ``captured_at_utc`` (the payload's capture stamp verbatim), because the
    cross-period reads order on that TEXT column lexically.
    ``detection_epoch`` is the record's ``at`` (the detection clock).
    ``five_hour_key`` is the planned account-local key a current producer
    journaled, else the pure canonical floor of the raw reset."""

    observation_id: str
    account_key: "str | None"
    captured_at_utc: str
    capture_epoch: int
    detection_epoch: int
    week_start_date: "str | None"
    week_start_at_epoch: "int | None"
    weekly_end_epoch: "int | None"
    five_hour_key: "int | None"
    raw_five_hour_reset: "int | None"
    two_axis: bool
    ineligible_reason: "str | None" = None
    stamped: bool = True


@dataclasses.dataclass(frozen=True)
class Envelope:
    """Monotone running bounds over the WHOLE prefix (never truncated)."""

    max_week_start_date: "str | None" = None
    max_five_hour_key: "int | None" = None
    max_raw_five_hour_reset: "int | None" = None
    max_weekly_end_epoch: "int | None" = None
    #: Lexical, as stored in `captured_at_utc`.
    max_capture_at_utc: "str | None" = None
    #: Reset/credit effective instants AND every capture and detection
    #: instant: an automatic credit's effective instant is at or before its
    #: arming tick, so absorbing every reading's instants bounds them
    #: conservatively, including effects no surviving row records.
    max_effective_instant_epoch: "int | None" = None

    def absorb_reading(self, r: Reading) -> "Envelope":
        return Envelope(
            max_week_start_date=_max(self.max_week_start_date, r.week_start_date),
            max_five_hour_key=_max(self.max_five_hour_key, r.five_hour_key),
            max_raw_five_hour_reset=_max(
                self.max_raw_five_hour_reset, r.raw_five_hour_reset),
            max_weekly_end_epoch=_max(self.max_weekly_end_epoch, r.weekly_end_epoch),
            max_capture_at_utc=_max(self.max_capture_at_utc, r.captured_at_utc),
            max_effective_instant_epoch=_max(
                self.max_effective_instant_epoch,
                _max(r.capture_epoch, r.detection_epoch)),
        )

    def absorb_effect(self, *, effective_epoch: "int | None",
                      week_start_date: "str | None" = None,
                      five_hour_key: "int | None" = None,
                      captured_at_utc: "str | None" = None,
                      weekly_end_epoch: "int | None" = None,
                      raw_five_hour_reset: "int | None" = None) -> "Envelope":
        """An effect any reference could hold: an event's effective instant, a
        manual floor's date key, a synthetic or exported snapshot's keys."""
        return Envelope(
            max_week_start_date=_max(self.max_week_start_date, week_start_date),
            max_five_hour_key=_max(self.max_five_hour_key, five_hour_key),
            max_raw_five_hour_reset=_max(
                self.max_raw_five_hour_reset, raw_five_hour_reset),
            max_weekly_end_epoch=_max(self.max_weekly_end_epoch, weekly_end_epoch),
            max_capture_at_utc=_max(self.max_capture_at_utc, captured_at_utc),
            max_effective_instant_epoch=_max(
                self.max_effective_instant_epoch, effective_epoch),
        )

    @staticmethod
    def merge(a: "Envelope", b: "Envelope") -> "Envelope":
        return Envelope(
            max_week_start_date=_max(a.max_week_start_date, b.max_week_start_date),
            max_five_hour_key=_max(a.max_five_hour_key, b.max_five_hour_key),
            max_raw_five_hour_reset=_max(
                a.max_raw_five_hour_reset, b.max_raw_five_hour_reset),
            max_weekly_end_epoch=_max(a.max_weekly_end_epoch, b.max_weekly_end_epoch),
            max_capture_at_utc=_max(a.max_capture_at_utc, b.max_capture_at_utc),
            max_effective_instant_epoch=_max(
                a.max_effective_instant_epoch, b.max_effective_instant_epoch),
        )

    def to_document(self) -> dict:
        return {
            "maxWeekStartDate": self.max_week_start_date,
            "maxFiveHourKey": self.max_five_hour_key,
            "maxRawFiveHourReset": self.max_raw_five_hour_reset,
            "maxWeeklyEndEpoch": self.max_weekly_end_epoch,
            "maxCaptureAtUtc": self.max_capture_at_utc,
            "maxEffectiveInstantEpoch": self.max_effective_instant_epoch,
        }

    @staticmethod
    def from_document(doc) -> "Envelope | None":
        """The envelope a document describes, or None when it is not a valid
        envelope document. An unverifiable envelope must never read as an
        empty (permissive) one, so callers treat None as quarantined."""
        if not isinstance(doc, dict):
            return None
        keys = {"maxWeekStartDate", "maxFiveHourKey", "maxRawFiveHourReset",
                "maxWeeklyEndEpoch", "maxCaptureAtUtc",
                "maxEffectiveInstantEpoch"}
        if set(doc) != keys:
            return None
        for key in ("maxWeekStartDate", "maxCaptureAtUtc"):
            if doc[key] is not None and not isinstance(doc[key], str):
                return None
        for key in ("maxFiveHourKey", "maxRawFiveHourReset",
                    "maxWeeklyEndEpoch", "maxEffectiveInstantEpoch"):
            if doc[key] is not None and type(doc[key]) is not int:
                return None
        return Envelope(
            max_week_start_date=doc["maxWeekStartDate"],
            max_five_hour_key=doc["maxFiveHourKey"],
            max_raw_five_hour_reset=doc["maxRawFiveHourReset"],
            max_weekly_end_epoch=doc["maxWeeklyEndEpoch"],
            max_capture_at_utc=doc["maxCaptureAtUtc"],
            max_effective_instant_epoch=doc["maxEffectiveInstantEpoch"],
        )


@dataclasses.dataclass(frozen=True)
class CertificateDecision:
    certified: bool
    #: Subset of 1..7, empty when certified or when the witness is ineligible.
    failed_clauses: "tuple[int, ...]"
    ineligible: "str | None" = None


def witness_ineligibility(witness: Reading) -> "str | None":
    """Why ``witness`` can never certify, or None (spec 5.6 item 4)."""
    if witness.ineligible_reason is not None:
        return witness.ineligible_reason
    if (not witness.stamped or witness.account_key is None):
        return INELIGIBLE_UNBOUND
    if (not witness.two_axis or witness.week_start_date is None
            or witness.five_hour_key is None
            or witness.raw_five_hour_reset is None
            or witness.week_start_at_epoch is None):
        return INELIGIBLE_SINGLE_AXIS
    return None


def _debounce_could_match(witness: Reading, envelope: Envelope) -> bool:
    """Clause 6, by the comparisons the detector performs.

    The weekly debounce state is ONE row per account; the detector treats it
    as armed for a reading iff the stored `week_start_date` equals the
    reading's and the stored end equals its end (`detect_reset_and_credit`).
    Every state the prefix could have armed carries a date key the prefix
    produced, so it is bounded by `max_week_start_date`; a witness date beyond
    that bound can match none of them, whatever its end. The five-hour
    confirmation state is keyed `(account, window key, source)`, so a witness
    key beyond `max_five_hour_key` reads no prefix row."""
    if (envelope.max_week_start_date is not None
            and witness.week_start_date <= envelope.max_week_start_date):
        return True
    if (envelope.max_five_hour_key is not None
            and witness.five_hour_key <= envelope.max_five_hour_key):
        return True
    return False


def certify(witness: Reading, envelope: Envelope, *,
            seen_identities=frozenset()) -> CertificateDecision:
    """Evaluate every clause of spec 5.6 item 4 against ``envelope`` (the
    merge of the account's and the excluded scope's bounds) and return ALL
    failed clauses, never just the first."""
    ineligible = witness_ineligibility(witness)
    if ineligible is not None:
        return CertificateDecision(False, (), ineligible)
    failed = []
    env = envelope
    # 1. Period-keyed freshness: the date key and the five-hour key lie
    #    beyond every possible prefix key. Comparing normalized ends is not
    #    enough, because distinct ends share one date key.
    if ((env.max_week_start_date is not None
         and witness.week_start_date <= env.max_week_start_date)
            or (env.max_five_hour_key is not None
                and witness.five_hour_key <= env.max_five_hour_key)):
        failed.append(1)
    # 2. Anchor selection: no prefix raw reset inside either tolerance of the
    #    witness's, so every possible prefix anchor yields the witness's own
    #    fresh key (conservatively the SUM of both tolerances).
    if (env.max_raw_five_hour_reset is not None
            and witness.raw_five_hour_reset - env.max_raw_five_hour_reset
            <= TOLERANCE_BLOCK_SEARCH_S + TOLERANCE_CANONICAL_S):
        failed.append(2)
    # 3. Range-keyed reads: no prefix effective instant inside the witness's
    #    weekly interval (the sufficient check: its start exceeds them all).
    if (env.max_effective_instant_epoch is not None
            and witness.week_start_at_epoch <= env.max_effective_instant_epoch):
        failed.append(3)
    # 4. Cross-period ordering under the actual SQL ordering
    #    (`captured_at_utc DESC, id DESC`, lexical TEXT comparison).
    if (env.max_capture_at_utc is not None
            and witness.captured_at_utc <= env.max_capture_at_utc):
        failed.append(4)
    # 5. Detection clock later than every predecessor's weekly end and raw
    #    five-hour reset — separate from capture ordering.
    latest_end = _max(env.max_weekly_end_epoch, env.max_raw_five_hour_reset)
    if latest_end is not None and witness.detection_epoch <= latest_end:
        failed.append(5)
    # 6. Singletons: the weekly debounce row and the five-hour confirmation
    #    state cannot match the witness.
    if _debounce_could_match(witness, env):
        failed.append(6)
    # 7. Logical identity: the witness is not a replay of a seen observation.
    if witness.observation_id in seen_identities:
        failed.append(7)
    return CertificateDecision(not failed, tuple(failed), None)


@dataclasses.dataclass(frozen=True)
class Certificate:
    witness_observation_id: str
    witness_week_start_date: str
    witness_five_hour_key: int
    witness_captured_at_utc: str
    last_capture_at_utc: str
    last_detection_epoch: int

    @staticmethod
    def from_witness(r: Reading) -> "Certificate":
        return Certificate(
            witness_observation_id=r.observation_id,
            witness_week_start_date=r.week_start_date,
            witness_five_hour_key=r.five_hour_key,
            witness_captured_at_utc=r.captured_at_utc,
            last_capture_at_utc=r.captured_at_utc,
            last_detection_epoch=r.detection_epoch,
        )

    def advance(self, r: Reading) -> "Certificate":
        return dataclasses.replace(
            self,
            last_capture_at_utc=_max(self.last_capture_at_utc, r.captured_at_utc),
            last_detection_epoch=_max(self.last_detection_epoch, r.detection_epoch),
        )

    def to_document(self) -> dict:
        return {
            "witnessObservationId": self.witness_observation_id,
            "witnessWeekStartDate": self.witness_week_start_date,
            "witnessFiveHourKey": self.witness_five_hour_key,
            "witnessCapturedAtUtc": self.witness_captured_at_utc,
            "lastCaptureAtUtc": self.last_capture_at_utc,
            "lastDetectionEpoch": self.last_detection_epoch,
        }

    @staticmethod
    def from_document(doc) -> "Certificate | None":
        if not isinstance(doc, dict) or set(doc) != {
                "witnessObservationId", "witnessWeekStartDate",
                "witnessFiveHourKey", "witnessCapturedAtUtc",
                "lastCaptureAtUtc", "lastDetectionEpoch"}:
            return None
        for key in ("witnessObservationId", "witnessWeekStartDate",
                    "witnessCapturedAtUtc", "lastCaptureAtUtc"):
            if not isinstance(doc[key], str) or not doc[key]:
                return None
        for key in ("witnessFiveHourKey", "lastDetectionEpoch"):
            if type(doc[key]) is not int:
                return None
        return Certificate(
            witness_observation_id=doc["witnessObservationId"],
            witness_week_start_date=doc["witnessWeekStartDate"],
            witness_five_hour_key=doc["witnessFiveHourKey"],
            witness_captured_at_utc=doc["witnessCapturedAtUtc"],
            last_capture_at_utc=doc["lastCaptureAtUtc"],
            last_detection_epoch=doc["lastDetectionEpoch"],
        )


def continuing_validity(cert: Certificate, r: Reading, *,
                        fallback_rows_captured_at=()) -> "str | None":
    """None while ``r`` stays inside the certified forward domain, else the
    revocation reason (spec 5.6 item 5).

    ``fallback_rows_captured_at`` are the capture instants of the rows the
    decision's NON-INERT cross-period reads returned (the access registry
    decides which reads are inert); a row from before the witness means the
    read fell back into the uncertain prefix."""
    if r.week_start_date is None or r.week_start_date < cert.witness_week_start_date:
        return "backward-key"
    if r.five_hour_key is not None and r.five_hour_key < cert.witness_five_hour_key:
        return "backward-key"
    if r.detection_epoch < cert.last_detection_epoch:
        return "clock-regression"
    if r.captured_at_utc < cert.last_capture_at_utc:
        return "order-regression"
    for captured in fallback_rows_captured_at:
        if captured is not None and captured < cert.witness_captured_at_utc:
            return "non-inert-fallback"
    return None


@dataclasses.dataclass(frozen=True)
class ControlCertificate:
    """A bound two-axis authoritative OAuth publication for one account
    (spec 5.6 item 7): the journal observation that witnesses it."""

    observation_id: str
    captured_at_utc: str

    def to_document(self) -> dict:
        return {"observationId": self.observation_id,
                "capturedAtUtc": self.captured_at_utc}

    @staticmethod
    def from_document(doc) -> "ControlCertificate | None":
        if (not isinstance(doc, dict)
                or set(doc) != {"observationId", "capturedAtUtc"}
                or not isinstance(doc["observationId"], str)
                or not isinstance(doc["capturedAtUtc"], str)):
            return None
        return ControlCertificate(doc["observationId"], doc["capturedAtUtc"])


@dataclasses.dataclass
class AccountRecovery:
    """One account's certificates and the trigger sequence each was granted
    against. A certificate granted against an older sequence is void."""

    accounting: "Certificate | None" = None
    accounting_trigger_seq: "int | None" = None
    control: "ControlCertificate | None" = None
    control_trigger_seq: "int | None" = None


@dataclasses.dataclass(frozen=True)
class Trigger:
    seq: int
    segment: "str | None"
    offset: "int | None"
    kind: str      # "excluded" | "legacy-spool-activity"
    reason: str    # the excluded family, or the activity reason
    at: "str | None"


def spool_marker(trigger: Trigger) -> dict:
    """The unresolved structural-envelope marker a spool trigger leaves: its
    journal coordinate and `at` (spec revision 3), and its reason — spec
    revision 5 words the marker of an incomplete inventory differently."""
    return {"seq": trigger.seq, "segment": trigger.segment,
            "offset": trigger.offset, "at": trigger.at,
            "reason": trigger.reason}


@dataclasses.dataclass(frozen=True)
class GateDecision:
    """Whether automatic outcomes may be minted for one reading."""

    allowed: bool
    reason: str


GATE_NO_TRIGGER = GateDecision(True, "no-trigger")
GATE_CERTIFIED = GateDecision(True, "certified")


class QuarantineState:
    """The deterministic quarantine fold over journal order.

    Every consumer — live ingest, rebuild, the rederive scratch replay — feeds
    the same events in journal order, so the persisted state is a function of
    the journal alone. Methods mutate in place and record which rows changed
    so a caller persists only those."""

    def __init__(self, *, latest_trigger_seq: int = 0, envelopes=None,
                 accounts=None, non_inert_cross_period: bool = False,
                 spool_markers=()):
        self.latest_trigger_seq = int(latest_trigger_seq)
        self.envelopes: dict = dict(envelopes or {})
        self.accounts: dict = dict(accounts or {})
        self.new_triggers: list = []
        self.dirty_envelopes: set = set()
        self.dirty_accounts: set = set()
        #: Every spool trigger's unresolved marker, in journal order. None is
        #: ever removed: a later trigger adds a generation and keeps the
        #: earlier uncertainty (spec revision 3).
        self.spool_markers: list = [dict(m) for m in spool_markers]
        if self.spool_markers:
            # A certificate persisted while a marker exists was issued
            # without the marker check (the rule before spec revision 3), so
            # it is invalidated here, never honored or advanced; a rebuild
            # likewise grants none while the retained triggers hold a marker.
            self.revoke_all_accounting()
        #: Session-local seen observation identities per scope (clause 7).
        #: Not persisted: a replayed identity always fails clause 1 as well,
        #: because its own first copy is inside the envelope.
        self.seen: dict = {}
        #: True when some registered cross-period access has no inert rule;
        #: then a deletion outcome revokes the account at its journal position.
        self.non_inert_cross_period = bool(non_inert_cross_period)

    # -- queries ---------------------------------------------------------
    def has_trigger(self) -> bool:
        return self.latest_trigger_seq > 0

    def has_unresolved_spool_marker(self) -> bool:
        return bool(self.spool_markers)

    def accounting_certified(self, account_key: str) -> bool:
        """Whether ``account_key`` holds a current accounting certificate.
        Never while a spool marker is unresolved, whatever a persisted row
        says (spec revision 3)."""
        if self.spool_markers:
            return False
        rec = self.accounts.get(account_key)
        return (rec is not None and rec.accounting is not None
                and rec.accounting_trigger_seq == self.latest_trigger_seq)

    def control_certified(self, account_key: str) -> bool:
        rec = self.accounts.get(account_key)
        return (rec is not None and rec.control is not None
                and rec.control_trigger_seq == self.latest_trigger_seq)

    def accounting_quarantined(self, account_key: str) -> bool:
        return self.has_trigger() and not self.accounting_certified(account_key)

    def control_quarantined(self, account_key: str) -> bool:
        return self.has_trigger() and not self.control_certified(account_key)

    def envelope(self, scope: str) -> Envelope:
        return self.envelopes.get(scope) or Envelope()

    # -- transitions -----------------------------------------------------
    def _set_envelope(self, scope: str, value: Envelope) -> None:
        if self.envelopes.get(scope) != value:
            self.envelopes[scope] = value
            self.dirty_envelopes.add(scope)

    def _account(self, account_key: str) -> AccountRecovery:
        rec = self.accounts.get(account_key)
        if rec is None:
            rec = AccountRecovery()
            self.accounts[account_key] = rec
        return rec

    def on_trigger(self, *, kind: str, reason: str, segment=None, offset=None,
                   at=None) -> Trigger:
        """A trigger quarantines EVERY account and voids every certificate
        (its sequence moves past them). The account a record names never
        narrows it."""
        self.latest_trigger_seq += 1
        trig = Trigger(self.latest_trigger_seq, segment, offset, kind, reason, at)
        self.new_triggers.append(trig)
        if kind == TRIGGER_KIND_LEGACY_SPOOL:
            self.spool_markers.append(spool_marker(trig))
        return trig

    def absorb_excluded_reading(self, r: Reading) -> None:
        self._set_envelope(
            EXCLUDED_SCOPE, self.envelope(EXCLUDED_SCOPE).absorb_reading(r))
        self.seen.setdefault(EXCLUDED_SCOPE, set()).add(r.observation_id)

    def absorb_effect(self, scope: str, **effect) -> None:
        self._set_envelope(scope, self.envelope(scope).absorb_effect(**effect))

    def revoke_accounting(self, account_key: str) -> None:
        """Drop the accounting certificate. The sequence it was granted
        against is kept, so `doctor` can say since when the account has been
        paused; without a certificate it never certifies anything."""
        rec = self.accounts.get(account_key)
        if rec is not None and rec.accounting is not None:
            rec.accounting = None
            self.dirty_accounts.add(account_key)

    def revoke_control(self, account_key: str) -> None:
        rec = self.accounts.get(account_key)
        if rec is not None and rec.control is not None:
            rec.control = None
            self.dirty_accounts.add(account_key)

    def revoke_all_accounting(self) -> None:
        for account_key in sorted(self.accounts):
            self.revoke_accounting(account_key)

    def on_reading(self, account_key: "str | None", r: Reading, *,
                   fallback_rows_captured_at=()) -> GateDecision:
        """Fold one admitted reading. Certification is evaluated BEFORE the
        reading is absorbed, against the whole prefix; continuing validity is
        checked before any outcome; the reading is then absorbed into its
        account's envelope. Returns the gate for the reading's outcomes."""
        if account_key is None:
            # An admitted historical reading with no account: its bounds
            # apply to every account.
            self._set_envelope(
                EXCLUDED_SCOPE, self.envelope(EXCLUDED_SCOPE).absorb_reading(r))
            self.seen.setdefault(EXCLUDED_SCOPE, set()).add(r.observation_id)
            return GATE_NO_TRIGGER if not self.has_trigger() else GateDecision(
                False, "quarantined")
        gate = GATE_NO_TRIGGER
        if self.has_trigger():
            if self.accounting_certified(account_key):
                rec = self.accounts[account_key]
                reason = continuing_validity(
                    rec.accounting, r,
                    fallback_rows_captured_at=fallback_rows_captured_at)
                if reason is not None:
                    self.revoke_accounting(account_key)
                    gate = GateDecision(False, f"revoked:{reason}")
                else:
                    advanced = rec.accounting.advance(r)
                    if advanced != rec.accounting:
                        rec.accounting = advanced
                        self.dirty_accounts.add(account_key)
                    gate = GATE_CERTIFIED
            elif self.spool_markers:
                # The omitted spool contributor bounds nothing, so no reading
                # certifies against the envelope while a marker is unresolved.
                gate = GateDecision(False, GATE_REASON_SPOOL_MARKER)
            else:
                merged = Envelope.merge(self.envelope(account_key),
                                        self.envelope(EXCLUDED_SCOPE))
                seen = (self.seen.get(account_key, set())
                        | self.seen.get(EXCLUDED_SCOPE, set()))
                decision = certify(r, merged, seen_identities=seen)
                if decision.certified:
                    rec = self._account(account_key)
                    rec.accounting = Certificate.from_witness(r)
                    rec.accounting_trigger_seq = self.latest_trigger_seq
                    self.dirty_accounts.add(account_key)
                    gate = GateDecision(True, "witness")
                else:
                    gate = GateDecision(False, "quarantined")
        self._set_envelope(account_key, self.envelope(account_key).absorb_reading(r))
        self.seen.setdefault(account_key, set()).add(r.observation_id)
        return gate

    def on_control_witness(self, account_key: str, *, observation_id: str,
                           captured_at_utc: str) -> None:
        """A bound two-axis authoritative OAuth publication for one account,
        positioned after the latest trigger. Its control transition must ALSO
        record the commit before drop consensus resumes (checked by the
        statusline reducer)."""
        if not self.has_trigger():
            return
        rec = self._account(account_key)
        cert = ControlCertificate(observation_id, captured_at_utc)
        if rec.control != cert or rec.control_trigger_seq != self.latest_trigger_seq:
            rec.control = cert
            rec.control_trigger_seq = self.latest_trigger_seq
            self.dirty_accounts.add(account_key)

    def on_deletion_outcome(self, account_key: "str | None") -> None:
        """A journaled automatic outcome that deleted snapshot rows. While
        every registered cross-period fallback read is inert this revokes
        nothing (spec 5.6 item 5: the certificate survives where the fallback
        read is registered inert); otherwise the affected account — every
        account when the event names none — is revoked at the event's journal
        position."""
        if not self.non_inert_cross_period:
            return
        if account_key is None:
            self.revoke_all_accounting()
        else:
            self.revoke_accounting(account_key)


def certificate_row(rec: AccountRecovery) -> tuple:
    """The persisted columns of one account's recovery state."""
    return (
        None if rec.accounting is None else json.dumps(
            rec.accounting.to_document(), sort_keys=True, separators=(",", ":")),
        rec.accounting_trigger_seq,
        None if rec.control is None else json.dumps(
            rec.control.to_document(), sort_keys=True, separators=(",", ":")),
        rec.control_trigger_seq,
    )


def account_from_row(accounting_json, accounting_seq, control_json,
                     control_seq) -> AccountRecovery:
    """The recovery state a persisted row describes. Missing or unverifiable
    certificate evidence reads as NO certificate (quarantined)."""
    def _load(text, loader):
        if not text:
            return None
        try:
            return loader(json.loads(text))
        except (TypeError, ValueError):
            return None

    accounting = _load(accounting_json, Certificate.from_document)
    control = _load(control_json, ControlCertificate.from_document)
    return AccountRecovery(
        accounting=accounting,
        accounting_trigger_seq=(
            accounting_seq if type(accounting_seq) is int else None),
        control=control,
        control_trigger_seq=(
            control_seq if type(control_seq) is int else None),
    )
