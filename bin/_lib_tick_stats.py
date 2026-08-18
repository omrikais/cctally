"""Always-on record of what a dashboard tick cost (issue #583, S1 spec §1).

Stdlib-only leaf module. It imports nothing from cctally, so
``bin/_cctally_tui.py``, ``bin/_cctally_dashboard.py``,
``bin/cctally-snapshot-measure`` and the ``dashboard-perf`` reader can all
reach it without a back-import into the dashboard module and without putting
ownership of the record inside ``bin/_lib_snapshot_cache.py``.

This is deliberately NOT an extension of ``bin/_lib_perf.py``. That module is
an opt-in diagnostic with a thread-local phase stack, unstable names and a
lifecycle that begins and ends inside one build. This one is always on, keeps
cross-thread counters, and survives across builds.

Concurrency (spec §1.1). Every update takes the single module-level lock,
constructs the COMPLETE replacement ``StatsSnapshot`` under it, and rebinds
``_STATE`` once. Readers return the current ``_STATE`` and take no lock. The
bare-rebind discipline of ``_lib_perf._LAST_BACKEND_PERF`` is not reused here:
that slot is written by whole replacement, so GIL atomicity is enough, while a
counter update is read-modify-write and would lose increments under the same
pattern.

Bounds: at most ``RING_CAPACITY`` retained records and at most
``MEMORY_BUDGET_BYTES`` of owned state. Both are asserted by
``tests/test_tick_stats.py``.
"""
from __future__ import annotations

import dataclasses
import sys
import threading
import time
import types

RING_CAPACITY = 64
MEMORY_BUDGET_BYTES = 65536

#: `published_at` is the only variable-length field in a record, so it is the
#: only one that can put the whole state over `MEMORY_BUDGET_BYTES`. Measured:
#: the stored field set is 27,189 bytes over a full ring, and 64 distinct 4 KiB
#: strings would be 283,039 — 4.3x over budget. All three production callers
#: pass a UTC ISO-8601 instant (32 characters), so this is not reachable today;
#: the cap makes the budget hold by CONSTRUCTION rather than by convention.
PUBLISHED_AT_MAX_CHARS = 64

#: The three mutually exclusive dispatch outcomes of one outer refresh (§1.4).
DISPATCH_KINDS = ("idle", "full", "degraded")
#: The three Group A bucket builders whose cache opens can fail silently (§1.6).
CACHE_OPEN_FAILURE_KINDS = ("daily", "weekly", "monthly")
#: The realised Codex source-leg regime, aggregated over the refresh (§1.5).
CODEX_REGIMES = ("active", "idle", "not_observed")
#: What the tick published. Metadata, never a dispatch category (§1.4).
PUBLICATIONS = ("final", "partial", "seed", "degraded")

#: The conversation sync loop's three outcomes (#583 S4 §6). A CLOSED set: the
#: recorder normalizes to these, so no error text, path, or other
#: caller-supplied string can reach module state or the debug endpoint. The
#: validation lives in the recorder rather than in the dataclass because a
#: frozen field typed `str` enforces nothing on its own.
CONVERSATION_STATUSES = ("ok", "store_unavailable", "error")

_INGEST = "ingest"
_BUILD = "build"


@dataclasses.dataclass(frozen=True, slots=True)
class TickRecord:
    """One completed tick. Fixed-size scalars and enum strings only — these
    field names are also the wire names on ``/api/debug/backend`` (§3.1)."""

    seq: int
    started_ns: int
    ended_ns: int
    duration_ns: int
    ingest_ran: bool
    ingest_ns: int
    builder_ns: int
    dispatch: str
    codex_regime: str
    publication: str
    cold: bool
    published_ns: int
    published_at: str
    period_ns: "int | None"
    cache_pin_ns: int

    def as_wire(self) -> dict:
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}


