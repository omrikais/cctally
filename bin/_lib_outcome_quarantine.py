"""Pure outcome quarantine and per-axis automatic recovery (#825/#832 spec
5.6; #948).

After the account-bound cutover, an older cctally process can still write.
Its records are excluded from current accounting, but excluding a reading
changes what the detectors see, and an older process's status-line render can
drive a publication nobody journals. So every excluded record and every legacy
spool change is an uncertainty TRIGGER that pauses automatic in-place credits,
early resets, five-hour credits and status-line drop publications for every
Claude account.

#948 ends that pause on its own, per account and per axis (weekly
``sevenDay``, five-hour ``fiveHour``), through a journaled barrier (spec 6.1):

1. every incomplete-inventory EPISODE closed by a journaled complete scan;
2. the unknown candidate COHORT's expiry, from retained clock instants;
3. a completed automatic OAuth reconciliation for the account after expiry
   and after the trigger, covering the axis (`outcome_authority_completion`);
4. a genuinely observed first entry into a fresh period of the axis — the
   WITNESS — checked against bounds over the prefix before it;
5. one `outcome_axis_release` record, appended immediately after the witness,
   that opens the axis's outcomes and its drop consensus together.

This module is the pure half: `Reading`, `Cohort`, the per-axis bounds
(`WeeklyBounds`, `FiveHourBounds`), the witness predicates and
`QuarantineState`, the deterministic fold over journal order that live
ingest, `db rebuild --db stats`, the `db rederive` scratch replay and the
derivation-completion scratch replay all run, so every recovery fact is a
function of the journal alone. Nothing here uses a TTL, quiet time, spool
absence or an OAuth reading alone as a release condition.

Pure: no I/O, no clock, no SQLite.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
from typing import Callable

WEEKLY = "sevenDay"
FIVE_HOUR = "fiveHour"
AXES = (WEEKLY, FIVE_HOUR)

#: `bin/_cctally_record.py` `_derive_5h_window_key`: the nearest
#: `five_hour_blocks` raw reset within 1,800 s anchors the window.
TOLERANCE_BLOCK_SEARCH_S = 1800
#: `bin/_lib_five_hour.py` `_FIVE_HOUR_JITTER_FLOOR_SECONDS`: the rolling
#: canonicalizer's jitter tolerance.
TOLERANCE_CANONICAL_S = 600
#: Anchor separation beyond both tolerances (spec 6.5, 6.6).
FIVE_HOUR_SEPARATION_S = TOLERANCE_BLOCK_SEARCH_S + TOLERANCE_CANONICAL_S

#: The clock model's receipt skew between a legacy write and its detection
#: (D4 (3)), and the candidate activity window of the reducer: a candidate
#: received at R is inactive at every evaluation from R + 90 s on.
RECEIPT_SKEW_S = 5
CANDIDATE_ACTIVE_S = 90
#: A cohort's bounds (spec 6.3).
COHORT_WEEKLY_OFFSET_S = 8 * 86400
COHORT_FIVE_HOUR_OFFSET_S = 6 * 3600

#: The bounds scope whose summaries apply to EVERY account: excluded records
#: under every reference interpretation, plus admitted historical records that
#: carry no account.
EXCLUDED_SCOPE = "excluded"
#: The bounds scope of every spool cohort (applies to every account).
COHORT_SCOPE = "cohorts"

STATE_PAUSED = "paused"
STATE_RELEASED = "recovered"

WAIT_INVENTORY = "inventory"
WAIT_EXPIRY = "expiry"
WAIT_USAGE_REFRESH = "usage-refresh"
WAIT_FRESH_PERIOD = "fresh-period"
WAITING_FOR = (WAIT_INVENTORY, WAIT_EXPIRY, WAIT_USAGE_REFRESH,
               WAIT_FRESH_PERIOD)

CAUSE_LEGACY_ACTIVITY = "legacy-spool-activity"
CAUSE_INVENTORY_INCOMPLETE = "inventory-incomplete"
CAUSE_EXCLUDED_WRITE = "excluded-write"

#: Why a released axis returned to paused (spec 6.9).
REVOKE_BACKWARD_KEY = "backward-key"
REVOKE_ORDER_REGRESSION = "order-regression"
REVOKE_NON_INERT_FALLBACK = "non-inert-fallback"
REVOKE_ADMINISTRATIVE = "administrative"
REVOKE_MANUAL_CREDIT = "manual-credit"
REVOCATION_REASONS = (REVOKE_BACKWARD_KEY, REVOKE_ORDER_REGRESSION,
                      REVOKE_NON_INERT_FALLBACK, REVOKE_ADMINISTRATIVE,
                      REVOKE_MANUAL_CREDIT)

#: Trigger kinds (the `outcome_quarantine_triggers.kind` column).
TRIGGER_KIND_EXCLUDED = "excluded"
TRIGGER_KIND_LEGACY_SPOOL = "legacy-spool-activity"


def _max(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _date_of(epoch: int) -> str:
    return _dt.datetime.fromtimestamp(int(epoch), tz=_dt.timezone.utc).date(
    ).isoformat()


@dataclasses.dataclass(frozen=True)
class Reading:
    """One Claude rate-limit observation as the detectors and the witness
    predicates see it.

    ``capture_text`` is the value the snapshot row stores in
    ``captured_at_utc`` (the payload's capture stamp verbatim), because the
    cross-period reads order on that TEXT column lexically; ``capture_epoch``
    its instant (the frozen evaluation instant of a reducer publication, the
    fetch instant of an OAuth reading). ``detection_epoch`` is the record's
    ``at`` (the detection clock). ``five_hour_key`` is the planned
    account-local key a current producer journaled, else the pure canonical
    floor of the raw reset.

    ``observed_axes`` are the axes the detector mask treats as a reading of
    their source; ``eligible_axes`` those `witness_eligible` accepts (spec
    6.6); ``projection_axes`` those a publication filled from the store."""

    observation_id: str
    account_key: "str | None"
    capture_text: str
    capture_epoch: int
    detection_epoch: int
    week_start_date: "str | None"
    week_start_at_epoch: "int | None"
    weekly_end_epoch: "int | None"
    five_hour_key: "int | None"
    raw_five_hour_reset: "int | None"
    observed_axes: frozenset = frozenset(AXES)
    eligible_axes: frozenset = frozenset()
    projection_axes: frozenset = frozenset()
    stamped: bool = True

    @property
    def captured_at_utc(self) -> str:
        return self.capture_text

    def bears(self, axis: str) -> bool:
        if axis == WEEKLY:
            return self.week_start_date is not None
        return self.five_hour_key is not None


@dataclasses.dataclass(frozen=True)
class Cohort:
    """The unknown candidates' bounds at uncertainty position P (spec 6.3)."""

    h: int
    r: int
    e: int
    u7_end: int
    u7_date: str
    u5_raw: int
    u5_key: int

    @staticmethod
    def from_h(h_epoch: int, *,
               normalize_week_end: "Callable[[int], int]") -> "Cohort":
        h = int(h_epoch)
        r = h + RECEIPT_SKEW_S
        e = r + CANDIDATE_ACTIVE_S
        u7_end = int(normalize_week_end(e + COHORT_WEEKLY_OFFSET_S))
        u5_raw = e + COHORT_FIVE_HOUR_OFFSET_S
        return Cohort(h=h, r=r, e=e, u7_end=u7_end,
                      u7_date=_date_of(u7_end - 7 * 86400),
                      u5_raw=u5_raw, u5_key=u5_raw + TOLERANCE_BLOCK_SEARCH_S)

    def to_document(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_document(doc) -> "Cohort | None":
        if not isinstance(doc, dict) or set(doc) != {
                "h", "r", "e", "u7_end", "u7_date", "u5_raw", "u5_key"}:
            return None
        for key in ("h", "r", "e", "u7_end", "u5_raw", "u5_key"):
            if type(doc[key]) is not int:
                return None
        if not isinstance(doc["u7_date"], str):
            return None
        return Cohort(**doc)


def _check_doc(doc, ints, strs):
    if not isinstance(doc, dict) or set(doc) != set(ints) | set(strs):
        return False
    for key in ints:
        if doc[key] is not None and type(doc[key]) is not int:
            return False
    for key in strs:
        if doc[key] is not None and not isinstance(doc[key], str):
            return False
    return True


@dataclasses.dataclass(frozen=True)
class WeeklyBounds:
    """The weekly summaries of one scope over the WHOLE retained prefix
    (spec 6.5): structural ``s7_date``/``s7_end``, capture order
    ``k7``/``t7``, the closed effect bound ``c7`` and the exclusive effect
    bound ``x7`` (strict-before: a witness's week start may EQUAL it)."""

    s7_date: "str | None" = None
    s7_end: "int | None" = None
    k7: "int | None" = None
    t7: "str | None" = None
    c7: "int | None" = None
    x7: "int | None" = None

    def absorb_reading(self, r: Reading, *, possible_effect: bool) -> "WeeklyBounds":
        if r.week_start_date is None:
            return self
        return WeeklyBounds(
            s7_date=_max(self.s7_date, r.week_start_date),
            s7_end=_max(self.s7_end, r.weekly_end_epoch),
            k7=_max(self.k7, r.capture_epoch),
            t7=_max(self.t7, r.capture_text),
            c7=_max(self.c7, r.capture_epoch) if possible_effect else self.c7,
            x7=self.x7,
        )

    def absorb_effect(self, *, effective_epoch=None, week_start_date=None,
                      week_end_epoch=None, capture_epoch=None,
                      capture_text=None, exclusive_end=None) -> "WeeklyBounds":
        return WeeklyBounds(
            s7_date=_max(self.s7_date, week_start_date),
            s7_end=_max(self.s7_end, week_end_epoch),
            k7=_max(self.k7, capture_epoch),
            t7=_max(self.t7, capture_text),
            c7=_max(self.c7, effective_epoch),
            x7=_max(self.x7, exclusive_end),
        )

    def absorb_cohort(self, c: Cohort) -> "WeeklyBounds":
        return WeeklyBounds(
            s7_date=_max(self.s7_date, c.u7_date),
            s7_end=_max(self.s7_end, c.u7_end),
            k7=self.k7, t7=self.t7,
            c7=_max(self.c7, c.e),
            x7=_max(self.x7, c.u7_end),
        )

    @staticmethod
    def merge(*items: "WeeklyBounds") -> "WeeklyBounds":
        out = WeeklyBounds()
        for b in items:
            out = WeeklyBounds(
                s7_date=_max(out.s7_date, b.s7_date),
                s7_end=_max(out.s7_end, b.s7_end),
                k7=_max(out.k7, b.k7), t7=_max(out.t7, b.t7),
                c7=_max(out.c7, b.c7), x7=_max(out.x7, b.x7))
        return out

    def to_document(self) -> dict:
        return {"s7Date": self.s7_date, "s7End": self.s7_end, "k7": self.k7,
                "t7": self.t7, "c7": self.c7, "x7": self.x7}

    @staticmethod
    def from_document(doc) -> "WeeklyBounds | None":
        if not _check_doc(doc, ("s7End", "k7", "c7", "x7"), ("s7Date", "t7")):
            return None
        return WeeklyBounds(s7_date=doc["s7Date"], s7_end=doc["s7End"],
                            k7=doc["k7"], t7=doc["t7"], c7=doc["c7"],
                            x7=doc["x7"])


@dataclasses.dataclass(frozen=True)
class FiveHourBounds:
    """The five-hour summaries of one scope (spec 6.5): the maximum canonical
    key ``k5``; ``raw_upper5`` = max(every raw reset, every cohort U5.raw,
    every key + 2,400 s); capture order ``k5c``/``t5``; and ``detect_floor``,
    the maximum of every cohort's U5.raw and every excluded record's raw
    reset, which a witness's detection must exceed. Effects are key-scoped
    (old-key confirmation state is inert by key mismatch), so there is no
    effect bound."""

    k5: "int | None" = None
    raw_upper5: "int | None" = None
    k5c: "int | None" = None
    t5: "str | None" = None
    detect_floor: "int | None" = None

    def absorb_reading(self, r: Reading, *, excluded: bool = False) -> "FiveHourBounds":
        if r.five_hour_key is None:
            return self
        upper = _max(r.raw_five_hour_reset, r.five_hour_key + FIVE_HOUR_SEPARATION_S)
        return FiveHourBounds(
            k5=_max(self.k5, r.five_hour_key),
            raw_upper5=_max(self.raw_upper5, upper),
            k5c=_max(self.k5c, r.capture_epoch),
            t5=_max(self.t5, r.capture_text),
            detect_floor=(_max(self.detect_floor, r.raw_five_hour_reset)
                          if excluded else self.detect_floor),
        )

    def absorb_effect(self, *, five_hour_key=None, raw_reset=None,
                      capture_epoch=None, capture_text=None) -> "FiveHourBounds":
        upper = raw_reset
        if five_hour_key is not None:
            upper = _max(upper, five_hour_key + FIVE_HOUR_SEPARATION_S)
        return FiveHourBounds(
            k5=_max(self.k5, five_hour_key),
            raw_upper5=_max(self.raw_upper5, upper),
            k5c=_max(self.k5c, capture_epoch),
            t5=_max(self.t5, capture_text),
            detect_floor=self.detect_floor,
        )

    def absorb_cohort(self, c: Cohort) -> "FiveHourBounds":
        return FiveHourBounds(
            k5=_max(self.k5, c.u5_key),
            raw_upper5=_max(self.raw_upper5,
                            max(c.u5_raw, c.u5_key + FIVE_HOUR_SEPARATION_S)),
            k5c=self.k5c, t5=self.t5,
            detect_floor=_max(self.detect_floor, c.u5_raw),
        )

    @staticmethod
    def merge(*items: "FiveHourBounds") -> "FiveHourBounds":
        out = FiveHourBounds()
        for b in items:
            out = FiveHourBounds(
                k5=_max(out.k5, b.k5),
                raw_upper5=_max(out.raw_upper5, b.raw_upper5),
                k5c=_max(out.k5c, b.k5c), t5=_max(out.t5, b.t5),
                detect_floor=_max(out.detect_floor, b.detect_floor))
        return out

    def to_document(self) -> dict:
        return {"k5": self.k5, "rawUpper5": self.raw_upper5, "k5c": self.k5c,
                "t5": self.t5, "detectFloor": self.detect_floor}

    @staticmethod
    def from_document(doc) -> "FiveHourBounds | None":
        if not _check_doc(doc, ("k5", "rawUpper5", "k5c", "detectFloor"),
                          ("t5",)):
            return None
        return FiveHourBounds(k5=doc["k5"], raw_upper5=doc["rawUpper5"],
                              k5c=doc["k5c"], t5=doc["t5"],
                              detect_floor=doc["detectFloor"])


def _capture_order_ok(r: Reading, k, t) -> bool:
    """Spec 6.6 *Capture order and ties*: the capture is at least the
    maximum instant AND its stored text at least the lexical maximum. Derived
    rows of a later journal record always carry a larger id, so an equal
    capture (instant and text) sorts the witness first under
    `ORDER BY captured_at_utc DESC, id DESC`; a chronologically later or
    equal but lexically earlier text would let predecessor reads select an
    older row, and never qualifies."""
    if k is not None and r.capture_epoch < k:
        return False
    if t is not None and r.capture_text < t:
        return False
    return True


def weekly_witness_qualifies(r: Reading, b: WeeklyBounds) -> bool:
    """Spec 6.6 *Weekly*, against summaries over the prefix strictly before
    ``r``. The first-entry condition and "no pending debounce matches r's
    week" are implied: every prefix weekly row, and so every armed debounce
    state, carries a date key at most ``s7_date``, and ``r``'s is greater."""
    if (r.week_start_date is None or r.weekly_end_epoch is None
            or r.week_start_at_epoch is None):
        return False
    if b.s7_date is not None and not r.week_start_date > b.s7_date:
        return False
    if b.s7_end is not None and not r.weekly_end_epoch > b.s7_end:
        return False
    if b.c7 is not None and not r.week_start_at_epoch > b.c7:
        return False
    if b.x7 is not None and not r.week_start_at_epoch >= b.x7:
        return False
    if not _capture_order_ok(r, b.k7, b.t7):
        return False
    if b.s7_end is not None and not r.detection_epoch > b.s7_end:
        return False
    return True


def five_hour_witness_qualifies(r: Reading, b: FiveHourBounds, *,
                                detection_floor: "int | None") -> bool:
    """Spec 6.6 *Five-hour*. ``detection_floor`` is the maximum of every
    cohort's U5.raw and every excluded record's raw reset (None when none
    exists). "No confirmation state for (Y, r.key)" is implied by the key
    freshness: confirmation state is keyed by window key, and every prefix
    key is at most ``k5``."""
    if r.five_hour_key is None or r.raw_five_hour_reset is None:
        return False
    if b.k5 is not None and not r.five_hour_key > b.k5:
        return False
    if (b.raw_upper5 is not None
            and not r.raw_five_hour_reset > b.raw_upper5 + FIVE_HOUR_SEPARATION_S):
        return False
    if not _capture_order_ok(r, b.k5c, b.t5):
        return False
    if detection_floor is not None and not r.detection_epoch > detection_floor:
        return False
    return True


@dataclasses.dataclass(frozen=True)
class GateDecision:
    """Whether automatic outcomes may be minted (or a drop published) for
    one axis."""

    allowed: bool
    reason: str


GATE_NO_TRIGGER = GateDecision(True, "no-trigger")
GATE_RELEASED = GateDecision(True, "released")


def all_axes(decision: GateDecision) -> dict:
    return {axis: decision for axis in AXES}


@dataclasses.dataclass
class AxisRecovery:
    """One (generation, account, axis): paused or released, what it waits
    for, the completion covering it, its witness (pending or released), its
    release, the reason of its last revocation and the times doctor shows.
    ``not_before`` is the detection lower bound snapshotted over the prefix
    strictly before a pending witness (spec 7.1); None otherwise."""

    state: str = STATE_PAUSED
    waiting_for: "str | None" = None
    coverage_id: "str | None" = None
    witness_id: "str | None" = None
    release_id: "str | None" = None
    revocation: "str | None" = None
    since: "str | None" = None
    recovered_at: "str | None" = None
    not_before: "int | None" = None
    #: The witness's period key and capture order, kept for revocation.
    witness_key: "str | int | None" = None


@dataclasses.dataclass(frozen=True)
class ControlWitness:
    """The latest control-witness observation of an account in the current
    generation: what an authority completion must name."""

    observation_id: str
    capture_epoch: int
    axes: frozenset
    weekly_end_epoch: "int | None" = None

    def to_document(self) -> dict:
        return {"observationId": self.observation_id,
                "captureEpoch": self.capture_epoch,
                "axes": sorted(self.axes),
                "weeklyEndEpoch": self.weekly_end_epoch}

    @staticmethod
    def from_document(doc) -> "ControlWitness | None":
        if (not isinstance(doc, dict)
                or set(doc) != {"observationId", "captureEpoch", "axes",
                                "weeklyEndEpoch"}
                or not isinstance(doc["observationId"], str)
                or type(doc["captureEpoch"]) is not int
                or not isinstance(doc["axes"], list)
                or not set(doc["axes"]) <= set(AXES)
                or (doc["weeklyEndEpoch"] is not None
                    and type(doc["weeklyEndEpoch"]) is not int)):
            return None
        return ControlWitness(doc["observationId"], doc["captureEpoch"],
                              frozenset(doc["axes"]), doc["weeklyEndEpoch"])


@dataclasses.dataclass(frozen=True)
class Trigger:
    seq: int
    segment: "str | None"
    offset: "int | None"
    kind: str      # "excluded" | "legacy-spool-activity"
    reason: str    # the excluded family, or the activity reason
    at: "str | None"


@dataclasses.dataclass
class Episode:
    """An incomplete-inventory episode, identified by the coordinate of the
    record that opened it (spec 6.3)."""

    opener_segment: "str | None"
    opener_offset: "int | None"
    opened_at: "str | None"
    closer_segment: "str | None" = None
    closer_offset: "int | None" = None
    closed_at: "str | None" = None

    @property
    def key(self) -> tuple:
        return (self.opener_segment, self.opener_offset)

    @property
    def open(self) -> bool:
        return self.closed_at is None and self.closer_segment is None


@dataclasses.dataclass
class ScopeBounds:
    weekly: WeeklyBounds = dataclasses.field(default_factory=WeeklyBounds)
    five_hour: FiveHourBounds = dataclasses.field(default_factory=FiveHourBounds)


def cause_of(trigger: "Trigger | None", inventory_reasons=()) -> "str | None":
    """The doctor cause of the pause a trigger started."""
    if trigger is None:
        return None
    if trigger.kind == TRIGGER_KIND_EXCLUDED:
        return CAUSE_EXCLUDED_WRITE
    if trigger.reason in inventory_reasons:
        return CAUSE_INVENTORY_INCOMPLETE
    return CAUSE_LEGACY_ACTIVITY


class QuarantineState:
    """The deterministic quarantine fold over journal order.

    Every consumer feeds the same events in journal order, so the persisted
    state is a function of the journal alone. Methods mutate in place and
    record which rows changed so a caller persists only those. The glue
    (`_cctally_quarantine.QuarantineFold`) maps records to these events."""

    def __init__(self, *, generation: int = 0,
                 latest_trigger: "Trigger | None" = None,
                 episodes=(), cohort: "Cohort | None" = None,
                 expired: bool = False, max_admitted_at: "int | None" = None,
                 bounds=None, accounts=None, control=None,
                 non_inert_cross_period: bool = False):
        self.generation = int(generation)
        self.latest_trigger = latest_trigger
        self.episodes: list = list(episodes)
        #: The LATEST spool cohort (set at P), and whether its expiry has
        #: been established after P.
        self.cohort = cohort
        self.expired = bool(expired)
        self.max_admitted_at = max_admitted_at
        self.bounds: dict = dict(bounds or {})
        self.accounts: dict = {k: dict(v) for k, v in (accounts or {}).items()}
        self.control: dict = dict(control or {})
        self.non_inert_cross_period = bool(non_inert_cross_period)
        self.new_triggers: list = []
        self.dirty_episodes: set = set()
        self.dirty_bounds: set = set()
        self.dirty_accounts: set = set()
        self.dirty_meta = False
        self.dirty_control: set = set()

    # -- queries ---------------------------------------------------------
    def has_trigger(self) -> bool:
        return self.generation > 0

    @property
    def latest_trigger_seq(self) -> int:
        return self.generation

    def open_episode(self) -> "Episode | None":
        for episode in reversed(self.episodes):
            if episode.open:
                return episode
        return None

    def scope(self, name: str) -> ScopeBounds:
        found = self.bounds.get(name)
        return found if found is not None else ScopeBounds()

    def merged(self, account_key: "str | None") -> ScopeBounds:
        """The summaries a witness of ``account_key`` is judged against: its
        own, the excluded scope's and every cohort's."""
        names = [EXCLUDED_SCOPE, COHORT_SCOPE]
        if account_key is not None:
            names.append(account_key)
        parts = [self.scope(n) for n in names]
        return ScopeBounds(
            weekly=WeeklyBounds.merge(*(p.weekly for p in parts)),
            five_hour=FiveHourBounds.merge(*(p.five_hour for p in parts)))

    def axis(self, account_key: str, axis: str) -> AxisRecovery:
        found = self.accounts.get(account_key, {}).get(axis)
        if found is not None:
            return found
        since = None if self.latest_trigger is None else self.latest_trigger.at
        return AxisRecovery(since=since)

    def _axis_mut(self, account_key: str, axis: str) -> AxisRecovery:
        per = self.accounts.setdefault(account_key, {})
        rec = per.get(axis)
        if rec is None:
            rec = self.axis(account_key, axis)
            per[axis] = rec
        self.dirty_accounts.add(account_key)
        return rec

    def released(self, account_key: str, axis: str) -> bool:
        return self.axis(account_key, axis).state == STATE_RELEASED

    def barrier_open(self) -> bool:
        """Steps 1–2 of the barrier: no episode open, and the latest spool
        cohort (when one exists) has expired."""
        if self.open_episode() is not None:
            return False
        return self.cohort is None or self.expired

    def waiting_for(self, account_key: str, axis: str) -> "str | None":
        if not self.has_trigger() or self.released(account_key, axis):
            return None
        if self.open_episode() is not None:
            return WAIT_INVENTORY
        if self.cohort is not None and not self.expired:
            return WAIT_EXPIRY
        if self.axis(account_key, axis).coverage_id is None:
            return WAIT_USAGE_REFRESH
        return WAIT_FRESH_PERIOD

    def axis_gate(self, account_key: "str | None", axis: str) -> GateDecision:
        if not self.has_trigger():
            return GATE_NO_TRIGGER
        if account_key is not None and self.released(account_key, axis):
            return GATE_RELEASED
        reason = (self.waiting_for(account_key, axis)
                  if account_key is not None else None)
        return GateDecision(False, f"paused:{reason or 'unbound'}")

    def gates(self, account_key: "str | None") -> dict:
        return {axis: self.axis_gate(account_key, axis) for axis in AXES}

    def pending_witness(self, account_key: str, axis: str) -> "str | None":
        rec = self.axis(account_key, axis)
        if (rec.state == STATE_PAUSED and rec.witness_id is not None
                and rec.release_id is None):
            return rec.witness_id
        return None

    def not_before(self, account_key: str, axis: str) -> "int | None":
        """A guaranteed lower bound on the axis's release instant from the
        detection clauses of spec 6.6 (spec 7.1): weekly ``S7.end``; five-hour
        the detection floor. Computed over the prefix strictly before a
        pending witness."""
        if not self.has_trigger() or self.released(account_key, axis):
            return None
        rec = self.axis(account_key, axis)
        if self.pending_witness(account_key, axis) is not None:
            return rec.not_before
        merged = self.merged(account_key)
        if axis == WEEKLY:
            return merged.weekly.s7_end
        return merged.five_hour.detect_floor

    def coverable_axes(self, account_key: str, observation_id: str) -> tuple:
        """The axes an `outcome_authority_completion` naming
        ``observation_id`` would newly cover now: paused, uncovered, observed
        by that observation (the account's latest control witness of the
        current generation), with the barrier's first two steps done and the
        fetch at or after the latest spool cohort's E (spec 6.4)."""
        if not self.has_trigger() or self.open_episode() is not None:
            return ()
        witness = self.control.get(account_key)
        if witness is None or witness.observation_id != observation_id:
            return ()
        if self.cohort is not None and witness.capture_epoch < self.cohort.e:
            return ()
        return tuple(
            axis for axis in AXES
            if axis in witness.axes and not self.released(account_key, axis)
            and self.axis(account_key, axis).coverage_id is None)

    # -- transitions -----------------------------------------------------
    def _set_scope(self, name: str, value: ScopeBounds) -> None:
        if self.bounds.get(name) != value:
            self.bounds[name] = value
            self.dirty_bounds.add(name)

    def _touch_all_accounts(self) -> None:
        self.dirty_accounts.update(self.accounts)

    def note_admitted(self, at_epoch: "int | None") -> None:
        """Every admitted record's `at` (H tracks the greatest)."""
        if at_epoch is not None and (self.max_admitted_at is None
                                     or at_epoch > self.max_admitted_at):
            self.max_admitted_at = at_epoch
            self.dirty_meta = True

    def note_clock(self, clock_epoch: "int | None") -> None:
        """An admitted record after P whose clock instant is at or after E
        establishes expiry (spec 6.3). The caller passes the clock instant
        of observations and ops only, and only for records after P."""
        if (self.cohort is not None and not self.expired
                and self.open_episode() is None
                and clock_epoch is not None and clock_epoch >= self.cohort.e):
            self.expired = True
            self.dirty_meta = True
            self._touch_all_accounts()

    def on_trigger(self, *, kind: str, reason: str, segment=None, offset=None,
                   at=None) -> Trigger:
        """A trigger starts a new generation: every release and every
        coverage of every account becomes void (spec 6.9)."""
        self.generation += 1
        trig = Trigger(self.generation, segment, offset, kind, reason, at)
        self.latest_trigger = trig
        self.new_triggers.append(trig)
        # The new generation's rows replace every account's.
        known = set(self.accounts)
        self.accounts = {key: {} for key in known}
        self.control = {}
        self.dirty_control.update(known)
        self.dirty_accounts.update(known)
        self.dirty_meta = True
        return trig

    def set_position(self, normalize_week_end, *, include_at=None) -> Cohort:
        """P is here: the latest spool cohort is computed from H (which the
        caller already advanced over the record at P) and absorbed into the
        cohort scope's summaries; its expiry is not yet established."""
        h = _max(self.max_admitted_at, include_at)
        if h is None:
            h = 0
        cohort = Cohort.from_h(h, normalize_week_end=normalize_week_end)
        self.cohort = cohort
        self.expired = False
        scope = self.scope(COHORT_SCOPE)
        self._set_scope(COHORT_SCOPE, ScopeBounds(
            weekly=scope.weekly.absorb_cohort(cohort),
            five_hour=scope.five_hour.absorb_cohort(cohort)))
        self.dirty_meta = True
        self._touch_all_accounts()
        return cohort

    def on_episode_open(self, *, segment, offset, at) -> Episode:
        """An episode opener. A later opener while one is open supersedes it
        (the journal's inventory pass names only the latest)."""
        current = self.open_episode()
        if current is not None:
            current.closer_segment, current.closer_offset = segment, offset
            current.closed_at = at
            self.dirty_episodes.add(current.key)
        for existing in self.episodes:
            if existing.key == (segment, offset):
                existing.closer_segment = existing.closer_offset = None
                existing.closed_at = None
                self.dirty_episodes.add(existing.key)
                self.cohort = None
                self.expired = False
                self.dirty_meta = True
                self._touch_all_accounts()
                return existing
        episode = Episode(segment, offset, at)
        self.episodes.append(episode)
        self.dirty_episodes.add(episode.key)
        self.cohort = None
        self.expired = False
        self.dirty_meta = True
        self._touch_all_accounts()
        return episode

    def on_episode_continue(self, *, segment, offset) -> bool:
        """A revision op naming an open episode: no trigger, no episode."""
        current = self.open_episode()
        return current is not None and current.key == (segment, offset)

    def on_episode_close(self, *, segment, offset, at, episode=None) -> bool:
        """Close the open episode at a journaled complete scan. ``episode``
        is the coordinate an inventory-completion op names; it closes only
        that episode. Closing never erases a trigger or a cohort."""
        current = self.open_episode()
        if current is None:
            return False
        if (episode is not None and current.opener_segment is not None
                and tuple(episode) != current.key):
            return False
        current.closer_segment, current.closer_offset = segment, offset
        current.closed_at = at
        self.dirty_episodes.add(current.key)
        self._touch_all_accounts()
        return True

    def absorb_reading(self, scope_name: str, r: Reading, *,
                       possible_effect: bool, excluded: bool = False) -> None:
        scope = self.scope(scope_name)
        self._set_scope(scope_name, ScopeBounds(
            weekly=scope.weekly.absorb_reading(r, possible_effect=possible_effect),
            five_hour=scope.five_hour.absorb_reading(r, excluded=excluded)))

    def absorb_weekly_effect(self, scope_name: str, **effect) -> None:
        scope = self.scope(scope_name)
        self._set_scope(scope_name, ScopeBounds(
            weekly=scope.weekly.absorb_effect(**effect),
            five_hour=scope.five_hour))

    def absorb_five_hour_effect(self, scope_name: str, **effect) -> None:
        scope = self.scope(scope_name)
        self._set_scope(scope_name, ScopeBounds(
            weekly=scope.weekly,
            five_hour=scope.five_hour.absorb_effect(**effect)))

    def weekly_possible_effect(self, account_key: "str | None",
                               r: Reading) -> bool:
        """Spec 6.5 C7 (b): a reading whose weekly axis is a genuine reading
        contributes its capture when some permitted predecessor end is later
        than its detection (the detector's live-end condition)."""
        if WEEKLY not in r.observed_axes or r.week_start_date is None:
            return False
        end = self.merged(account_key).weekly.s7_end
        return end is not None and end > r.detection_epoch

    def on_control_witness(self, account_key: str, witness: ControlWitness) -> None:
        if not self.has_trigger():
            return
        if self.control.get(account_key) != witness:
            self.control[account_key] = witness
            self.dirty_control.add(account_key)

    def on_authority_completion(self, account_key: str, completion_id: str, *,
                                generation: int, observation_id: str,
                                axes) -> bool:
        """Validate a completion op (spec 6.4 *Validity*) and cover each
        listed axis it newly covers. Returns whether it was valid."""
        if generation != self.generation or not self.has_trigger():
            return False
        axes = tuple(axes)
        if not axes or not set(axes) <= set(AXES):
            return False
        coverable = set(self.coverable_axes(account_key, observation_id))
        witness = self.control.get(account_key)
        if witness is None or not set(axes) <= witness.axes:
            return False
        newly = [axis for axis in axes if axis in coverable]
        if not newly:
            return False
        for axis in newly:
            rec = self._axis_mut(account_key, axis)
            rec.coverage_id = completion_id
        if WEEKLY in newly and witness.weekly_end_epoch is not None:
            # Spec 6.5 X7: reference-only drop publications continue in the
            # covered week until its end.
            self.absorb_weekly_effect(account_key,
                                      exclusive_end=witness.weekly_end_epoch)
        return True

    def evaluate_reading(self, account_key: "str | None", r: Reading, *,
                         normalize_week_end=None) -> dict:
        """Fold one admitted reading (spec 6.6, 6.7, 6.9): revoke a released
        axis on backward input, forfeit a pending witness on a later
        X-bearing record, evaluate the gate, evaluate each paused covered
        axis's witness against the prefix strictly before ``r``, then absorb
        ``r``. Returns the per-axis gate for ``r``'s own outcomes — evaluated
        before any witness, so a witness's own derivation runs closed."""
        if account_key is None:
            self.absorb_reading(EXCLUDED_SCOPE, r,
                                possible_effect=self.weekly_possible_effect(None, r))
            return {axis: self.axis_gate(None, axis) for axis in AXES}
        own = self.scope(account_key)
        if self.has_trigger():
            for axis in AXES:
                if not r.bears(axis):
                    continue
                rec = self.axis(account_key, axis)
                if rec.state == STATE_RELEASED:
                    reason = self._revocation(own, axis, r, rec)
                    if reason is not None:
                        self.revoke_axis(account_key, axis, reason,
                                         at=_iso(r.detection_epoch))
                elif self.pending_witness(account_key, axis) is not None:
                    # Spec 6.7: an X-bearing record between witness and
                    # release forfeits the period.
                    rec = self._axis_mut(account_key, axis)
                    rec.witness_id = None
                    rec.witness_key = None
                    rec.not_before = None
        gates = self.gates(account_key)
        if self.has_trigger() and self.barrier_open():
            merged = self.merged(account_key)
            for axis in AXES:
                rec = self.axis(account_key, axis)
                if (rec.state != STATE_PAUSED or rec.coverage_id is None
                        or rec.witness_id is not None
                        or axis not in r.eligible_axes):
                    continue
                if axis == WEEKLY:
                    ok = weekly_witness_qualifies(r, merged.weekly)
                    snapshot = merged.weekly.s7_end
                    key = r.week_start_date
                else:
                    floor = merged.five_hour.detect_floor
                    ok = five_hour_witness_qualifies(
                        r, merged.five_hour, detection_floor=floor)
                    snapshot = floor
                    key = r.five_hour_key
                if ok:
                    rec = self._axis_mut(account_key, axis)
                    rec.witness_id = r.observation_id
                    rec.witness_key = key
                    rec.not_before = snapshot
        self.absorb_reading(account_key, r,
                            possible_effect=self.weekly_possible_effect(account_key, r))
        return gates

    @staticmethod
    def _revocation(own: ScopeBounds, axis: str, r: Reading,
                    rec: AxisRecovery) -> "str | None":
        if axis == WEEKLY:
            if rec.witness_key is not None and r.week_start_date < rec.witness_key:
                return REVOKE_BACKWARD_KEY
            k, t = own.weekly.k7, own.weekly.t7
        else:
            if rec.witness_key is not None and r.five_hour_key < rec.witness_key:
                return REVOKE_BACKWARD_KEY
            k, t = own.five_hour.k5c, own.five_hour.t5
        if (k is not None and r.capture_epoch < k) or (
                t is not None and r.capture_text < t):
            return REVOKE_ORDER_REGRESSION
        return None

    def on_release(self, account_key: str, axis: str, *, generation: int,
                   witness_id: str, completion_id: str, release_id: str,
                   at: "str | None") -> bool:
        """Validate an `outcome_axis_release` (spec 6.7 *Validity*): current
        generation, the completion that covers the axis, the axis's pending
        witness (no X-bearing record of the account since). Invalid → ignored."""
        if (generation != self.generation or axis not in AXES
                or not self.has_trigger()):
            return False
        rec = self.axis(account_key, axis)
        if (rec.state != STATE_PAUSED or rec.coverage_id is None
                or rec.coverage_id != completion_id
                or self.pending_witness(account_key, axis) != witness_id):
            return False
        rec = self._axis_mut(account_key, axis)
        rec.state = STATE_RELEASED
        rec.release_id = release_id
        rec.recovered_at = at
        rec.revocation = None
        rec.not_before = None
        return True

    def revoke_axis(self, account_key: str, axis: str, reason: str, *,
                    at: "str | None" = None, void_coverage: bool = False) -> None:
        """A released axis returns to paused with its reason and needs a new
        witness in a later period; a pending witness is dropped. Coverage
        stays valid unless ``void_coverage`` (an administrative op)."""
        rec = self.axis(account_key, axis)
        if (rec.state != STATE_RELEASED and rec.witness_id is None
                and not (void_coverage and rec.coverage_id is not None)):
            return
        rec = self._axis_mut(account_key, axis)
        if rec.state == STATE_RELEASED:
            rec.revocation = reason
            rec.since = at or rec.since
        rec.state = STATE_PAUSED
        rec.witness_id = None
        rec.witness_key = None
        rec.release_id = None
        rec.recovered_at = None
        rec.not_before = None
        if void_coverage:
            rec.coverage_id = None

    def revoke_all(self, reason: str, *, at=None, axes=AXES,
                   void_coverage: bool = False) -> None:
        for account_key in sorted(self.accounts):
            for axis in axes:
                self.revoke_axis(account_key, axis, reason, at=at,
                                 void_coverage=void_coverage)

    def on_deletion_outcome(self, account_key: "str | None", axis: str, *,
                            at=None) -> None:
        """A journaled automatic outcome that deleted snapshot rows. While
        every registered cross-period fallback read is inert this revokes
        nothing; otherwise BOTH axes — of every account when the event names
        none — are revoked (spec 6.9). ``axis`` is the outcome's own axis,
        but the rows it deletes are shared snapshot rows: a five-hour credit
        that deletes the common latest row sends the weekly detector's
        moved-end predecessor lookup or debounce singleton back into the
        paused prefix, so the reads that can fall back are either axis's."""
        del axis
        if not self.non_inert_cross_period:
            return
        if account_key is None:
            self.revoke_all(REVOKE_NON_INERT_FALLBACK, at=at)
        else:
            for each in AXES:
                self.revoke_axis(account_key, each, REVOKE_NON_INERT_FALLBACK,
                                 at=at)

    def ensure_account(self, account_key: str) -> None:
        """Materialize an account's rows in the current generation."""
        if account_key not in self.accounts:
            self.accounts[account_key] = {}
            self.dirty_accounts.add(account_key)


def _iso(epoch: "int | None") -> "str | None":
    if epoch is None:
        return None
    return _dt.datetime.fromtimestamp(int(epoch), tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def iso(epoch: "int | None") -> "str | None":
    return _iso(epoch)


def axis_row(state: QuarantineState, account_key: str, axis: str) -> tuple:
    """The persisted columns of one (account, axis) in the current
    generation, in `outcome_quarantine_axis_state` column order after
    ``(generation, account_key, axis)``."""
    rec = state.axis(account_key, axis)
    return (rec.state, state.waiting_for(account_key, axis), rec.coverage_id,
            rec.witness_id, rec.release_id, rec.revocation, rec.since,
            rec.recovered_at, rec.not_before,
            None if rec.witness_key is None else json.dumps(rec.witness_key))


def axis_from_row(row) -> "AxisRecovery | None":
    """The `AxisRecovery` a persisted row describes, or None when the row is
    unverifiable (the caller then reads the axis as paused)."""
    (state, waiting, coverage, witness, release, revocation, since,
     recovered, not_before, witness_key) = row
    if state not in (STATE_PAUSED, STATE_RELEASED):
        return None
    if waiting is not None and waiting not in WAITING_FOR:
        return None
    if not_before is not None and type(not_before) is not int:
        return None
    try:
        key = None if witness_key is None else json.loads(witness_key)
    except (TypeError, ValueError):
        return None
    if key is not None and not isinstance(key, (str, int)):
        return None
    if state == STATE_RELEASED and (release is None or witness is None
                                    or coverage is None):
        return None
    return AxisRecovery(state=state, waiting_for=waiting, coverage_id=coverage,
                        witness_id=witness, release_id=release,
                        revocation=revocation, since=since,
                        recovered_at=recovered, not_before=not_before,
                        witness_key=key)
