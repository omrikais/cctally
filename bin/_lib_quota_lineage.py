"""Pure kernel for correction-to-fit lineage (#1015).

No I/O, no clock, no database, no printing, no environment read; stdlib only.
The glue that reads the stats projections lives in
`bin/_cctally_quota_calibration.py` (`read_correction_facts`) and in the
journal paths that write them (`bin/_cctally_journal.py`).

Spec: docs/superpowers/specs/2026-10-31-1015-calibration-correction-lineage-design.md
(sections 5.1 to 5.3).

This module owns two things the data plane needs:

* :func:`batch_footprint` — the ``(provider, account_key)`` pairs one completed
  correction batch touched. Every path that records a batch's lineage (the
  selector's completion step on live ingest, the live emitters and the
  apply-only rebuild) computes it here, so the paths cannot disagree.
* The value types of the correction facts a fit is assessed against
  (:class:`CorrectionFacts`, :class:`CorrectionRow`, :class:`BatchFact`).

The lineage predicate, the window construction and the transition lineage are
added beside them by the rest of #1015.
"""
from __future__ import annotations

import bisect
from typing import Callable, Iterable, Literal, NamedTuple

#: An owner as the fold stores it: ``(provider, account_key)``. ``'*'`` in the
#: account position is a stored account-blind row (a `*`-family latch, an
#: effects-only event); ``('*', '*')`` is written only when ownership is
#: genuinely unknowable.
Owner = tuple[str, str]

#: The footprint entry for a revision whose owner cannot be resolved.
UNKNOWN_OWNER: Owner = ("*", "*")

ActionKind = Literal["replace", "tombstone", "new"]


class Action(NamedTuple):
    """One action of a completed correction batch, in manifest order.

    ``kind`` is ``"replace"`` (a replacement revision), ``"tombstone"`` or
    ``"new"`` (a replacement of an event id with no prior revision; a
    ``"replace"`` whose predecessor resolves to None is treated the same).
    ``payload_owner`` is the owner the fold would store for the new payload,
    None for a tombstone or when it cannot be resolved (a replacement or new
    event with an unresolvable payload owner contributes :data:`UNKNOWN_OWNER`).
    """

    event_id: str
    rev: int
    kind: ActionKind
    payload_owner: "Owner | None"


class Revision(NamedTuple):
    """The revision of an event id effective immediately before an action.

    ``status`` is ``"active"`` for a revision carrying data, whose ``owner`` is
    the owner the fold stores for it (None when that cannot be resolved), or
    ``"tombstone"`` for a revision that carries no data — a tombstoned id,
    whose replacement adds only its own payload and whose re-tombstoning
    removes nothing. ``predecessor_of`` returns None when the id has no
    effective revision at all.
    """

    status: Literal["active", "tombstone"]
    owner: "Owner | None" = None


def _active_owner(revision: Revision) -> "Owner | None":
    """The data owner a revision contributes when it is replaced or removed:
    nothing for a tombstone, its owner (or :data:`UNKNOWN_OWNER`) otherwise."""
    if revision.status != "active":
        return None
    return revision.owner if revision.owner is not None else UNKNOWN_OWNER


def batch_footprint(
    actions: Iterable[Action],
    predecessor_of: Callable[[str, int], "Revision | None"],
) -> frozenset:
    """The ``(provider, account_key)`` pairs one completed batch touched.

    For each action, in manifest order:

    * a replacement contributes the owner of the revision it replaces and the
      owner of its new payload, so a move from account A to account B touches
      both;
    * a tombstone contributes the owner of the revision it removes;
    * a new event contributes the owner of its payload.

    The predecessor is the revision effective immediately before that action:
    the revision an earlier action of the same batch produced for the same id,
    else ``predecessor_of(event_id, rev)`` (the caller's view of the effective
    revision before the batch). A replacement whose predecessor resolves to
    None is a new event. :data:`UNKNOWN_OWNER` appears only when an owner
    cannot be resolved. A batch with no actions has an empty footprint.
    """
    touched: set = set()
    produced: dict = {}
    for action in actions:
        if action.event_id in produced:
            predecessor = produced[action.event_id]
        elif action.kind == "new":
            predecessor = None
        else:
            predecessor = predecessor_of(action.event_id, action.rev)
        if action.kind == "tombstone":
            if predecessor is not None:
                removed = _active_owner(predecessor)
                if removed is not None:
                    touched.add(removed)
            produced[action.event_id] = Revision("tombstone")
            continue
        payload = (action.payload_owner if action.payload_owner is not None
                   else UNKNOWN_OWNER)
        if predecessor is not None:
            replaced = _active_owner(predecessor)
            if replaced is not None:
                touched.add(replaced)
        touched.add(payload)
        produced[action.event_id] = Revision("active", action.payload_owner)
    return frozenset(touched)