@dataclasses.dataclass(frozen=True, slots=True)
class ConversationSyncRecord:
    """One completed conversation sync pass (#583 S4).

    Fixed-size scalars and one validated enum string; these field names are
    also the wire names on ``/api/debug/backend``. A SECOND ring rather than a
    mixed record kind: mixing conversation passes into ``records`` would evict
    main-tick samples and corrupt every aggregate computed over them.

    ``period_ns`` is the FORWARD interval, ``start[i+1] - start[i]``: the gap
    from this pass's own start to the next pass's start. It is therefore
    ``None`` on the newest record until the following pass records, which
    stamps it. Pairing a pass's CPU with the interval that PRECEDED it instead
    shifts the denominator by one pass, and that ratio has no upper bound — a
    long pass following short ones is charged against a short interval and the
    published share exceeds 100%, and exceeds the 50% ceiling the loop's duty
    bound guarantees.

    ``TickRecord.period_ns`` is the same name for the OPPOSITE convention: it
    is the BACKWARD publish interval, so the FIRST tick record carries ``None``
    while here the NEWEST conversation record does. Both ride one
    ``/api/debug/backend`` response, so do not transplant a summarizer between
    the two rings without re-deriving which end is null.
    """

    seq: int
    started_ns: int
    ended_ns: int
    duration_ns: int
    cpu_ns: int
    period_ns: "int | None"
    status: str

    def as_wire(self) -> dict:
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}


@dataclasses.dataclass(frozen=True, slots=True)
class StatsSnapshot:
    """An immutable whole-state read. Rebound as one object, never mutated."""

    dispatch_counts: "types.MappingProxyType[str, int]"
    cache_open_failures: "types.MappingProxyType[str, int]"
    tick_seq: int
    records: "tuple[TickRecord, ...]"
    standalone: "TickRecord | None"
    conversation_records: "tuple[ConversationSyncRecord, ...]" = ()


def _frozen_counts(kinds, source=None) -> "types.MappingProxyType[str, int]":
    base = {k: (source[k] if source else 0) for k in kinds}
    return types.MappingProxyType(base)


_EMPTY = StatsSnapshot(
    dispatch_counts=_frozen_counts(DISPATCH_KINDS),
    cache_open_failures=_frozen_counts(CACHE_OPEN_FAILURE_KINDS),
    tick_seq=0,
    records=(),
    standalone=None,
    conversation_records=(),
)

_LOCK = threading.Lock()
_STATE = _EMPTY
_tls = threading.local()


def snapshot() -> StatsSnapshot:
    """The current whole state. Lock-free: ``_STATE`` is only ever rebound."""
    return _STATE


def reset_for_tests() -> None:
    global _STATE
    with _LOCK:
        _STATE = _EMPTY
    _tls.tick = None


def current() -> "TickContext | None":
    """The tick context open on THIS thread, or None.

    ``_tui_build_snapshot`` consults this to decide whether to open a
    standalone context: inside a dashboard refresh it must not, or an A2
    partial build would be recorded as a second tick (§1.2).
    """
    return getattr(_tls, "tick", None)


def note_cache_open_failure(kind: str) -> None:
    """Count one silent Group A cache-open failure (§1.6)."""
    if kind not in CACHE_OPEN_FAILURE_KINDS:
        raise ValueError(f"unknown cache-open failure kind: {kind!r}")
    global _STATE
    with _LOCK:
        prior = _STATE
        counts = dict(prior.cache_open_failures)
        counts[kind] += 1
        _STATE = dataclasses.replace(
            prior,
            cache_open_failures=types.MappingProxyType(counts),
        )


