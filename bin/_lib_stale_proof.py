"""Provider-sample staleness proofs and accounting receipts (#797 #823 #840).

Pure kernel for spec section 4.4: no I/O. The glue resolves the account's
provider samples, its accounted value, its credits and the stats index
identity from one validated snapshot and passes them in; the reducer then
receives one immutable `MemberFacts` per member.

A member's rise is refused only when its sample identity σ is *proven stale*
by a provider sample requested after both its frozen provider-activity
instant `t_m` and its receipt's completion instant `e_σ`, **and** σ holds a
valid accounting receipt. Everything else is eligible, so a window in which
no member is proven stale reduces exactly as before.

Order is always decided on precision intervals: an instant written with p
fractional digits stands for ``[k, k + 10**(9-p))`` nanoseconds, and "a is
after b" is proven only when a's interval starts at or after b's ends.
Equal instants, overlapping precision and missing instants never order.

Both axes are decided here. The weekly axis earns by (i) or (ii); the
five-hour axis earns by (i) only, against the `statusline` source's detector
baseline under the current five-hour credit generation (D11), and its
validity follows the five-hour rows of the section 4.4 table.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Callable, Collection, Iterable, Literal, Mapping, Sequence

import _lib_oauth_samples as _samples
import _lib_provider_activity as _activity


TOLERANCE_PP = 1.0
SUPPORTED_REVISIONS = ((_activity.EVIDENCE_REVISION, _activity.PARSER_REVISION),)

WEEKLY = "sevenDay"
FIVE_HOUR = "fiveHour"
AXES = (WEEKLY, FIVE_HOUR)
PATHS = ("maximal", "provider_support")
CAUSAL_KINDS = ("provider", "statusline", "manual", "legacy", "corrected", "unknown")

# Samples captured this close to the window's reset are all kept by
# `compact_samples`: a member's advertised (raw) reset lies within half an
# hour of the normalized window, and a sample captured after it proves
# nothing for that member.
_RESET_MARGIN_SECONDS = 3600
_NS = 1_000_000_000


class MalformedReceipt(ValueError):
    """A receipt or credit document that does not match the codec."""


# --- instants --------------------------------------------------------------------


def _interval(text: str | None) -> tuple[int, int] | None:
    """The ``[start, end)`` nanosecond interval a canonical instant denotes."""
    if not isinstance(text, str):
        return None
    try:
        start = _activity.instant_key(text)
        digits = _activity.instant_precision(text)
    except ValueError:
        return None
    return start, start + 10 ** (9 - min(digits, 9))


def after(a: str | None, b: str | None) -> bool:
    """Whether instant ``a`` is proven to follow instant ``b``."""
    ia, ib = _interval(a), _interval(b)
    return ia is not None and ib is not None and ia[0] >= ib[1]


def _before_epoch(text: str | None, epoch: int) -> bool:
    """Whether instant ``text`` is proven to precede the exact second ``epoch``."""
    interval = _interval(text)
    return interval is not None and interval[1] <= epoch * _NS


def _at_or_after_epoch(text: str | None, epoch: int) -> bool:
    interval = _interval(text)
    return interval is not None and interval[0] >= epoch * _NS


def _instant_order(text: str) -> int:
    interval = _interval(text)
    return -1 if interval is None else interval[0]


# --- identities --------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SampleIdentity:
    """σ: one member's sample, keyed by everything that makes it that sample."""

    token: str
    activity_at_utc: str
    axis: str
    percent: float
    window: int
    evidence_revision: int
    parser_revision: int

    def key(self) -> str:
        """A stable digest of σ, the receipt map key."""
        text = json.dumps([self.token, self.activity_at_utc, self.axis,
                           float(self.percent), int(self.window),
                           self.evidence_revision, self.parser_revision],
                          separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()


@dataclasses.dataclass(frozen=True)
class Member:
    """A candidate member with known evidence: σ and the raw reset it advertised."""

    sigma: SampleIdentity
    advertised_reset_epoch: int | None = None


@dataclasses.dataclass(frozen=True)
class MemberFacts:
    refused: bool
    proof_observation_id: str | None
    receipt_valid: bool


ALL_ELIGIBLE = MemberFacts(False, None, False)


def sample_identity(token: str, witness, *, axis: str, percent: float,
                    window: int) -> SampleIdentity | None:
    """σ for a member, or None when its evidence is unknown: no witness, an
    unknown status, or an evidence or parser revision this kernel does not
    support (an unsupported revision never proves and never earns)."""
    if witness is None or witness.status != "known" or witness.instant_utc is None:
        return None
    if (witness.evidence_revision, witness.parser_revision) not in SUPPORTED_REVISIONS:
        return None
    if _interval(witness.instant_utc) is None:
        return None
    return SampleIdentity(token, witness.instant_utc, axis, float(percent), int(window),
                          witness.evidence_revision, witness.parser_revision)


def _window_text(window: int) -> str:
    return _samples.normalize_weekly_reset(int(window))


def _sample_window(sample) -> str | None:
    if sample.weekly_resets_at_utc is None:
        return None
    try:
        return _samples.weekly_window(sample.weekly_resets_at_utc)
    except ValueError:
        return None


def _in_window(samples: Iterable, window: int) -> list:
    text = _window_text(window)
    return [s for s in samples
            if s.weekly_percent is not None and _sample_window(s) == text]


def _five_in_window(samples: Iterable, window: int) -> list:
    """The samples of one five-hour window, by canonical key equality."""
    return [s for s in samples
            if s.five_hour_percent is not None and s.five_hour_window_key is not None
            and int(s.five_hour_window_key) == int(window)]


def _axis_window(samples: Iterable, axis: str, window: int) -> list:
    return (_five_in_window(samples, window) if axis == FIVE_HOUR
            else _in_window(samples, window))


def _axis_value(sample, axis: str) -> float:
    return float(sample.five_hour_percent if axis == FIVE_HOUR else sample.weekly_percent)


def _latest(samples: Iterable):
    best = None
    for s in samples:
        if best is None or (_instant_order(s.requested_at_utc), s.observation_id) > (
                _instant_order(best.requested_at_utc), best.observation_id):
            best = s
    return best


# --- the proof -----------------------------------------------------------------------


def proven_stale(sigma: SampleIdentity, samples: Iterable, *, after_utc: str,
                 advertised_reset_epoch: int | None = None):
    """The latest provider sample proving σ stale, or None.

    A sample proves σ stale when it is in σ's authoritative window, its
    request instant ``q`` follows both ``t_m`` and ``after_utc``, its raw
    value is strictly more than `TOLERANCE_PP` below σ's, and its response
    was captured before the member's advertised reset (the window's reset
    when the member advertised none). On the five-hour axis the window is
    the canonical key and the value is the sample's raw five-hour percent."""
    if sigma.axis not in AXES:
        return None
    reset = sigma.window if advertised_reset_epoch is None else int(advertised_reset_epoch)
    proving = [
        s for s in _axis_window(samples, sigma.axis, sigma.window)
        if _axis_value(s, sigma.axis) < sigma.percent - TOLERANCE_PP
        and after(s.requested_at_utc, sigma.activity_at_utc)
        and after(s.requested_at_utc, after_utc)
        and _before_epoch(s.captured_at_utc, reset)
    ]
    return _latest(proving)


# --- accounting ------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CreditTransition:
    """One credit of an account and window, resolved through its causal low."""

    axis: str
    event_id: str
    event_revision: str
    causal_kind: Literal["provider", "statusline", "manual", "legacy", "corrected",
                         "unknown"]
    requested_at_utc: str | None
    # Five-hour cross-source facts (Task 5).
    baseline_requested_at_utc: str | None = None
    baseline_percent: float | None = None
    arming_requested_at_utc: str | None = None


@dataclasses.dataclass(frozen=True)
class Accounted:
    """What a completed, covered derivation left for an account, axis and window."""

    account_key: str
    axis: str
    window: int
    percent: float | None          # the effective stored value of the current segment
    support_id: str | None         # the supporting observation or publication
    support_hash: str | None       # the supporting axis decision's content hash
    floor_epoch: int | None        # the current segment's floor
    credits: tuple                 # the credit generation: CreditTransition, ...
    coverage: str | None           # the covered interpretation and frontier
    detector_policy: int | None = None


@dataclasses.dataclass(frozen=True)
class Receipt:
    sigma: SampleIdentity
    path: Literal["maximal", "provider_support"]
    account_key: str
    window: int
    support_id: str | None
    support_hash: str | None
    accounted_percent: float | None
    provider_observation_id: str | None
    provider_percent: float | None
    provider_requested_at_utc: str | None
    provider_window: str | None
    detector_policy: int | None
    credits: tuple                 # ((event_id, event_revision), ...)
    coverage: str | None
    completed_at_utc: str          # e_σ, a completion upper bound
    index_identity: str


def earn_receipts(members: Sequence[Member], accounted: Accounted, samples: Iterable, *,
                  completed_at_utc: str, index_identity: str) -> tuple[Receipt, ...]:
    """The receipts a covered, completed derivation earns for ``members``.

    (i) A member whose value equals exactly the accounted value earns a
    maximal receipt; ties earn independently. (ii) Otherwise a weekly member
    below the accounted value earns provider support when the admitted
    provider sample that supports the completed accounting — the current
    segment's latest, B — was requested after its ``t_m`` and reads raw at
    or above its value. An older, higher sample the accounting has since
    moved below supports nothing, and neither does the MAX-clamped stored
    value. A pending or retained low, a projection fill, a numerically
    dominating member and unknown evidence never earn. On the five-hour axis
    the accounted value is the `statusline` source's detector baseline under
    the current credit generation and only (i) exists: that baseline holds
    no provider samples."""
    if accounted.axis not in AXES or accounted.percent is None:
        return ()
    segment = [s for s in _in_window(samples, accounted.window)
               if accounted.floor_epoch is None
               or _at_or_after_epoch(s.captured_at_utc, accounted.floor_epoch)]
    generation = tuple((c.event_id, c.event_revision) for c in accounted.credits
                       if c.axis == accounted.axis)
    latest = _latest(segment)
    earned = []
    for m in members:
        s = m.sigma
        if s.axis != accounted.axis or s.window != accounted.window:
            continue
        support = None
        if s.percent == accounted.percent:
            path = "maximal"
        elif accounted.axis == FIVE_HOUR:
            continue
        else:
            support = latest
            if (support is None or s.percent > accounted.percent
                    or float(support.weekly_percent) < s.percent
                    or not after(support.requested_at_utc, s.activity_at_utc)):
                continue
            path = "provider_support"
        earned.append(Receipt(
            sigma=s, path=path, account_key=accounted.account_key,
            window=accounted.window, support_id=accounted.support_id,
            support_hash=accounted.support_hash,
            accounted_percent=float(accounted.percent),
            provider_observation_id=None if support is None else support.observation_id,
            provider_percent=None if support is None else float(support.weekly_percent),
            provider_requested_at_utc=None if support is None else support.requested_at_utc,
            provider_window=None if support is None else _sample_window(support),
            detector_policy=accounted.detector_policy, credits=generation,
            coverage=accounted.coverage, completed_at_utc=completed_at_utc,
            index_identity=index_identity))
    return tuple(earned)


def receipt_valid(receipt: Receipt, transitions: Iterable[CreditTransition], *,
                  index_identity: str, supports: Collection[str] | None = None) -> bool:
    """Whether ``receipt`` stays valid against every current credit of its
    account and window (the weekly rows of the section 4.4 table).

    The index identity must match (a rebuild, re-upgrade or epoch change
    invalidates). Every credit of the receipt's generation must still be
    present at its revision (a corrected or tombstoned event invalidates).
    A credit after the receipt survives only when its causal low is an
    admitted provider sample requested after ``e_σ`` or a status-line reading
    (R15); every other or unordered credit invalidates. When ``supports`` is
    given, the receipt's supporting observation and its provider sample B
    must still be among them (a held, retracted or tombstoned support
    invalidates).

    The five-hour rows: a `statusline`-source credit survives (its causal low
    is the source's own arming observation, R15). A credit confirmed by
    another source survives only when its baseline observation is an
    admitted provider sample requested after ``t_m`` whose RAW five-hour
    value is at least σ's, and its ARMING observation — never the later
    confirming one — is a provider sample requested after ``e_σ``; every
    other or unordered credit invalidates. Supports are not checked on this
    axis: the accounted baseline is detector state, not a stored row."""
    if receipt.sigma.axis not in AXES or receipt.index_identity != index_identity:
        return False
    five = receipt.sigma.axis == FIVE_HOUR
    if supports is not None and not five:
        if receipt.support_id is not None and receipt.support_id not in supports:
            return False
        if (receipt.provider_observation_id is not None
                and receipt.provider_observation_id not in supports):
            return False
    current = {}
    for t in transitions:
        if t.axis == receipt.sigma.axis:
            current[t.event_id] = t
    generation = dict(receipt.credits)
    for event_id, revision in generation.items():
        t = current.get(event_id)
        if t is None or t.event_revision != revision:
            return False
    for event_id, t in current.items():
        if event_id in generation:
            continue
        if t.causal_kind == "statusline":
            continue
        if five:
            if (t.causal_kind == "provider"
                    and t.baseline_percent is not None
                    and float(t.baseline_percent) >= receipt.sigma.percent
                    and after(t.baseline_requested_at_utc,
                              receipt.sigma.activity_at_utc)
                    and after(t.arming_requested_at_utc, receipt.completed_at_utc)):
                continue
            return False
        if t.causal_kind == "provider" and after(t.requested_at_utc,
                                                 receipt.completed_at_utc):
            continue
        return False
    return True


def refusal_facts(axis: str, members: Sequence[Member], samples: Iterable,
                  receipts: Iterable[Receipt], transitions: Iterable[CreditTransition], *,
                  index_identity: str, supports: Collection[str] | None = None
                  ) -> dict[str, MemberFacts]:
    """Per-member facts for one axis, keyed by contributor token.

    A member is refused only when it holds a valid receipt for its exact σ
    and a sample requested after both ``t_m`` and ``e_σ`` proves it stale.
    A member without a receipt is eligible with no proof reported; one whose
    receipt is invalid is eligible with its proof reported. Each axis is
    decided on its own members, samples and credits."""
    if axis not in AXES:
        return {}
    samples = list(samples)
    transitions = list(transitions)
    by_sigma: dict[str, list[Receipt]] = {}
    for r in receipts:
        by_sigma.setdefault(r.sigma.key(), []).append(r)
    facts: dict[str, MemberFacts] = {}
    for m in members:
        s = m.sigma
        if s.axis != axis:
            continue
        held = sorted(by_sigma.get(s.key(), ()),
                      key=lambda r: _instant_order(r.completed_at_utc))
        if not held:
            facts[s.token] = ALL_ELIGIBLE
            continue
        valid = [r for r in held
                 if receipt_valid(r, transitions, index_identity=index_identity,
                                  supports=supports)]
        reset = m.advertised_reset_epoch
        if valid:
            strict = proven_stale(s, samples, after_utc=valid[0].completed_at_utc,
                                  advertised_reset_epoch=reset)
        else:
            strict = None
        proof = strict or proven_stale(s, samples, after_utc=s.activity_at_utc,
                                       advertised_reset_epoch=reset)
        facts[s.token] = MemberFacts(
            refused=bool(valid) and strict is not None,
            proof_observation_id=None if proof is None else proof.observation_id,
            receipt_valid=bool(valid))
    return facts


# --- the receipt map ---------------------------------------------------------------------


def _order(r: Receipt) -> tuple:
    return (_instant_order(r.completed_at_utc), r.sigma.key())


def merge_receipts(existing: Iterable[Receipt], new: Iterable[Receipt]) -> tuple[Receipt, ...]:
    """One receipt per σ, keeping the earliest completion (an earlier ``e_σ``
    refuses sooner; a later one never replaces it)."""
    best: dict[str, Receipt] = {}
    for r in [*existing, *new]:
        key = r.sigma.key()
        if key not in best or _order(r) < _order(best[key]):
            best[key] = r
    return tuple(sorted(best.values(), key=_order))


def prune_receipts(receipts: Iterable[Receipt], transitions: Iterable[CreditTransition], *,
                   index_identity: str, window: int | None,
                   supports: Collection[str] | None = None) -> tuple[Receipt, ...]:
    """The receipts still valid for the current window. An invalidated receipt
    is dropped for good: it is never revived when the invalidating fact goes
    away, only re-earned by new accounting with a new ``e_σ``."""
    transitions = list(transitions)
    return tuple(sorted(
        (r for r in receipts
         if window is not None and r.window == window
         and receipt_valid(r, transitions, index_identity=index_identity,
                           supports=supports)),
        key=_order))


def bounded_receipts(existing: Iterable[Receipt], new: Iterable[Receipt], *,
                     referenced_tokens: Collection[str],
                     fits: Callable[[tuple], bool]) -> tuple[Receipt, ...]:
    """Merge ``new`` into ``existing`` within the document limit ``fits``.

    The oldest receipt that no outstanding work references is evicted first;
    when nothing unreferenced is left, new receipts are omitted (newest
    first) rather than blocking accounting; as a last resort the oldest
    referenced receipts go too. Eviction means eligible."""
    existing = merge_receipts(existing, ())
    keys = {r.sigma.key() for r in existing}
    fresh = [r for r in merge_receipts((), new) if r.sigma.key() not in keys]
    kept_old = list(existing)
    kept_new = list(fresh)

    def current() -> tuple:
        return tuple(sorted([*kept_old, *kept_new], key=_order))

    while not fits(current()):
        evictable = [r for r in kept_old if r.sigma.token not in referenced_tokens]
        if evictable:
            kept_old.remove(min(evictable, key=_order))
        elif kept_new:
            kept_new.remove(max(kept_new, key=_order))
        elif kept_old:
            kept_old.remove(min(kept_old, key=_order))
        else:
            break
    return current()


def latest_per_token(receipts: Iterable[Receipt]) -> tuple[Receipt, ...]:
    """Per contributor token and axis, only the receipts of its latest frozen
    instant. A contributor's provider-activity instant never moves backwards
    (the scanner keeps every instant it observed), so a sample with an older
    ``t_m`` is never presented again; its receipt can authorize nothing."""
    receipts = list(receipts)
    latest: dict[tuple[str, str], int] = {}
    for r in receipts:
        key = (r.sigma.token, r.sigma.axis)
        latest[key] = max(latest.get(key, -1), _instant_order(r.sigma.activity_at_utc))
    return tuple(sorted(
        (r for r in receipts
         if _instant_order(r.sigma.activity_at_utc) == latest[(r.sigma.token, r.sigma.axis)]),
        key=_order))


def floor_microsecond(text: str) -> str | None:
    """A canonical instant truncated to whole microseconds and stepped back
    one, in the provider samples' ``q`` form: every ``q`` proven after
    ``text`` compares strictly greater than it as text."""
    interval = _interval(text)
    if interval is None:
        return None
    import datetime as _dt
    micros = interval[0] // 1000 - 1
    instant = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(
        microseconds=micros)
    return _samples.format_request_started(instant)


# --- the compact sample set ------------------------------------------------------------------


def compact_samples(samples: Iterable, *, window: int,
                    floor_epoch: int | None, axis: str = WEEKLY) -> tuple:
    """A subset of the window's samples that yields the same `proven_stale`
    and `earn_receipts` answers for every member (both pick the latest
    qualifying sample, and qualification is monotone in ``q`` within one
    value): per weekly value, the latest sample and the latest captured at
    or after the floor, plus every sample captured within an hour of the
    window's reset (a member's advertised reset may precede the window).
    ``axis`` selects the window identity and value (five-hour: the canonical
    key and the raw five-hour percent)."""
    margin = int(window) - _RESET_MARGIN_SECONDS
    keep: dict[str, object] = {}
    latest: dict[float, object] = {}
    latest_floor: dict[float, object] = {}
    for s in _axis_window(samples, axis, window):
        if not _before_epoch(s.captured_at_utc, margin):
            keep[s.observation_id] = s
            continue
        v = _axis_value(s, axis)
        latest[v] = _latest([x for x in (latest.get(v), s) if x is not None])
        if floor_epoch is None or _at_or_after_epoch(s.captured_at_utc, floor_epoch):
            latest_floor[v] = _latest([x for x in (latest_floor.get(v), s) if x is not None])
    for s in [*latest.values(), *latest_floor.values()]:
        keep[s.observation_id] = s
    return tuple(sorted(keep.values(),
                        key=lambda s: (_instant_order(s.requested_at_utc), s.observation_id)))


# --- codecs ------------------------------------------------------------------------------------


_SIGMA_KEYS = ("token", "activityAt", "axis", "percent", "window", "evidenceRevision",
               "parserRevision")
_RECEIPT_KEYS = frozenset({
    "sigma", "path", "account", "window", "supportId", "supportHash", "accountedPercent",
    "providerObservationId", "providerPercent", "providerRequestedAt", "providerWindow",
    "detectorPolicy", "credits", "coverage", "completedAt", "indexIdentity"})
_CREDIT_KEYS = frozenset({
    "axis", "eventId", "eventRevision", "causalKind", "requestedAt", "baselineRequestedAt",
    "baselinePercent", "armingRequestedAt"})


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedReceipt("not a number")
    return float(value)


def _opt_number(value) -> float | None:
    return None if value is None else _number(value)


def _int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedReceipt("not an integer")
    return value


def _opt_int(value) -> int | None:
    return None if value is None else _int(value)


def _text(value) -> str:
    if not isinstance(value, str) or not value:
        raise MalformedReceipt("not a string")
    return value


def _opt_text(value) -> str | None:
    return None if value is None else _text(value)


def _instant(value) -> str:
    if _interval(_text(value)) is None:
        raise MalformedReceipt("not a canonical instant")
    return value


def _opt_instant(value) -> str | None:
    return None if value is None else _instant(value)


def _exact(doc, keys) -> dict:
    if not isinstance(doc, dict) or set(doc) != set(keys):
        raise MalformedReceipt("unexpected keys")
    return doc


def _sigma_to_json(s: SampleIdentity) -> dict:
    return {"token": s.token, "activityAt": s.activity_at_utc, "axis": s.axis,
            "percent": s.percent, "window": s.window,
            "evidenceRevision": s.evidence_revision, "parserRevision": s.parser_revision}


def _sigma_from_json(doc) -> SampleIdentity:
    doc = _exact(doc, _SIGMA_KEYS)
    axis = _text(doc["axis"])
    if axis not in AXES:
        raise MalformedReceipt("unknown axis")
    return SampleIdentity(_text(doc["token"]), _instant(doc["activityAt"]), axis,
                          _number(doc["percent"]), _int(doc["window"]),
                          _int(doc["evidenceRevision"]), _int(doc["parserRevision"]))


def receipt_to_json(r: Receipt) -> dict:
    return {
        "sigma": _sigma_to_json(r.sigma), "path": r.path, "account": r.account_key,
        "window": r.window, "supportId": r.support_id, "supportHash": r.support_hash,
        "accountedPercent": r.accounted_percent,
        "providerObservationId": r.provider_observation_id,
        "providerPercent": r.provider_percent,
        "providerRequestedAt": r.provider_requested_at_utc,
        "providerWindow": r.provider_window, "detectorPolicy": r.detector_policy,
        "credits": [[event_id, revision] for event_id, revision in r.credits],
        "coverage": r.coverage, "completedAt": r.completed_at_utc,
        "indexIdentity": r.index_identity,
    }


def receipt_from_json(doc) -> Receipt:
    doc = _exact(doc, _RECEIPT_KEYS)
    path = doc["path"]
    if path not in PATHS:
        raise MalformedReceipt("unknown path")
    credits = doc["credits"]
    if not isinstance(credits, list):
        raise MalformedReceipt("credits")
    pairs = []
    for pair in credits:
        if not isinstance(pair, list) or len(pair) != 2:
            raise MalformedReceipt("credit pair")
        pairs.append((_text(pair[0]), _text(pair[1])))
    r = Receipt(
        sigma=_sigma_from_json(doc["sigma"]), path=path, account_key=_text(doc["account"]),
        window=_int(doc["window"]), support_id=_opt_text(doc["supportId"]),
        support_hash=_opt_text(doc["supportHash"]),
        accounted_percent=_opt_number(doc["accountedPercent"]),
        provider_observation_id=_opt_text(doc["providerObservationId"]),
        provider_percent=_opt_number(doc["providerPercent"]),
        provider_requested_at_utc=_opt_instant(doc["providerRequestedAt"]),
        provider_window=_opt_text(doc["providerWindow"]),
        detector_policy=_opt_int(doc["detectorPolicy"]), credits=tuple(pairs),
        coverage=_opt_text(doc["coverage"]), completed_at_utc=_instant(doc["completedAt"]),
        index_identity=_text(doc["indexIdentity"]))
    if (path == "provider_support") != (r.provider_observation_id is not None):
        raise MalformedReceipt("path and provider support disagree")
    return r


def credit_to_json(c: CreditTransition) -> dict:
    return {"axis": c.axis, "eventId": c.event_id, "eventRevision": c.event_revision,
            "causalKind": c.causal_kind, "requestedAt": c.requested_at_utc,
            "baselineRequestedAt": c.baseline_requested_at_utc,
            "baselinePercent": c.baseline_percent,
            "armingRequestedAt": c.arming_requested_at_utc}


def credit_from_json(doc) -> CreditTransition:
    doc = _exact(doc, _CREDIT_KEYS)
    axis = _text(doc["axis"])
    kind = doc["causalKind"]
    if axis not in AXES or kind not in CAUSAL_KINDS:
        raise MalformedReceipt("credit axis or kind")
    return CreditTransition(
        axis=axis, event_id=_text(doc["eventId"]), event_revision=_text(doc["eventRevision"]),
        causal_kind=kind, requested_at_utc=_opt_instant(doc["requestedAt"]),
        baseline_requested_at_utc=_opt_instant(doc["baselineRequestedAt"]),
        baseline_percent=_opt_number(doc["baselinePercent"]),
        arming_requested_at_utc=_opt_instant(doc["armingRequestedAt"]))


def receipts_by_key(receipts: Iterable[Receipt]) -> Mapping[str, Receipt]:
    return {r.sigma.key(): r for r in receipts}