# --------------------------------------------------------------------------
# correction facts (read from the published stats projections)
# --------------------------------------------------------------------------

#: A journal coordinate ``(segment, end_offset)``: the end of the record's
#: line. Coordinates compare in journal order through
#: `_cctally_journal.journal_coordinate_key`; this kernel never orders them
#: itself, the caller supplies comparable keys where it needs to.
Coordinate = tuple[str, int]


class CorrectionRow(NamedTuple):
    """One reviewed decision of one observation, joined to the observation
    index and to its op's coordinate. ``account_key`` and ``captured_at`` are
    None when no index row resolves the observation (an unresolved
    reference)."""

    op_id: str
    observation_id: str
    disposition: Literal["hold", "retract", "accept"]
    account_key: "str | None"
    captured_at: "str | None"
    op_coord: Coordinate


class BatchFact(NamedTuple):
    """One completed correction batch: its commit coordinate and its
    footprint (a frozenset of ``(provider, account_key)`` pairs; empty for an
    empty batch)."""

    batch_id: str
    commit_coord: Coordinate
    footprint: frozenset


class CorrectionFacts(NamedTuple):
    """Everything a fit's lineage is assessed against, read from one coherent
    stats snapshot: the correction rows in the account scope, every completed
    batch, and the current basis (the ids of every completed batch)."""

    corrections: tuple
    batches: tuple
    basis: frozenset


# --------------------------------------------------------------------------
# fit lineage: windows, digest, the lineage records (#1015 spec 5.2)
# --------------------------------------------------------------------------

#: The `fitLineage` / `transitionLineage` record version.
LINEAGE_VERSION = 1

#: Regime keys a non-fitting transition legitimately changes (closing,
#: stale-marking, the #1015 lifecycle annotations, the frozen transition
#: lineage with its own digest) plus the writer-provenance stamps. Every
#: other key a fit writes is bound by the lineage digest (spec 5.2).
DIGEST_EXCLUDED_KEYS = frozenset((
    "effectiveUntil", "status", "supersededByCorrection",
    "recalibrationAttempt", "transitionLineage",
    "wpg", "wpgDigest", "wpr", "wprDigest", "wpx", "wpxDigest",
))

FIT_LINEAGE_KEY = "fitLineage"
TRANSITION_LINEAGE_KEY = "transitionLineage"

WindowKind = Literal["nosplit", "successor", "baseline"]


class Assessment(NamedTuple):
    """A fit's (or a transition's) lineage against the correction facts.

    ``corrections`` is ``((op_id, observation_count), ...)`` in journal order
    of the op, populated only for ``"tainted"``.
    """

    state: Literal["clean", "tainted", "unavailable"]
    corrections: tuple = ()


CLEAN = Assessment("clean")
UNAVAILABLE = Assessment("unavailable")


def _canonical(value) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _instant(value):
    """An aware UTC datetime from a datetime or an ISO string, else None."""
    import datetime as dt
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _iso(value) -> "str | None":
    instant = _instant(value)
    return None if instant is None else instant.isoformat()


def window(start, until, *, until_inclusive: bool) -> dict:
    """One dependency window as stored: canonical UTC instants, None open."""
    return {"from": _iso(start), "until": _iso(until),
            "untilInclusive": bool(until_inclusive)}


def dependency_windows(kind: WindowKind, *, a, s=None, t=None) -> tuple:
    """The correction dependency windows of one fit (spec 5.2).

    ``a`` is the analysis start (`PersistMode.regime_start`), ``s`` the
    split's first instant, ``t`` the fit's `asOf`. A no-split fit depends on
    ``[A, T]``; a successor on ``[S, T]`` plus the pre-split ``[A, S)`` that
    supplies its fence and baseline composition; a baseline created in the
    same run on ``[A, S)``. A correction exactly at ``S`` belongs to the
    successor, one exactly at ``T`` is included.
    """
    if kind == "nosplit":
        return (window(a, t, until_inclusive=True),)
    if kind == "successor":
        return (window(s, t, until_inclusive=True),
                window(a, s, until_inclusive=False))
    if kind == "baseline":
        return (window(a, s, until_inclusive=False),)
    raise ValueError(f"unknown dependency-window kind {kind!r}")