class _Span:
    """One measured region inside a tick. Nesting-aware in both directions."""

    __slots__ = ("_tick", "_kind", "_start", "child_ingest", "child_build")

    def __init__(self, tick: "TickContext", kind: str):
        self._tick = tick
        self._kind = kind
        self._start = 0
        self.child_ingest = 0
        self.child_build = 0

    def __enter__(self):
        self._start = self._tick._now()
        self._tick._spans.append(self)
        return self

    def __exit__(self, *exc):
        elapsed = self._tick._now() - self._start
        spans = self._tick._spans
        # Identity-aware unwind, so a span whose __exit__ was skipped cannot
        # strand this one on the stack.
        if self in spans:
            while spans and spans[-1] is not self:
                spans.pop()
            spans.pop()
        own = elapsed - self.child_ingest - self.child_build
        if own < 0:
            own = 0
        self._tick._add(self._kind, own)
        up_ingest = self.child_ingest + (own if self._kind == _INGEST else 0)
        up_build = self.child_build + (own if self._kind == _BUILD else 0)
        if spans:
            spans[-1].child_ingest += up_ingest
            spans[-1].child_build += up_build
        return False


class TickContext:
    """A tick in progress. Thread-confined; only ``finish`` touches the lock."""

    __slots__ = ("_now", "_standalone", "_started_ns", "_spans", "_finished",
                 "_ingest_ns", "_builder_ns", "_ingest_ran", "_dispatch",
                 "_codex_regime", "_publication", "_cold", "_cache_pin_ns")

    def __init__(self, *, monotonic_ns, standalone: bool):
        self._now = monotonic_ns
        self._standalone = standalone
        self._started_ns = monotonic_ns()
        self._spans: list[_Span] = []
        self._finished = False
        self._ingest_ns = 0
        self._builder_ns = 0
        self._ingest_ran = False
        # An unset dispatch is the degraded case by construction: a crash or a
        # deferral publishes without ever reaching a dispatch decision, and
        # leaving it unclassified would break `idle + full + degraded ==
        # tick_seq`, which is what the operator reads as "how many ticks ran".
        self._dispatch = "degraded"
        self._codex_regime = "not_observed"
        self._publication = "final"
        self._cold = False
        self._cache_pin_ns = 0

    # ── measurement ──────────────────────────────────────────────────────

    def ingest_span(self) -> _Span:
        """Measure an ingest region. Also stamps ``ingest_ran``."""
        self._ingest_ran = True
        return _Span(self, _INGEST)

    def build_span(self) -> _Span:
        """Measure a builder region."""
        return _Span(self, _BUILD)

    def mark_ingest(self, ns: int) -> None:
        """Attribute already-measured time to ingest. Stamps ``ingest_ran``.

        For a caller that measured the region itself rather than opening one
        of the two built-in spans — no production seam does today, and both
        are kept because the record must be writable from outside them.

        A caller that reports ingest time is reporting that ingest ran, and
        `finish` zeroes `ingest_ns` when the flag is false — so leaving the
        flag to the span form alone would silently discard the figure.
        """
        self._ingest_ran = True
        self._add(_INGEST, int(ns))

    def mark_build(self, ns: int) -> None:
        """Attribute already-measured time to the builder.

        The `mark_ingest` note applies: this exists for a caller outside the
        two built-in spans, not because a production seam uses it.
        """
        self._add(_BUILD, int(ns))

    def mark_cache_pin(self, ns: int) -> None:
        """Attribute a held cache.db read transaction to this tick (#583 S5).

        Measured at the `BEGIN` and `ROLLBACK` boundaries in
        `_tui_build_source_bundle`, so it is the HOLD itself rather than that
        function's cumulative duration. The two differ: the duration also
        counts the work before `BEGIN` and after `ROLLBACK`, which makes it an
        upper bound on the hold and not the hold. Spec §2.4 forbids quoting a
        hold figure sourced from the duration.

        ACCUMULATED, not last-write, for the reason `set_codex_regime`
        documents: A2 can run several builds inside one refresh, each opening
        its own pin, and reporting only the last one would understate a
        refresh that pinned twice. It is therefore a sum of holds within the
        tick and not the longest single hold; a tick that pinned once, which
        is every non-A2 tick, reports that one hold exactly.

        Negative and non-integer inputs are clamped to zero rather than
        raising, because this runs on the publish path and a diagnostic must
        never take down the tick it is describing.
        """
        try:
            value = int(ns)
        except (TypeError, ValueError):
            return
        self._cache_pin_ns += max(0, value)

    def _add(self, kind: str, ns: int) -> None:
        if kind == _INGEST:
            self._ingest_ns += ns
        else:
            self._builder_ns += ns

    # ── classification, aggregated over the whole refresh ────────────────

    def set_dispatch(self, value: str) -> None:
        """`full` wins over `idle`, which wins over `degraded` (§1.4).

        Aggregated, not last-write: A2 can run several builds inside one
        refresh and they may disagree, and an expensive full build followed by
        an idle one is a full tick.
        """
        if value not in DISPATCH_KINDS:
            raise ValueError(f"unknown dispatch: {value!r}")
        rank = {"degraded": 0, "idle": 1, "full": 2}
        if rank[value] > rank[self._dispatch]:
            self._dispatch = value

    def mark_degraded(self) -> None:
        """Force the degraded classification, overriding a completed build.

        Precedence cannot express this: a refresh whose A2 partial built fine
        and whose FINAL build then crashed produced no usable final build, so
        it is degraded even though a build completed (§1.4).
        """
        self._dispatch = "degraded"
        self._publication = "degraded"

    def set_codex_regime(self, value: str) -> None:
        """`active` wins over `idle`, which wins over `not_observed` (§1.5).

        Last-write classification would move an expensive tick into the idle
        population once a partial build has populated the reuse memo, which is
        the F40 distortion this classifier exists to prevent.
        """
        if value not in CODEX_REGIMES:
            raise ValueError(f"unknown codex regime: {value!r}")
        rank = {"not_observed": 0, "idle": 1, "active": 2}
        if rank[value] > rank[self._codex_regime]:
            self._codex_regime = value

    def set_publication(self, value: str) -> None:
        if value not in PUBLICATIONS:
            raise ValueError(f"unknown publication: {value!r}")
        self._publication = value

    def set_cold(self, value: bool) -> None:
        """Sticky: a refresh containing one cold build is a cold tick."""
        self._cold = self._cold or bool(value)

    @property
    def finished(self) -> bool:
        return self._finished

    # ── close ────────────────────────────────────────────────────────────

    def finish(self, *, published_ns: int, published_at: str) -> None:
        """Freeze this tick into the ring. Idempotent; a second call is a
        no-op, so a crash handler may close a tick the happy path already
        closed."""
        if self._finished:
            return
        self._finished = True
        # Truncate rather than reject: this runs on the publish path, and a
        # diagnostic must never take down the tick it is describing.
        published_at = str(published_at)[:PUBLISHED_AT_MAX_CHARS]
        while self._spans:
            self._spans.pop().__exit__(None, None, None)
        ended_ns = self._now()
        if getattr(_tls, "tick", None) is self:
            _tls.tick = None

        global _STATE
        with _LOCK:
            prior = _STATE
            if self._standalone:
                seq = prior.tick_seq
                period = None
            else:
                seq = prior.tick_seq + 1
                period = (
                    published_ns - prior.records[-1].published_ns
                    if prior.records else None
                )
            record = TickRecord(
                seq=seq,
                started_ns=self._started_ns,
                ended_ns=ended_ns,
                duration_ns=max(0, ended_ns - self._started_ns),
                ingest_ran=self._ingest_ran,
                ingest_ns=self._ingest_ns if self._ingest_ran else 0,
                builder_ns=self._builder_ns,
                dispatch=self._dispatch,
                codex_regime=self._codex_regime,
                publication=self._publication,
                cold=self._cold,
                published_ns=int(published_ns),
                published_at=published_at,
                period_ns=period,
                cache_pin_ns=self._cache_pin_ns,
            )
            if self._standalone:
                _STATE = dataclasses.replace(prior, standalone=record)
                return
            counts = dict(prior.dispatch_counts)
            counts[self._dispatch] += 1
            _STATE = StatsSnapshot(
                dispatch_counts=types.MappingProxyType(counts),
                cache_open_failures=prior.cache_open_failures,
                tick_seq=seq,
                records=(prior.records + (record,))[-RING_CAPACITY:],
                standalone=prior.standalone,
                # Carried forward BY HAND: this is the one mutator that rebuilds
                # the snapshot field by field instead of using
                # `dataclasses.replace`, so dropping this line would let every
                # refresh tick wipe the conversation ring in production while
                # the whole suite stayed green. Regression:
                # `tests/test_tick_stats.py`.
                conversation_records=prior.conversation_records,
            )


