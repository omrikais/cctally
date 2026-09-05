"""The meter-rate-change event family's pure kernel (#661 S2 spec §6).

A provider metering-rate transition has no percentage threshold and no
threshold-derived severity, so §6.1 keeps it OUT of `AXIS_REGISTRY` — every
member of which is a numeric threshold axis whose `AlertEntry` requires a
numeric `threshold`. This module owns the family's identity, its severity and
its complete deterministic event payload, and it is pure: stdlib only, no
clock read, no database, no file access.

The transition is derived by COMPARING two persisted calibration states, which
is what makes it a function of the persistence transition itself rather than
of a detector re-run. §6.5 step 1 puts that comparison under the calibration
file's lock, and everything here runs inside it.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

UTC = dt.timezone.utc

#: The family's discriminator on the journal, on the wire and in the notifier
#: dispatch payload. Deliberately NOT a member of `AXIS_REGISTRY`.
FAMILY = "meter_rate_change"

#: The journal evt kind and its opaque id prefix. The id is a token and is
#: never parsed back into its components (the journal contract forbids it).
EVT_KIND = FAMILY
EVT_ID_PREFIX = "mrc"

#: #690: the version-2 identity prefix. New transitions are emitted under it
#: with a self-sufficient payload that also carries the disclosure evidence.
#:
#: A second PREFIX rather than a key on the v1 payload or an ordinary higher
#: revision. A key is not available: event selection compares revision, status
#: and the whole-record hash, and preflight quarantines same-revision
#: divergence, so a re-emission of an already-recorded identity under a
#: changed payload would be quarantined. A higher revision is not available
#: either, because revisions above zero belong to correction batches.
#:
#: The cost of a second prefix is that journal selection and correction key on
#: `event_id` ALONE, so two prefixes are two independent logical events unless
#: every path that reaches one of them accounts for both. Two paths do.
#:
#: The PRESENCE check was taught both: `unrecorded_rate_change_transitions`
#: builds both ids per identity, and a terminal latch under either suppresses
#: recovery under the other. Without that, a tombstoned or higher-revision v1
#: record would be invisible to the v2 lookup and the transition would be
#: resurrected the first time a v2-capable binary ran.
#:
#: The EMITTER was taught it too (#750 S2): `record_meter_rate_change` emits
#: under v1 whenever the natural key holds ANY v1 record, and reaches v2 only
#: for a key with none. So a v1 record the presence check re-offered is
#: re-emitted as v1 and converges its missing row rather than becoming a
#: second logical event, and a v1 record in any other state stays latched.
#: Existence rather than liveness, because the emitter's other caller is
#: `cmd_quota`'s FRESH detection, which the presence check does not gate: a
#: tombstoned v1 record would otherwise be invisible to the v2 lookup and the
#: transition resurrected the first time it was detected again.
#:
#: The CORRECTION path needs nothing, and that is by construction rather than
#: by omission. `_preview_from_snapshot` in `bin/_cctally_rederive.py` raises
#: `RederiveConflict` for any family other than `_lib_rederive.FAMILY`, which
#: is `claude-usage`, so no supported `db rederive` family can target an event
#: under either prefix. A correction that could reach one would have to add a
#: family first, and adding one is where this question would have to be
#: answered.
EVT_ID_PREFIX_V2 = "mrc2"

#: The payload shape's own version, so a later field addition is legible at
#: replay without guessing from key presence.
JOURNAL_IDENTITY_VERSION = 1

#: The v2 payload's discriminator. Distinct from `JOURNAL_IDENTITY_VERSION`,
#: which describes the IDENTITY tuple `identity()` returns and is unchanged:
#: `(provider, account_key, effective_from)` still keys the row. This names
#: the KEY SET of the payload, which is what the fold branches on.
PAYLOAD_VERSION_V2 = 2

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ALARM = "alarm"

#: `unitsPerPoint` is weighted quota units per meter point, so a HIGHER value
#: means more work before the meter moves — a more generous rate. A drop is
#: therefore the direction a user needs to know about, and a drop of at least
#: this fraction is the one worth an alarm rather than a warning. The
#: maintainer's own observed transition sits 31% below its predecessor.
ALARM_DROP_FRACTION = 0.25

#: Below this the change is noise rather than a transition worth a severity
#: above `info`. The detector has already confirmed a split by the time this
#: runs, so this only separates warn from info.
NOTABLE_DROP_FRACTION = 0.02


@dataclasses.dataclass(frozen=True)
class RateChangeTransition:
    """One confirmed predecessor-to-successor metering-rate transition.

    Every field the ROW is made of is carried into the journal payload, so
    the payload alone suffices to reconstruct the row and a later deletion or
    quarantine of the calibration file cannot affect a rebuild (§6.5 step 4).
    That is what makes the latch survive a rebuild whose key originated in an
    unjournaled file.

    `withholding_status` is the one field outside that set (#688). It is
    notification disclosure rather than row content, it reaches only
    `alert_payload`, and its own comment states why the journal payload must
    stay byte-stable.
    """

    provider: str
    account_key: str
    effective_from: str
    previous_units_per_point: float
    new_units_per_point: float
    severity: str
    detected_at: str
    #: The calibration status the run was withheld under, when the transition
    #: was admitted by #688's detection-keyed path; `None` for every ordinary
    #: transition. It is DISCLOSURE CONTEXT and nothing else: it is not part
    #: of `identity()`, and it is deliberately absent from `event_payload`,
    #: because event selection hashes the whole record and quarantines two
    #: revision-0 records that share an id and differ in hash. A key added to
    #: the journal payload would quarantine any re-emission of an
    #: already-recorded identity. `alert_payload` is never journaled, so the
    #: disclosure rides there instead.
    #:
    #: #690 UPDATES that last sentence for the three fields below and for this
    #: one: the disclosure now ALSO rides a version-2 journal payload under
    #: `EVT_ID_PREFIX_V2`, which is a distinct identity and therefore cannot
    #: make a re-emitted v1 identity hash differently from its retained line.
    #: The v1 payload stays byte-frozen.
    withholding_status: "str | None" = None
    #: The typed `WithholdingCause` values the detector's own inputs carried,
    #: as a canonical JSON array of enum VALUES, or None.
    #:
    #: None, `"[]"` and a populated array are THREE distinct states: None
    #: means legacy or unrecoverable evidence, `"[]"` means assessed with no
    #: such origin. Serialized as values rather than `repr` so a member added
    #: later cannot silently alias onto an existing one.
    detector_input_causes: "str | None" = None
    #: The typed `CompositionProvenance` values, same serialization and the
    #: same three-state rule. A status NAME cannot identify its origin —
    #: there are five members — which is why this is carried separately
    #: rather than folded into `withholding_status`.
    composition_provenance: "str | None" = None
    #: `baseline_fit.population["withheld"]` (#692): how many fenced
    #: observations carrying a cause fall strictly BEFORE `window_start`.
    #:
    #: A COUNT, not a boolean, so zero, one and many stay distinguishable and
    #: honest copy stays possible; None is reserved for the legacy case where
    #: it was never retained. It is never inferred from
    #: `detector_input_causes`, from diagnostics text, or from the successor
    #: status. Folding it into `withholding_status` is rejected: it would make
    #: an otherwise healthy successor read as currently withheld.
    baseline_withheld_days: "int | None" = None

    def identity(self) -> tuple:
        """The §6.3 key: `(provider, account identity, effectiveFrom)`.

        The constants fingerprint is deliberately absent. Including it would
        re-alert on our own algorithm revisions, and excluding it does not
        make the key immune to split drift — a revised detector can select a
        different `effectiveFrom` for the same underlying provider transition,
        and R8 can turn a formerly merged identity into a real account key.
        The promise is once per exact key, not once per provider regime.
        """
        return (self.provider, self.account_key, self.effective_from)


def _finite_positive(value) -> "float | None":
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number if number > 0.0 else None


def _instant(value) -> "dt.datetime | None":
    """Parse an ISO instant, or None. Never compares timestamp STRINGS.

    The store holds mixed UTC offset spellings, so two spellings of one
    instant must resolve to one key or a re-run would look like a second
    transition.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


#: #747 — the codomain of `transition_severity`, as a value rather than a
#: convention. `dashboard/web/src/types/envelope.ts` derives its
#: `RateChangeSeverity` type from a tuple of the same three tokens and
#: `toastSeverityCoverage.test.ts` walks that tuple to prove each member has a
#: reachable border rule. Nothing connected the two halves, so a fourth token
#: added here would render on the base amber border with no test on either
#: side observing it.
#:
#: Order is part of the contract: the parity assertion is ordered equality.
#: The members are the module's own constants rather than fresh literals, so
#: this really is the codomain — a re-spelled literal could drift from what
#: `transition_severity` actually returns and still look correct here.
RATE_CHANGE_SEVERITIES = (SEVERITY_INFO, SEVERITY_WARN, SEVERITY_ALARM)


def transition_severity(previous: float, new: float) -> str:
    """An EXPLICIT severity, never derived from a threshold.

    §6.1: the family has no percentage threshold, so `severity_for` — which
    maps an integer threshold onto the three tiers — cannot describe it. A
    rate that became more generous is information; one that became less
    generous is a warning, and a drop of a quarter or more is an alarm.
    """
    if previous <= 0.0 or new <= 0.0:
        return SEVERITY_INFO
    if new >= previous:
        return SEVERITY_INFO
    drop = (previous - new) / previous
    if drop >= ALARM_DROP_FRACTION:
        return SEVERITY_ALARM
    if drop >= NOTABLE_DROP_FRACTION:
        return SEVERITY_WARN
    return SEVERITY_INFO


def _adjacent_pairs(regimes) -> dict:
    """`{successor start instant -> (predecessor, successor)}`.

    A pair exists when a CLOSED regime's `effectiveUntil` is the exact instant
    a later regime starts at. `reduce_state` writes both ends from one
    `boundary_iso` on a confirmed change, so a real transition is adjacent by
    construction.

    A predecessor marked `stale` is excluded, which is what keeps a
    FINGERPRINT-only recalculation out: that path closes the open regime at
    `now` with `status: "stale"` and appends a fresh regime starting at the
    analysis start, so the two are neither adjacent nor a rate transition. The
    status check is the second, independent guard.
    """
    closed: dict = {}
    for regime in regimes:
        if not isinstance(regime, dict):
            continue
        if str(regime.get("status") or "") == "stale":
            continue
        until = _instant(regime.get("effectiveUntil"))
        if until is not None:
            closed.setdefault(until, regime)
    pairs: dict = {}
    for regime in regimes:
        if not isinstance(regime, dict):
            continue
        start = _instant(regime.get("effectiveFrom"))
        if start is None:
            continue
        predecessor = closed.get(start)
        if predecessor is None or predecessor is regime:
            continue
        pairs[start] = (predecessor, regime)
    return pairs


def enumerate_transitions(regimes, *, provider, account_key,
                          detected_at) -> tuple:
    """EVERY qualified transition in one persisted state, oldest first.

    This is the qualification, and `detect_transitions` is a freshness filter
    over it. They must not be two qualifications: recovery (#689) enumerates
    what is already persisted, and a pair it admitted but detection refused —
    or the reverse — would record a transition the detector never confirmed.

    ALL FOUR evidence fields are deliberately left at their `None` defaults:
    `withholding_status`, `detector_input_causes`, `composition_provenance`
    and `baseline_withheld_days`. Each is captured at detection time from the
    `QuotaAnalysis` — #688 for the first, #690 and #692 for the other three —
    and this function sees only stored regimes and no analysis at all.

    That is accepted rather than repaired, which is why the four columns are
    nullable: a transition reconstructed on the #689 recovery route carries
    null evidence BY CONSTRUCTION, and the client's fallback branch renders
    honest generic copy rather than asserting a cause it does not have.
    Spec §3.3 records this docstring as the decision.
    """
    pairs = _adjacent_pairs(regimes or ())
    out: list = []
    for start in sorted(pairs):
        predecessor, successor = pairs[start]
        previous = _finite_positive(predecessor.get("unitsPerPoint"))
        new = _finite_positive(successor.get("unitsPerPoint"))
        if previous is None or new is None:
            # A pair whose rates are not both finite and positive describes
            # no measurable transition. Recording it would publish two
            # numbers the renderer would have to withhold anyway, and
            # skipping it must not stop the walk over the remaining pairs.
            continue
        out.append(RateChangeTransition(
            provider=str(provider),
            account_key=str(account_key),
            effective_from=start.isoformat(),
            previous_units_per_point=previous,
            new_units_per_point=new,
            severity=transition_severity(previous, new),
            detected_at=str(detected_at),
        ))
    return tuple(out)


def detect_transitions(before_regimes, after_regimes, *, provider,
                       account_key, detected_at) -> tuple:
    """Every transition the persistence step just created, oldest first.

    §6.3: the axis fires only on a confirmed predecessor-to-successor
    transition — never on the initial regime a new user first fits, which has
    no predecessor to be adjacent to, and never on a fingerprint-only
    recalculation, which produces no adjacent non-stale pair.

    Comparing BEFORE against AFTER is what makes a re-run of `cctally quota`
    over an already-recorded change produce nothing: the pair is present on
    both sides. The durable row's UNIQUE key is still the latch — this only
    keeps the journal from growing a line per invocation.

    EVERY fresh pair is returned, not the newest one (#661 S2 Stage C
    review). The earlier form took `max(fresh)`, and because the next call
    sees both pairs in `before`, an unreported pair was lost permanently
    rather than deferred. §6.3 promises "once per exact key", which reads as
    at least once per key. Returning the whole list costs nothing, because
    `_insert_meter_rate_change` is an `INSERT OR IGNORE` over that same key
    and the caller records each descriptor through it.

    The subtraction compares CANONICAL instants. `_adjacent_pairs` keys on a
    parsed datetime normalised to UTC, and a descriptor's `effective_from` is
    that key's `isoformat()`, so both sides agree on one spelling. Do not pass
    those keys through `_instant`: it accepts only a string and returns None
    for a datetime, which would empty the before set and re-emit everything.
    """
    already = {start.isoformat()
               for start in _adjacent_pairs(before_regimes or ())}
    return tuple(
        t for t in enumerate_transitions(
            after_regimes, provider=provider, account_key=account_key,
            detected_at=detected_at)
        if t.effective_from not in already)


def event_payload(transition: RateChangeTransition, *, created_at: str) -> dict:
    """The COMPLETE deterministic journal payload (§6.5 step 4).

    Self-sufficient by design: the row is reconstructible from this alone, so
    a later deletion or quarantine of the calibration file cannot affect a
    rebuild.

    The payload is a pure function of the transition AND of `created_at`,
    which the caller takes from the command clock. A crash-replayed duplicate
    is still byte-identical, because replay re-reads the line that is already
    on the journal rather than rebuilding this payload. A RE-EMISSION is not:
    the same transition emitted by a later run carries a different
    `created_at_utc` and `detected_at_utc` and therefore a different content
    hash under the same event id. That is #689's hazard, and it is why
    `record_meter_rate_change` classifies before it appends rather than
    relying on this payload being reproducible.
    """
    return {
        "provider": transition.provider,
        "account_key": transition.account_key,
        "effective_from": transition.effective_from,
        "previous_units_per_point": transition.previous_units_per_point,
        "new_units_per_point": transition.new_units_per_point,
        "severity": transition.severity,
        "detected_at_utc": transition.detected_at,
        "created_at_utc": created_at,
        "journal_identity_version": JOURNAL_IDENTITY_VERSION,
    }


def event_payload_v2(transition: RateChangeTransition, *,
                     created_at: str) -> dict:
    """The version-2 journal payload: the complete row PLUS the evidence.

    Self-sufficient in the same way `event_payload` is — the row is
    reconstructible from this alone — and emitted under `EVT_ID_PREFIX_V2`,
    which is a different logical event, so the v1 payload's byte-freeze is
    untouched and no already-recorded v1 identity can be made to hash
    differently.

    The four evidence values are published ALWAYS, as their value or as JSON
    `null`, following this repository's wire rule that a published key is
    nulled rather than dropped. A reader must be able to tell null from `[]`
    from `0`.
    """
    payload = event_payload(transition, created_at=created_at)
    payload["payload_version"] = PAYLOAD_VERSION_V2
    payload["withholding_status"] = transition.withholding_status
    payload["detector_input_causes"] = transition.detector_input_causes
    payload["composition_provenance"] = transition.composition_provenance
    payload["baseline_withheld_days"] = transition.baseline_withheld_days
    return payload


def alert_payload(transition: RateChangeTransition) -> dict:
    """The notifier payload. `axis` carries the family, NOT a registry id.

    There is no `threshold` key, and there must not be one: the dispatch glue
    derives severity from a threshold when it finds one, and this family's
    severity is explicit. `severity` is therefore passed verbatim.

    `withholding_status` is emitted ALWAYS, as a string or as JSON `null`,
    following this repository's wire rule that a published key is nulled
    rather than dropped (#688). It appears here and not in `event_payload`
    because this payload is dispatched from `ctx.deferred_alerts` and is never
    journaled, so adding it cannot make a re-emitted identity hash
    differently from its retained line.
    """
    return {
        "axis": FAMILY,
        "severity": transition.severity,
        "provider": transition.provider,
        "account_key": transition.account_key,
        "effective_from": transition.effective_from,
        "previous_units_per_point": transition.previous_units_per_point,
        "new_units_per_point": transition.new_units_per_point,
        "withholding_status": transition.withholding_status,
        # #690: the rest of the disclosure, on the same never-journaled
        # payload and under the same nulled-not-dropped rule. The notifier
        # copy needs the cause to avoid promising a fitted budget the
        # withheld calibration may not have produced, and the count to say
        # how thin the baseline was without inferring it from the status.
        "detector_input_causes": transition.detector_input_causes,
        "composition_provenance": transition.composition_provenance,
        "baseline_withheld_days": transition.baseline_withheld_days,
    }


def rate_change_ratio(transition: RateChangeTransition) -> "float | None":
    """`new / previous`, or None when either end is unusable."""
    if transition.previous_units_per_point <= 0.0:
        return None
    return transition.new_units_per_point / transition.previous_units_per_point


def active_rate_change(regimes) -> "dict | None":
    """Spec §6.6's marker predicate: the ACTIVE open regime has a confirmed
    predecessor, and the marker shows for the whole of that successor regime.

    Derived, never stored — S1 persists regime history rather than a current
    detector verdict, so "while a change is detected" needs a definition or
    the marker never clears. This requires no new durable state, which §1
    forbids this session from adding.

    Returns the transition's own numbers, or None when no open regime has a
    predecessor. The open regime is the one with no `effectiveUntil`; a store
    holding several is malformed and the LATEST is taken, which is the same
    choice the reader makes.

    `calibration_status` is the SUCCESSOR regime's own status (#688). It is
    reported because a successor that is not prediction-ready has no fitted
    budget, and `doctor`'s remediation used to send that user to a command
    that would refuse.
    """
    pairs = _adjacent_pairs(regimes or ())
    open_starts = [
        start for start, (_pred, succ) in pairs.items()
        if succ.get("effectiveUntil") in (None, "")
    ]
    if not open_starts:
        return None
    start = max(open_starts)
    predecessor, successor = pairs[start]
    previous = _finite_positive(predecessor.get("unitsPerPoint"))
    new = _finite_positive(successor.get("unitsPerPoint"))
    return {
        "active": True,
        "effective_from": start.isoformat(),
        "previous_units_per_point": previous,
        "new_units_per_point": new,
        "severity": (transition_severity(previous, new)
                     if previous is not None and new is not None
                     else SEVERITY_INFO),
        "calibration_status": successor.get("status"),
    }