def _regime_core(regime) -> dict:
    return {k: v for k, v in dict(regime).items()
            if k not in DIGEST_EXCLUDED_KEYS and k != FIT_LINEAGE_KEY}


def lineage_digest(regime) -> str:
    """The digest a regime's `fitLineage.digest` must equal (spec 5.2).

    It binds every field a fit writes — the fitted values and their context,
    `asOf`, `effectiveFrom` — and the lineage itself (windows, estimator,
    scope, basis, `fitId`), and excludes only `DIGEST_EXCLUDED_KEYS`. A
    previous release's in-place `update()` always writes a new `asOf`, so it
    breaks this digest and the regime is assessed as legacy.
    """
    lineage = regime.get(FIT_LINEAGE_KEY) if isinstance(regime, dict) else None
    lineage_body = ({k: v for k, v in lineage.items() if k != "digest"}
                    if isinstance(lineage, dict) else None)
    return "sha256:" + _sha256(_canonical(
        {"regime": _regime_core(regime), "lineage": lineage_body}))


def fit_lineage(*, scope, basis, windows, estimator, regime) -> dict:
    """The immutable `fitLineage` of a fit this binary creates (spec 5.2).

    ``regime`` is the record the lineage describes (any `fitLineage` it
    already carries is ignored); the returned object's `digest` verifies
    against ``{**regime, "fitLineage": <returned>}``.
    """
    body = {
        "version": LINEAGE_VERSION,
        "accountScope": str(scope),
        "basis": sorted(str(b) for b in basis),
        "estimator": None if estimator is None else dict(estimator),
        "dependencyWindows": [dict(w) for w in windows],
        "legacy": False,
    }
    fit_id = "fit:" + _sha256(_canonical(
        {"regime": _regime_core(regime), "lineage": body}))
    lineage = {"version": LINEAGE_VERSION, "fitId": fit_id, **body}
    lineage = {k: lineage[k] for k in (
        "version", "fitId", "accountScope", "basis", "estimator",
        "dependencyWindows", "legacy")}
    lineage["digest"] = lineage_digest(
        {**dict(regime), FIT_LINEAGE_KEY: lineage})
    return lineage


def fit_lineage_verifies(regime) -> bool:
    """Whether ``regime`` carries a current `fitLineage` whose digest holds."""
    if not isinstance(regime, dict):
        return False
    lineage = regime.get(FIT_LINEAGE_KEY)
    return (isinstance(lineage, dict)
            and lineage.get("version") == LINEAGE_VERSION
            and lineage.get("legacy") is False
            and isinstance(lineage.get("fitId"), str)
            and lineage.get("digest") == lineage_digest(regime))


def legacy_lineage(regime, era_start, *, scope="*") -> dict:
    """The conservative lineage of a regime with no verifying `fitLineage`.

    ``basis = []`` and one window ``[era start of its coefficientEra, asOf]``;
    an unreadable era start or `asOf` leaves that end open. ``fitId`` is the
    sha256 of the regime's canonical content. ``scope`` is the bucket the
    regime is stored under (``"*"``, every account, when unknown).
    """
    record = dict(regime) if isinstance(regime, dict) else {}
    return {
        "version": LINEAGE_VERSION,
        "fitId": "fit:" + _sha256(_canonical(record)),
        "accountScope": str(scope),
        "basis": [],
        "estimator": None,
        "dependencyWindows": [window(era_start, record.get("asOf"),
                                     until_inclusive=True)],
        "legacy": True,
    }


def regime_lineage(regime, *, scope, era_start) -> dict:
    """The lineage a regime is assessed under: its own verifying
    `fitLineage`, else :func:`legacy_lineage`."""
    if fit_lineage_verifies(regime):
        return dict(regime[FIT_LINEAGE_KEY])
    return legacy_lineage(regime, era_start, scope=scope)


def transition_lineage(*, windows, scope, basis, descriptor) -> dict:
    """The frozen lineage of one detected transition (spec 5.7).

    ``windows`` is the union of both sides' dependency windows and the
    detector coverage; ``descriptor`` is every value the `meter_rate_change`
    event journals other than the lineage itself. The digest covers all of
    it and is independent of either regime's `fitLineage`.
    """
    unique: list = []
    for w in windows:
        w = dict(w)
        if w not in unique:
            unique.append(w)
    body = {
        "version": LINEAGE_VERSION,
        "accountScope": str(scope),
        "basis": sorted(str(b) for b in basis),
        "dependencyWindows": unique,
        "descriptor": dict(descriptor),
    }
    body["digest"] = "sha256:" + _sha256(_canonical(body))
    return body