def record_conversation_pass(
    *, seq, started_ns, ended_ns, duration_ns, cpu_ns, status
) -> None:
    """Append one conversation sync pass to its ring, under the SHARED lock.

    No second lock and no second state object: the module keeps one `_LOCK` and
    one immutable whole-state replacement (#583 S4 §6).

    `status` is normalized against `CONVERSATION_STATUSES` HERE. That is what
    makes the no-leak constraint true — validating in the recorder rather than
    trusting the dataclass is the point, because a frozen field typed `str`
    would accept `str(exc)` or a filesystem path and publish it through the
    debug endpoint.

    This call also FINALIZES the previous record's `period_ns`, because that
    field is the FORWARD interval `start[i+1] - start[i]` and this pass's start
    is what closes it. The previous entry is replaced in place with
    `dataclasses.replace` under the same lock; the record appended here carries
    `period_ns=None` until its own successor arrives.

    Storing the BACKWARD interval instead — the gap that preceded the pass —
    would pair each pass's CPU with someone else's interval, and
    `cpu[i] / (start[i] - start[i-1])` has no upper bound: a long pass after
    short ones renders above 100%, well past the 50% ceiling the loop's duty
    bound guarantees.
    """
    global _STATE
    safe_status = status if status in CONVERSATION_STATUSES else "error"
    start = int(started_ns)
    record = ConversationSyncRecord(
        seq=int(seq),
        started_ns=start,
        ended_ns=int(ended_ns),
        duration_ns=max(0, int(duration_ns)),
        cpu_ns=max(0, int(cpu_ns)),
        period_ns=None,
        status=safe_status,
    )
    with _LOCK:
        prior = _STATE
        retained = prior.conversation_records
        if retained:
            previous = retained[-1]
            retained = retained[:-1] + (
                dataclasses.replace(
                    previous, period_ns=start - previous.started_ns,
                ),
            )
        _STATE = dataclasses.replace(
            prior,
            conversation_records=(retained + (record,))[-RING_CAPACITY:],
        )


