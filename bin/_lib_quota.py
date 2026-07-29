"""Pure provider-neutral quota interpretation primitives.

This module deliberately owns no provider I/O.  Adapters turn physical cache
rows into :class:`QuotaObservation` objects; this kernel then keeps the logical
identity, history selection, freshness, blocks, forecasts, crossings, and
threshold decisions deterministic.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Iterable, Literal

import _lib_accounts


FUTURE_CLOCK_SKEW_SECONDS = 300
_SOURCE_PATH_KEY_PREFIX = b"cctally-source-path-v1\0"
_RULE_FINGERPRINT_PREFIX = b"cctally-quota-alert-rule-v1\0"

FreshnessState = Literal["fresh", "stale", "future", "unavailable"]
ForecastStatus = Literal[
    "ok", "insufficient-history", "unavailable", "stale", "future",
]
ThresholdKind = Literal["actual", "projected"]


def _require_aware(value: dt.datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _integer_percent(value: float) -> int:
    """Return a percent HWM without exposing binary float-floor drift."""
    return math.floor(value + 1e-9)


# --------------------------------------------------------------------------
# #416 spec §4.2 — tolerance-anchored reset canonicalization (decision D4).
#
# The Codex quota path never received the jitter canonicalization the Claude 5h
# path has, so the RAW `resets_at` enters the window identity and one physical
# window splits into many, each with its own peak and milestone ladder (spec
# §1.4: one reset-minute observed split six ways).
#
# The tolerance is safe by construction: the next GENUINE reset of a 5h window
# is five hours out and of a weekly window seven days out, so nothing within
# 600s of an established anchor can be a different cycle.
#
# This kernel is pure and takes the established anchor set as an argument,
# because resolution must happen at INGEST over the complete population (spec
# §4.1 / review F7). A read-time anchor is deterministic only for a fixed input
# population, and the dashboard reads at most 35 days / 1,000 rows with those
# bounds applied in SQL — so omitting the earliest member of a jitter cluster
# would silently move its anchor and make the dashboard and the CLI disagree
# about window identity.
# --------------------------------------------------------------------------

CODEX_RESET_ANCHOR_TOLERANCE_SECONDS = 600


def resolve_reset_anchor(
    anchors: Iterable[dt.datetime], raw_reset: dt.datetime,
    *, tolerance_seconds: int = CODEX_RESET_ANCHOR_TOLERANCE_SECONDS,
) -> dt.datetime:
    """Return the anchor ``raw_reset`` joins, or ``raw_reset`` itself when it
    establishes a new one.

    ``anchors`` are the anchors already established for this observation's
    identity (the identity MINUS the reset and MINUS the account — see
    ``_physical_window_key``, which excludes the account precisely so an
    unidentified observation can be adopted by a same-window identified one).

    First sight wins and an anchor NEVER moves: the returned value is always an
    anchor already in ``anchors`` or the observation's own raw value, never a
    recomputed centroid. Assignment picks the NEAREST anchor within tolerance,
    breaking a tie on the earlier anchor, so for a given anchor set the answer
    does not depend on the order the anchors were established in. (The anchor
    SET can still depend on arrival order in the pathological case of a chain of
    observations each within tolerance of its neighbour but not of the first;
    real jitter is seconds wide, so a real cluster collapses to one anchor under
    every order.)
    """
    _require_aware(raw_reset, "raw_reset")
    if (isinstance(anchors, ResetAnchorIndex)
            and anchors.tolerance_seconds == tolerance_seconds):
        # O(1) via the bucketed index; the linear body below stays the
        # reference semantics for a caller holding a plain sequence, and
        # `ResetAnchorIndex.resolve` is pinned against it by an equivalence
        # test over a randomized population.
        return anchors.resolve(raw_reset)
    return _resolve_reset_anchor_linear(
        anchors, raw_reset, tolerance_seconds=tolerance_seconds)


def _resolve_reset_anchor_linear(
    anchors: Iterable[dt.datetime], raw_reset: dt.datetime,
    *, tolerance_seconds: int,
) -> dt.datetime:
    best: dt.datetime | None = None
    best_delta: float | None = None
    for anchor in anchors:
        _require_aware(anchor, "anchor")
        delta = abs((raw_reset - anchor).total_seconds())
        if delta > tolerance_seconds:
            continue
        if best is None or delta < best_delta or (
            delta == best_delta and anchor < best
        ):
            best, best_delta = anchor, delta
    return raw_reset if best is None else best


class ResetAnchorIndex:
    """An established-anchor set with O(1) ``resolve``.

    Semantically identical to ``resolve_reset_anchor`` over the same anchors —
    nearest within tolerance, ties broken on the earlier anchor, first sight
    wins, the anchor never moves, and the pathological chain-of-neighbours
    order-dependence that docstring documents is preserved exactly, because the
    anchor SET is still whatever the caller established in whatever order.

    Only the LOOKUP changes. The linear scan did a full ``datetime`` subtraction
    against every established anchor, and a 5h group accumulates ~1,750 anchors
    a year — so the scan is quadratic in the population. That is not a
    background cost: cache migration 032 runs the resolver over the WHOLE
    ``quota_window_snapshots`` table synchronously on the first DB open after
    upgrade, and ``cache-sync --rebuild`` runs it over the whole walk, so the
    quadratic term lands on the operator's first post-upgrade command as a
    multi-minute hang.

    Anchors are bucketed by ``floor(epoch / tolerance)``. Anything within
    ``tolerance`` of a probe lies in the probe's own bucket or one on either
    side, so three bucket lookups are EXHAUSTIVE — this is an exact index, not
    an approximation, and no correctness argument depends on the bucket width
    beyond that identity.
    """

    __slots__ = ("_tolerance", "_buckets", "_order")

    def __init__(
        self, anchors: Iterable[dt.datetime] = (),
        *, tolerance_seconds: int = CODEX_RESET_ANCHOR_TOLERANCE_SECONDS,
    ) -> None:
        if not isinstance(tolerance_seconds, int) or isinstance(tolerance_seconds, bool):
            raise ValueError("tolerance_seconds must be an int")
        if tolerance_seconds <= 0:
            raise ValueError("tolerance_seconds must be positive")
        self._tolerance = tolerance_seconds
        self._buckets: dict[int, list[dt.datetime]] = {}
        self._order: list[dt.datetime] = []
        for anchor in anchors:
            self.add(anchor)

    @property
    def tolerance_seconds(self) -> int:
        return self._tolerance

    def _bucket(self, value: dt.datetime) -> int:
        return int(value.timestamp() // self._tolerance)

    def add(self, anchor: dt.datetime) -> bool:
        """Establish ``anchor``. Returns False when it was already present."""
        _require_aware(anchor, "anchor")
        bucket = self._buckets.setdefault(self._bucket(anchor), [])
        if anchor in bucket:
            return False
        bucket.append(anchor)
        self._order.append(anchor)
        return True

    def resolve(self, raw_reset: dt.datetime) -> dt.datetime:
        """The anchor ``raw_reset`` joins, or ``raw_reset`` when it establishes
        a new one. Does NOT add it — the caller decides, exactly as with the
        pure function."""
        _require_aware(raw_reset, "raw_reset")
        probe = self._bucket(raw_reset)
        best: dt.datetime | None = None
        best_delta: float | None = None
        for offset in (-1, 0, 1):
            for anchor in self._buckets.get(probe + offset, ()):
                delta = abs((raw_reset - anchor).total_seconds())
                if delta > self._tolerance:
                    continue
                if best is None or delta < best_delta or (
                    delta == best_delta and anchor < best
                ):
                    best, best_delta = anchor, delta
        return raw_reset if best is None else best

    def __contains__(self, anchor: object) -> bool:
        if not isinstance(anchor, dt.datetime):
            return False
        return anchor in self._buckets.get(self._bucket(anchor), ())

    def __iter__(self):
        """Established anchors in the order they were established."""
        return iter(self._order)

    def __len__(self) -> int:
        return len(self._order)


@dataclass(frozen=True)
class QuotaWindowIdentity:
    """One root-qualified native quota window identity.

    ``limit_id`` and ``limit_name`` are provider display metadata.  They are
    intentionally excluded from equality and hashing: labels never merge root,
    slot, duration, or logical-limit identities, and a label-only change never
    re-arms alert rules.  They remain present on observations so a metadata
    change remains an interpreted history point.
    """

    source: str
    source_root_key: str
    logical_limit_key: str
    observed_slot: str
    window_minutes: int
    # account_key (#341) participates in equality/hashing so never-combine
    # extends to accounts automatically: two accounts sharing one physical
    # window key are distinct identities. It defaults to the reserved sentinel so
    # a single-account / pre-#341 caller building an identity by keyword is
    # byte-stable. limit_id/limit_name remain compare=False display metadata.
    account_key: str = _lib_accounts.UNATTRIBUTED
    limit_id: str | None = field(default=None, compare=False)
    limit_name: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        for name in ("source", "source_root_key", "logical_limit_key", "observed_slot",
                     "account_key"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.window_minutes, int) or isinstance(self.window_minutes, bool):
            raise ValueError("window_minutes must be a positive integer")
        if self.window_minutes <= 0:
            raise ValueError("window_minutes must be a positive integer")


@dataclass(frozen=True)
class QuotaObservation:
    """One validated physical observation after provider-specific parsing."""

    identity: QuotaWindowIdentity
    captured_at: dt.datetime
    used_percent: float
    resets_at: dt.datetime
    source_path: str
    line_offset: int
    plan_type: str | None = None
    individual_limit_json: str | None = None
    reached_type: str | None = None
    # #416 spec §4.1/§4.2: the tolerance-anchored reset resolved at INGEST over
    # the complete population and stored on the cache row. ``resets_at`` stays
    # the RAW provider value, retained unchanged as evidence.
    #
    # Every reset-identity consumer reads THIS field, not ``resets_at``, and
    # ``__post_init__`` fills it from ``resets_at`` when the caller has none —
    # so a pre-migration row, an older binary's row, or any construction site
    # that predates the column behaves exactly as it does today rather than
    # failing. That default is what makes "route every consumer through the
    # anchor" a safe blanket rule instead of a per-call-site judgement.
    canonical_resets_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.captured_at, "captured_at")
        _require_aware(self.resets_at, "resets_at")
        if self.canonical_resets_at is None:
            object.__setattr__(self, "canonical_resets_at", self.resets_at)
        else:
            _require_aware(self.canonical_resets_at, "canonical_resets_at")
        if not isinstance(self.used_percent, (int, float)) or isinstance(self.used_percent, bool):
            raise ValueError("used_percent must be a number")
        if not math.isfinite(self.used_percent) or not 0 <= self.used_percent <= 100:
            raise ValueError("used_percent must be between 0 and 100")
        if not isinstance(self.source_path, str) or not self.source_path.startswith("/"):
            raise ValueError("source_path must be a canonical absolute path")
        if not isinstance(self.line_offset, int) or isinstance(self.line_offset, bool) or self.line_offset < 0:
            raise ValueError("line_offset must be a non-negative integer")

    @property
    def captured_at_utc(self) -> dt.datetime:
        """Compatibility spelling for physical-cache adapter code."""
        return self.captured_at

    @property
    def resets_at_utc(self) -> dt.datetime:
        """Compatibility spelling for physical-cache adapter code."""
        return self.resets_at


@dataclass(frozen=True)
class QuotaHistory:
    """All physical evidence and deduplicated interpreted points for one identity."""

    identity: QuotaWindowIdentity
    physical_observations: tuple[QuotaObservation, ...]
    observations: tuple[QuotaObservation, ...]


@dataclass(frozen=True)
class QuotaBlock:
    """A native reset block scoped to exactly one logical quota identity."""

    identity: QuotaWindowIdentity
    resets_at: dt.datetime
    nominal_start_at: dt.datetime
    observations: tuple[QuotaObservation, ...]
    first_observed_at: dt.datetime
    last_observed_at: dt.datetime
    first_percent: float
    current_percent: float


@dataclass(frozen=True)
class QuotaPercentMilestone:
    """The first observed physical crossing for an integer percent HWM."""

    percent: int
    captured_at: dt.datetime
    observation: QuotaObservation


@dataclass(frozen=True)
class QuotaFreshness:
    """Freshness of locally retained physical evidence, not provider-live state."""

    state: FreshnessState
    captured_at: dt.datetime | None
    age_seconds: int | None
    stale_after_seconds: int | None
    source: str = "local-rollout"


@dataclass(frozen=True)
class QuotaForecast:
    """One reset-scoped duration-agnostic quota forecast."""

    status: ForecastStatus
    current_percent: float | None
    rate_percent_per_hour: float | None
    projected_percent: float | None
    resets_at: dt.datetime | None
    remaining_seconds: int | None
    sample_count: int
    sample_span_seconds: int
    confidence: Literal["high", "medium", "low"] | None


@dataclass(frozen=True)
class QuotaRule:
    """An exact source/root/logical-limit threshold override."""

    source: str
    source_root_key: str
    logical_limit_key: str
    actual_thresholds: tuple[int, ...]
    projected_thresholds: tuple[int, ...]

    def __post_init__(self) -> None:
        for name in ("source", "source_root_key", "logical_limit_key"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(self, "actual_thresholds", validate_thresholds(self.actual_thresholds))
        object.__setattr__(self, "projected_thresholds", validate_thresholds(self.projected_thresholds))


@dataclass(frozen=True)
class ResolvedQuotaRule:
    """Thresholds selected for one identity before durable alert evaluation."""

    actual_thresholds: tuple[int, ...]
    projected_thresholds: tuple[int, ...]


@dataclass(frozen=True)
class QuotaThresholdDecision:
    """A threshold currently satisfied by actual or projected quota usage."""

    kind: ThresholdKind
    threshold: int


def identity_sort_key(identity: QuotaWindowIdentity) -> tuple[str, str, str, str, int, str]:
    """Return the stable full logical identity ordering used by every selector.

    ``account_key`` (#341) is appended LAST so the existing 5-element ordering is
    preserved byte-for-byte for a single-account install (the account is constant
    there) and only ever breaks a genuine cross-account tie deterministically.
    """
    return (
        identity.source,
        identity.source_root_key,
        identity.logical_limit_key,
        identity.observed_slot,
        identity.window_minutes,
        identity.account_key,
    )


def physical_order_key(observation: QuotaObservation) -> tuple[dt.datetime, dt.datetime, str, int]:
    """The frozen total order for physical rows within one identity.

    The reset component is the CANONICAL anchor (#416 §4.1), so two captures of
    one physical window whose raw resets differ only by provider jitter tie here
    and fall through to the deterministic physical position
    (``source_path``, ``line_offset``) instead of being ordered by the jitter.
    """
    return (
        observation.captured_at,
        observation.canonical_resets_at,
        observation.source_path,
        observation.line_offset,
    )


def logical_value_tuple(observation: QuotaObservation) -> tuple[object, ...]:
    """The exact interpreted-point duplicate tuple from the approved design."""
    identity = observation.identity
    return (
        identity.source,
        identity.source_root_key,
        identity.logical_limit_key,
        identity.observed_slot,
        identity.window_minutes,
        identity.limit_id,
        identity.limit_name,
        observation.used_percent,
        # CANONICAL, not raw (#416 §4.1): the reset is part of the interpreted
        # point's value, so raw jitter would make one unchanged reading look
        # like a run of distinct interpreted points and defeat the dedup.
        observation.canonical_resets_at,
        observation.plan_type,
        observation.individual_limit_json,
        observation.reached_type,
    )


def source_path_key(source_path: str) -> str:
    """Return the privacy-safe stable source path key mandated for quota JSON."""
    if not isinstance(source_path, str) or not source_path.startswith("/"):
        raise ValueError("source_path must be a canonical absolute path")
    return hashlib.sha256(_SOURCE_PATH_KEY_PREFIX + source_path.encode("utf-8")).hexdigest()[:32]


def build_history(observations: Iterable[QuotaObservation]) -> tuple[QuotaHistory, ...]:
    """Partition physical observations and collapse exact logical duplicates.

    The first row in the total physical order represents an exact duplicate
    run.  All rows remain in ``physical_observations`` so freshness can use the
    last physical capture even when history has only one interpreted point.
    """
    by_identity: dict[QuotaWindowIdentity, list[QuotaObservation]] = {}
    for observation in observations:
        by_identity.setdefault(observation.identity, []).append(observation)

    result: list[QuotaHistory] = []
    for identity in sorted(by_identity, key=identity_sort_key):
        physical = tuple(sorted(by_identity[identity], key=physical_order_key))
        interpreted: list[QuotaObservation] = []
        previous_value: tuple[object, ...] | None = None
        for observation in physical:
            value = logical_value_tuple(observation)
            if not interpreted or value != previous_value:
                interpreted.append(observation)
            previous_value = value
        result.append(QuotaHistory(
            identity=identity,
            physical_observations=physical,
            observations=tuple(interpreted),
        ))
    return tuple(result)


def _physical_window_key(observation: QuotaObservation) -> tuple[object, ...]:
    """The account-INDEPENDENT physical window key used by the continuity fold.

    Two observations share a physical window iff they agree on root, limit key,
    slot, window minutes, and the reset boundary — the account is deliberately
    EXCLUDED so unidentified observations can be adopted by a same-window
    identified account (spec §2 window-account continuity).

    The reset is the CANONICAL anchor (#416 §4.1). With the raw value, jitter
    ALONE defeats continuity adoption: an unidentified observation a few seconds
    off the identified one is a different physical window and is never adopted.
    """
    identity = observation.identity
    return (
        identity.source,
        identity.source_root_key,
        identity.logical_limit_key,
        identity.observed_slot,
        identity.window_minutes,
        observation.canonical_resets_at,
    )


def adopt_unidentified_observations(
    observations: Iterable[QuotaObservation],
) -> tuple[QuotaObservation, ...]:
    """Apply the window-account continuity rule (#341 spec §2 rev 4).

    Group observations by their physical window key (account EXCLUDED). Within a
    group, identified observations (``account_key != unattributed``) are
    AUTHORITATIVE and NEVER re-assigned — two identified accounts sharing an
    identical physical window key stay separate windows (never-combine holds
    unconditionally). Unidentified observations are adopted by the window's
    account IFF exactly ONE identified account is ever observed for that key;
    zero or ambiguous (>=2) identified accounts leave them ``unattributed``.

    Pure and order-preserving: identified observations pass through untouched; an
    adopted unidentified observation is returned with its identity re-stamped to
    the single identified account. A single-account install (all observations
    already carry one account, or all are unattributed with no identified peer)
    is a byte-stable no-op.
    """
    values = tuple(observations)
    identified_by_window: dict[tuple[object, ...], set[str]] = {}
    for observation in values:
        account = observation.identity.account_key
        if account != _lib_accounts.UNATTRIBUTED:
            identified_by_window.setdefault(
                _physical_window_key(observation), set()
            ).add(account)
    result: list[QuotaObservation] = []
    for observation in values:
        if observation.identity.account_key != _lib_accounts.UNATTRIBUTED:
            result.append(observation)
            continue
        accounts = identified_by_window.get(_physical_window_key(observation))
        if accounts is not None and len(accounts) == 1:
            adopted = next(iter(accounts))
            result.append(replace(
                observation, identity=replace(observation.identity, account_key=adopted),
            ))
        else:
            result.append(observation)
    return tuple(result)


def latest_physical_observation(observations: Iterable[QuotaObservation]) -> QuotaObservation | None:
    """Select the latest local physical capture with deterministic tie breaking."""
    values = tuple(observations)
    return max(values, key=physical_order_key) if values else None


def _single_identity_observations(
    observations: Iterable[QuotaObservation],
) -> tuple[QuotaObservation, ...]:
    values = tuple(observations)
    if len({observation.identity for observation in values}) > 1:
        raise ValueError("selector requires observations for exactly one quota identity")
    return values


def select_baseline(
    observations: Iterable[QuotaObservation], as_of: dt.datetime,
) -> QuotaObservation | None:
    """Return the last ordered observation that was already captured at ``as_of``."""
    _require_aware(as_of, "as_of")
    values = _single_identity_observations(observations)
    eligible = [
        observation for observation in values
        if observation.captured_at <= as_of
    ]
    return max(eligible, key=physical_order_key) if eligible else None


def build_blocks(observations: Iterable[QuotaObservation]) -> tuple[QuotaBlock, ...]:
    """Segment deduplicated interpreted history at each native reset boundary.

    Blocks are keyed on the CANONICAL anchor (#416 §4.1), so one physical window
    is one block however many raw reset spellings its observations carry — and
    ``QuotaBlock.resets_at``, which every renderer shows, is that same anchor.
    """
    by_block: dict[tuple[QuotaWindowIdentity, dt.datetime], list[QuotaObservation]] = {}
    for history in build_history(observations):
        for observation in history.observations:
            by_block.setdefault(
                (history.identity, observation.canonical_resets_at), []
            ).append(observation)

    blocks: list[QuotaBlock] = []
    for (identity, resets_at), points in sorted(
        by_block.items(), key=lambda item: (identity_sort_key(item[0][0]), item[0][1]),
    ):
        ordered = tuple(sorted(points, key=physical_order_key))
        first = ordered[0]
        last = ordered[-1]
        blocks.append(QuotaBlock(
            identity=identity,
            resets_at=resets_at,
            nominal_start_at=resets_at - dt.timedelta(minutes=identity.window_minutes),
            observations=ordered,
            first_observed_at=first.captured_at,
            last_observed_at=last.captured_at,
            first_percent=first.used_percent,
            current_percent=last.used_percent,
        ))
    return tuple(blocks)


def percent_milestones(block: QuotaBlock) -> tuple[QuotaPercentMilestone, ...]:
    """Return post-baseline integer crossings using a non-decreasing HWM."""
    observations = iter(block.observations)
    first = next(observations, None)
    if first is None:
        return ()
    high_water = _integer_percent(first.used_percent)
    milestones: list[QuotaPercentMilestone] = []
    for observation in observations:
        current = _integer_percent(observation.used_percent)
        if current <= high_water:
            continue
        for percent in range(high_water + 1, current + 1):
            milestones.append(QuotaPercentMilestone(
                percent=percent,
                captured_at=observation.captured_at,
                observation=observation,
            ))
        high_water = current
    return tuple(milestones)


def stale_after_seconds(window_minutes: int) -> int:
    """Return the native-duration freshness bound frozen by the S2 design."""
    if not isinstance(window_minutes, int) or isinstance(window_minutes, bool) or window_minutes <= 0:
        raise ValueError("window_minutes must be a positive integer")
    return max(900, min((window_minutes * 60) // 10, 3600))


def quota_freshness(
    observations: Iterable[QuotaObservation], as_of: dt.datetime,
) -> QuotaFreshness:
    """Classify latest physical local-rollout evidence independently of baseline use."""
    _require_aware(as_of, "as_of")
    latest = latest_physical_observation(_single_identity_observations(observations))
    if latest is None:
        return QuotaFreshness(
            state="unavailable", captured_at=None, age_seconds=None,
            stale_after_seconds=None,
        )
    age_seconds = int((as_of - latest.captured_at).total_seconds())
    stale_after = stale_after_seconds(latest.identity.window_minutes)
    if age_seconds < -FUTURE_CLOCK_SKEW_SECONDS:
        state: FreshnessState = "future"
    elif age_seconds > stale_after:
        state = "stale"
    else:
        state = "fresh"
    return QuotaFreshness(
        state=state,
        captured_at=latest.captured_at,
        age_seconds=age_seconds,
        stale_after_seconds=stale_after,
    )


def _null_forecast(status: ForecastStatus) -> QuotaForecast:
    return QuotaForecast(
        status=status,
        current_percent=None,
        rate_percent_per_hour=None,
        projected_percent=None,
        resets_at=None,
        remaining_seconds=None,
        sample_count=0,
        sample_span_seconds=0,
        confidence=None,
    )


def _single_identity_history(observations: Iterable[QuotaObservation]) -> QuotaHistory | None:
    history = build_history(observations)
    if not history:
        return None
    if len(history) != 1:
        raise ValueError("forecast requires observations for exactly one quota identity")
    return history[0]


def forecast_quota(
    observations: Iterable[QuotaObservation], as_of: dt.datetime,
) -> QuotaForecast:
    """Forecast reset-scoped quota use from elapsed native-cycle pace."""
    _require_aware(as_of, "as_of")
    history = _single_identity_history(observations)
    if history is None:
        return _null_forecast("unavailable")

    freshness = quota_freshness(history.physical_observations, as_of)
    baseline = select_baseline(history.observations, as_of)
    if baseline is None:
        return _null_forecast("future" if freshness.state == "future" else "unavailable")

    # Same-cycle selection on the CANONICAL anchor (#416 §4.1). With the raw
    # value, a jittered cycle's evidence splits and the forecast silently runs
    # on a fraction of the samples it should have.
    points = tuple(
        observation for observation in history.observations
        if observation.canonical_resets_at == baseline.canonical_resets_at
        and observation.captured_at <= as_of
    )
    points = tuple(sorted(points, key=physical_order_key))
    sample_count = 0
    for prior, current in zip(points, points[1:]):
        elapsed_seconds = (current.captured_at - prior.captured_at).total_seconds()
        delta_percent = current.used_percent - prior.used_percent
        if elapsed_seconds > 0 and delta_percent > 0:
            sample_count += 1

    # The cycle geometry rides the same anchor, so the countdown, the elapsed
    # fraction, and the reported reset all describe ONE window rather than
    # whichever jittered spelling the baseline observation happened to carry.
    anchor = baseline.canonical_resets_at
    remaining_seconds = max(0, int((anchor - as_of).total_seconds()))
    native_window_seconds = baseline.identity.window_minutes * 60
    cycle_start = anchor - dt.timedelta(minutes=baseline.identity.window_minutes)
    cycle_elapsed_seconds = min(
        float(native_window_seconds),
        max(0.0, (as_of - cycle_start).total_seconds()),
    )
    if sample_count > 0 and cycle_elapsed_seconds > 0:
        rate = baseline.used_percent / (cycle_elapsed_seconds / 3600)
        projected = min(100.0, max(
            baseline.used_percent,
            baseline.used_percent + rate * (remaining_seconds / 3600),
        ))
        if sample_count >= 4 and cycle_elapsed_seconds >= native_window_seconds * 0.25:
            confidence: Literal["high", "medium", "low"] = "high"
        elif sample_count >= 2 and cycle_elapsed_seconds >= native_window_seconds * 0.10:
            confidence = "medium"
        else:
            confidence = "low"
    else:
        rate = None
        projected = None
        confidence = None

    if freshness.state == "future":
        status: ForecastStatus = "future"
    elif freshness.state == "stale":
        status = "stale"
    elif sample_count == 0:
        status = "insufficient-history"
    else:
        status = "ok"
    return QuotaForecast(
        status=status,
        current_percent=baseline.used_percent,
        rate_percent_per_hour=rate,
        projected_percent=projected,
        resets_at=anchor,
        remaining_seconds=remaining_seconds,
        sample_count=sample_count,
        sample_span_seconds=int(cycle_elapsed_seconds) if sample_count > 0 else 0,
        confidence=confidence,
    )


def validate_thresholds(values: Iterable[int]) -> tuple[int, ...]:
    """Validate the frozen ordered-unique quota threshold grammar."""
    result = tuple(values)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in result):
        raise ValueError("thresholds must be ordered unique integers from 1 through 100")
    if any(value < 1 or value > 100 for value in result):
        raise ValueError("thresholds must be integers from 1 through 100")
    if tuple(sorted(set(result))) != result:
        raise ValueError("thresholds must be ordered unique integers from 1 through 100")
    return result


def resolve_quota_rule(
    identity: QuotaWindowIdentity,
    *,
    default_actual_thresholds: Iterable[int],
    default_projected_thresholds: Iterable[int],
    rules: Iterable[QuotaRule],
) -> ResolvedQuotaRule:
    """Resolve one exact root-qualified override or the validated defaults."""
    defaults = ResolvedQuotaRule(
        actual_thresholds=validate_thresholds(default_actual_thresholds),
        projected_thresholds=validate_thresholds(default_projected_thresholds),
    )
    matches = [
        rule for rule in rules
        if (rule.source, rule.source_root_key, rule.logical_limit_key) == (
            identity.source, identity.source_root_key, identity.logical_limit_key,
        )
    ]
    if len(matches) > 1:
        raise ValueError("quota rules must be unique by source, source_root_key, and logical_limit_key")
    if not matches:
        return defaults
    match = matches[0]
    return ResolvedQuotaRule(
        actual_thresholds=match.actual_thresholds,
        projected_thresholds=match.projected_thresholds,
    )


def quota_rule_fingerprint(
    identity: QuotaWindowIdentity,
    resolved: ResolvedQuotaRule,
    *,
    global_enabled: bool,
    quota_enabled: bool,
) -> str:
    """Return the exact canonical resolved-rule SHA-256 fingerprint."""
    actual = validate_thresholds(resolved.actual_thresholds)
    projected = validate_thresholds(resolved.projected_thresholds)
    payload = {
        "actualThresholds": list(actual),
        "globalEnabled": global_enabled,
        "identity": {
            "logicalLimitKey": identity.logical_limit_key,
            "source": identity.source,
            "sourceRootKey": identity.source_root_key,
        },
        "projectedThresholds": list(projected),
        "quotaEnabled": quota_enabled,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(_RULE_FINGERPRINT_PREFIX + encoded).hexdigest()


def quota_threshold_decisions(
    *,
    current_percent: float | None,
    projected_percent: float | None,
    actual_thresholds: Iterable[int],
    projected_thresholds: Iterable[int],
) -> tuple[QuotaThresholdDecision, ...]:
    """Return one fire-once candidate per threshold, preferring actual claims."""
    actual = validate_thresholds(actual_thresholds)
    projected = validate_thresholds(projected_thresholds)
    decisions: list[QuotaThresholdDecision] = []
    claimed: set[int] = set()
    if current_percent is not None:
        for threshold in actual:
            if current_percent >= threshold:
                decisions.append(QuotaThresholdDecision(kind="actual", threshold=threshold))
                claimed.add(threshold)
    if projected_percent is not None:
        for threshold in projected:
            if projected_percent >= threshold and threshold not in claimed:
                decisions.append(QuotaThresholdDecision(kind="projected", threshold=threshold))
    return tuple(decisions)