def transition_lineage_verifies(lineage) -> bool:
    if not isinstance(lineage, dict) or lineage.get("version") \
            != LINEAGE_VERSION or not isinstance(lineage.get("descriptor"),
                                                 dict):
        return False
    body = {k: v for k, v in lineage.items() if k != "digest"}
    return lineage.get("digest") == "sha256:" + _sha256(_canonical(body))


def legacy_transition_lineage(*, account_key, detected_at,
                              unattributed="unattributed") -> dict:
    """The conservative lineage of a transition row with a null
    `fit_lineage` (spec 5.7): ``basis = []``, window
    ``(-inf, detected_at]``, and the row's account as scope — except the
    `unattributed` sentinel, which the historical writer used for a merged
    fit and is therefore assessed with the merged scope."""
    scope = "*" if account_key in (None, unattributed) else str(account_key)
    return {
        "version": LINEAGE_VERSION,
        "accountScope": scope,
        "basis": [],
        "dependencyWindows": [window(None, detected_at,
                                     until_inclusive=True)],
        "legacy": True,
    }


# --------------------------------------------------------------------------
# the predicate (spec 5.3)
# --------------------------------------------------------------------------

def _coordinate_order(coord) -> tuple:
    """Journal order of a ``(segment, offset)`` coordinate: the key
    `_cctally_journal.journal_coordinate_key` uses."""
    import _lib_journal
    return (_lib_journal.segment_sort_key(str(coord[0])), int(coord[1]))


def _touches(footprint, account_key) -> bool:
    return (("claude", account_key) in footprint
            or ("claude", "*") in footprint or ("*", "*") in footprint)


def _touches_claude(footprint) -> bool:
    return any(provider in ("claude", "*") for provider, _a in footprint)


def _parsed_windows(windows) -> tuple:
    """`(start, until, until_inclusive)` per well-formed window, parsed once
    per assessment rather than once per correction."""
    return tuple((_instant(w.get("from")), _instant(w.get("until")),
                  bool(w.get("untilInclusive")))
                 for w in windows if isinstance(w, dict))


def _in_windows(instant, windows) -> bool:
    for start, until, inclusive in windows:
        if start is not None and instant < start:
            continue
        if until is not None and (
                instant > until or (instant == until and not inclusive)):
            continue
        return True
    return False


class _Prepared(NamedTuple):
    """One facts object with every coordinate ordered and every capture
    parsed once: `(op_order, row, captured_instant)` per correction and
    `(commit_order, batch)` per batch (R6)."""

    rows: tuple
    batches: tuple


#: The last facts object prepared, with the key it was prepared under. The
#: facts are immutable tuples and the reference is held, so an identity
#: match is the same content; a consumer assessing many lineages against one
#: facts object (the sweep, the transition rows) parses each capture once.
_PREPARED_MEMO: tuple = (None, None, None)


def _prepared(facts, key) -> _Prepared:
    global _PREPARED_MEMO
    memo_facts, memo_key, memo = _PREPARED_MEMO
    if memo_facts is facts and memo_key is key:
        return memo
    prepared = _Prepared(
        tuple((key(row.op_coord), row, _instant(row.captured_at))
              for row in facts.corrections),
        tuple(sorted(((key(b.commit_coord), b) for b in facts.batches),
                     key=lambda item: item[0])))
    _PREPARED_MEMO = (facts, key, prepared)
    return prepared