def begin_tick(*, monotonic_ns=None, standalone: bool = False) -> TickContext:
    """Open a tick context and make it ``current()`` on this thread.

    ``standalone`` records a ``tui --render-once`` or
    ``cctally-snapshot-measure`` build: it fills the single standalone slot
    instead of the ring, so it neither advances ``tick_seq`` nor lands in a
    dispatch count (§1.2).
    """
    ctx = TickContext(
        monotonic_ns=monotonic_ns or time.monotonic_ns,
        standalone=standalone,
    )
    _tls.tick = ctx
    return ctx


def _deep_size(obj, _seen=None) -> int:
    """Recursive ``sys.getsizeof`` over the owned state. Test-only.

    Each distinct object is counted once, so the interned enum strings the
    records share are not multiplied by the ring length.
    """
    if _seen is None:
        _seen = set()
    if id(obj) in _seen:
        return 0
    _seen.add(id(obj))
    total = sys.getsizeof(obj)
    if isinstance(obj, (str, bytes, int, float, bool, type(None))):
        return total
    if isinstance(obj, (dict, types.MappingProxyType)):
        for key, value in obj.items():
            total += _deep_size(key, _seen) + _deep_size(value, _seen)
        return total
    if isinstance(obj, (tuple, list, set, frozenset)):
        for item in obj:
            total += _deep_size(item, _seen)
        return total
    for field in getattr(type(obj), "__slots__", ()):
        total += _deep_size(getattr(obj, field, None), _seen)
    return total
