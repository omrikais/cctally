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

#: The payload shape's own version, so a later field addition is legible at
#: replay without guessing from key presence.
JOURNAL_IDENTITY_VERSION = 1

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

    Every field is carried into the journal payload, so the payload alone
    suffices to reconstruct the row and a later deletion or quarantine of the
    calibration file cannot affect a rebuild (§6.5 step 4). That is what makes
    the latch survive a rebuild whose key originated in an unjournaled file.
    """

    provider: str
    account_key: str
    effective_from: str
    previous_units_per_point: float
    new_units_per_point: float
    severity: str
    detected_at: str

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
    """
    after_pairs = _adjacent_pairs(after_regimes or ())
    before_pairs = _adjacent_pairs(before_regimes or ())
    out: list = []
    for start in sorted(s for s in after_pairs if s not in before_pairs):
        predecessor, successor = after_pairs[start]
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


def event_payload(transition: RateChangeTransition, *, created_at: str) -> dict:
    """The COMPLETE deterministic journal payload (§6.5 step 4).

    Self-sufficient by design: the row is reconstructible from this alone, so
    a later deletion or quarantine of the calibration file cannot affect a
    rebuild. The payload is also a pure function of the transition, which is
    what makes a crash-replayed duplicate line byte-identical to the original.
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


def alert_payload(transition: RateChangeTransition) -> dict:
    """The notifier payload. `axis` carries the family, NOT a registry id.

    There is no `threshold` key, and there must not be one: the dispatch glue
    derives severity from a threshold when it finds one, and this family's
    severity is explicit. `severity` is therefore passed verbatim.
    """
    return {
        "axis": FAMILY,
        "severity": transition.severity,
        "provider": transition.provider,
        "account_key": transition.account_key,
        "effective_from": transition.effective_from,
        "previous_units_per_point": transition.previous_units_per_point,
        "new_units_per_point": transition.new_units_per_point,
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
    }