def assess(lineage, facts, *, coord_key=None) -> Assessment:
    """Assess one lineage against the correction facts (spec 5.3).

    Tainted iff some correction ``(op, obs)`` has a completed batch ``b``
    outside the lineage's basis whose commit follows the op's coordinate,
    whose footprint contains ``('claude', obs.account)``, ``('claude', '*')``
    or ``('*', '*')``, with the observation inside the lineage's scope and
    one of its dependency windows. An `accept` counts like any correction.
    An unresolved reference (no index row) followed by such a batch makes
    the assessment `unavailable`; it is never read as "no correction". A
    None lineage or None facts is `unavailable`. A lineage whose basis
    holds every completed batch is clean without a probe.
    """
    if lineage is None or facts is None or not isinstance(lineage, dict):
        return UNAVAILABLE
    key = coord_key or _coordinate_order
    basis = frozenset(str(b) for b in (lineage.get("basis") or ()))
    outside = [(commit, frozenset(b.footprint))
               for commit, b in _prepared(facts, key).batches
               if b.batch_id not in basis]
    if not outside:
        return CLEAN
    # The batches committed after an op are a suffix of `outside` in commit
    # order, and both footprint predicates are membership tests, so "some
    # later batch's footprint matches" is "the union of the suffix's
    # footprints matches": one bisection per correction instead of a scan of
    # every batch (R6).
    commits = [commit for commit, _fp in outside]
    suffix: list = [frozenset()] * (len(outside) + 1)
    for i in range(len(outside) - 1, -1, -1):
        suffix[i] = suffix[i + 1] | outside[i][1]
    scope = str(lineage.get("accountScope") or "*")
    windows = _parsed_windows(lineage.get("dependencyWindows") or ())
    tainted: dict = {}
    unresolved = False
    for op_order, row, captured in _prepared(facts, key).rows:
        first = bisect.bisect_right(commits, op_order)
        if first == len(commits):
            continue
        later = suffix[first]
        if row.account_key is None or captured is None:
            if _touches_claude(later):
                unresolved = True
            continue
        if scope != "*" and row.account_key != scope:
            continue
        if not _in_windows(captured, windows):
            continue
        if _touches(later, row.account_key):
            tainted.setdefault(row.op_id, set()).add(row.observation_id)
    if tainted:
        return Assessment("tainted", tuple(
            (op, len(observations)) for op, observations in tainted.items()))
    if unresolved:
        return UNAVAILABLE
    return CLEAN


# --------------------------------------------------------------------------
# what a set of lineages needs from the correction rows (R6)
# --------------------------------------------------------------------------

class RowDemand(NamedTuple):
    """The correction rows a set of lineages can be affected by.

    `assess` ignores a correction whose op no batch outside the lineage's
    basis touching Claude follows, and a resolved, parseable observation
    outside the lineage's scope or dependency windows. A reader may
    therefore omit every row of an op ordered at or after ``op_before`` (the
    latest such batch over the set), and every resolved row whose capture is
    a parseable instant outside ``[start, until]`` (None is open; ``empty``
    means the set has no window) or, when ``account`` is set, of another
    account. Unresolved and unparseable rows are always kept: the kernel
    reads them as unresolved references. ``op_before`` None keeps every op.
    """

    op_before: "tuple | None"
    start: "object | None"
    until: "object | None"
    empty: bool
    account: "str | None"


#: Every row: what a consumer that cannot name its lineages in advance (the
#: sweep, which assesses the state it re-reads under the lock) reads.
ALL_ROWS = RowDemand(None, None, None, False, None)


def row_demand(lineages, batches, *, coord_key=None) -> "RowDemand | None":
    """What ``lineages`` need from the correction rows, given every
    completed batch; None when no row can affect any of them (each basis
    holds every batch touching Claude), so the rows need not be read.

    The facts a reader returns under this demand assess every lineage in
    the set exactly as the full facts do (spec 5.3): the op filter keeps an
    op whenever some lineage has a later outside batch touching Claude (the
    precondition of both a taint and an unresolved verdict), and the window
    and account bounds are the union over the lineages that need rows.
    """
    key = coord_key or _coordinate_order
    latest = None
    starts: list = []
    untils: list = []
    open_start = open_until = False
    scopes: set = set()
    for lineage in lineages:
        if not isinstance(lineage, dict):
            continue        # assessed `unavailable` without any fact
        basis = frozenset(str(b) for b in (lineage.get("basis") or ()))
        commits = [key(b.commit_coord) for b in batches
                   if b.batch_id not in basis and _touches_claude(b.footprint)]
        if not commits:
            continue
        newest = max(commits)
        latest = newest if latest is None or newest > latest else latest
        scopes.add(str(lineage.get("accountScope") or "*"))
        for start, until, _inclusive in _parsed_windows(
                lineage.get("dependencyWindows") or ()):
            if start is None:
                open_start = True
            else:
                starts.append(start)
            if until is None:
                open_until = True
            else:
                untils.append(until)
    if latest is None:
        return None
    empty = not (starts or open_start)
    return RowDemand(
        op_before=latest,
        start=None if open_start or not starts else min(starts),
        until=None if open_until or not untils else max(untils),
        empty=empty,
        account=(next(iter(scopes))
                 if len(scopes) == 1 and "*" not in scopes else None))
