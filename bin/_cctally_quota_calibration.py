"""The non-mutating read of the persisted quota calibration (#661 S2).

The only I/O half of the §1.1 adapters: open the sidecar, hand its bytes to
the pure validator in `bin/_lib_quota_calibration.py`, and report a typed
cause when anything about it is unusable.

WHY THIS EXISTS RATHER THAN A CALL TO `load_calibrations`. That loader
QUARANTINES a malformed or version-ahead file by renaming it aside. `doctor`
is documented read-only, the status line runs per prompt, and the dashboard
serves a read request — a rename from any of them makes a documented reader a
writer, and the rename is invisible to the user who triggered it. This module
therefore never imports or calls `load_calibrations`, `_quarantine`, or
`save_calibrations`, and takes no lock: `save_calibrations` publishes through
`os.replace` in the same directory, so a lock-free reader always observes a
complete old or new inode, and a flock here would let a writer stall a prompt.

Spec: docs/superpowers/specs/2026-08-28-661-s2-quota-insight-surfaces.md §1.1
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3

import _cctally_quota_model
import _lib_journal
import _lib_quota_model as qm
import _lib_quota_lineage as ql
from _lib_quota_lineage import BatchFact, CorrectionFacts, CorrectionRow
from _lib_quota_calibration import (  # re-exported for callers and tests
    AppliedQuota, ApplyRejection, RegimeRejection, ValidatedRegime,
    apply_regime, validate_regime,
)

__all__ = [
    "AppliedQuota", "ApplyRejection", "CalibrationRead", "RegimeRejection",
    "ValidatedRegime", "apply_regime", "assess_regime",
    "read_calibration_file", "read_correction_facts", "refusal_status",
    "validate_regime", "Supersession", "SupersessionState",
    "assess_active_rate_change", "clean_active_rate_change", "marker_regimes",
    "rate_change_supersession", "read_rate_change_supersessions",
    "transition_lineage_for",
]


@dataclasses.dataclass(frozen=True)
class CalibrationRead:
    """Exactly one of `regime` and `rejection` is set.

    `regime_status` carries the stored `status` of the regime the prediction
    gate refused, and is `None` in every other outcome. S1 records WHY it held
    a successor fit below the gate, and a surface that states
    `insufficient-history` while the store says `unstable-fit` names a cause
    the reader cannot reconcile with `cctally quota`.
    """

    regime: "ValidatedRegime | None"
    rejection: "RegimeRejection | None"
    regime_status: "str | None" = None


def _read_text(path) -> str:
    """Open and read in one step, with NO prior existence check.

    A concurrent quarantine rename can remove the primary name between a
    check and an open, so a `path.exists()` guard would turn a race into a
    traceback. The absent case arrives here as `OSError` like every other
    failure and is separated by its errno at the call site.
    """
    with open(str(path), "r", encoding="utf-8") as handle:
        return handle.read()


def _state_key(account_key) -> str:
    """The stored bucket name for an account. `None` means the merged view."""
    return (_cctally_quota_model.MERGED_STATE_KEY if account_key is None
            else str(account_key))


def read_calibration_file(*, account_key, path=None,
                          expected_fingerprint=None,
                          expected_revision=None,
                          require_prediction_ready=True,
                          stats_conn=None) -> CalibrationRead:
    """The open regime for one account, validated, or a typed cause.

    `expected_fingerprint` and `expected_revision` default to THIS binary's
    constants, so a caller that names neither still refuses a regime fitted
    under other coefficients rather than applying it silently.

    `require_prediction_ready` defaults to True, so every projection consumer
    is refused a regime S1 marked `detection-only`. A detection consumer —
    section 6's rate-change alert — passes False and reads the same record.

    #1015 spec 5.4: a regime that validates is then assessed against the
    published correction facts. A tainted (or already annotated) regime is
    refused as `RECALIBRATION_NEEDED`, carrying in `regime_status` the
    status of its `recalibrationAttempt` when that attempt's basis is the
    current completed basis; unreadable lineage is `LINEAGE_UNAVAILABLE`.
    `stats_conn` is a current stats connection the caller already holds;
    without one the facts are read through the fail-fast read-only opener,
    which never recovers or waits. No stats access happens unless a regime
    validated.
    """
    if path is None:
        path = _cctally_quota_model.calibration_path()
    if expected_fingerprint is None:
        expected_fingerprint = qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT
    if expected_revision is None:
        expected_revision = qm.QUOTA_MODEL_ALGORITHM_REVISION

    try:
        raw = _read_text(path)
    except FileNotFoundError:
        return CalibrationRead(None, RegimeRejection.ABSENT)
    except (IsADirectoryError, NotADirectoryError):
        return CalibrationRead(None, RegimeRejection.ABSENT)
    except OSError:
        return CalibrationRead(None, RegimeRejection.UNREADABLE)

    try:
        state = json.loads(raw)
    except ValueError:
        return CalibrationRead(None, RegimeRejection.MALFORMED)
    if not isinstance(state, dict):
        return CalibrationRead(None, RegimeRejection.MALFORMED)

    version = state.get("schemaVersion")
    if isinstance(version, bool) or not isinstance(version, int) \
            or version > _cctally_quota_model.CALIBRATION_STATE_SCHEMA_VERSION:
        return CalibrationRead(None, RegimeRejection.SCHEMA_VERSION)

    accounts = state.get("accounts")
    if not isinstance(accounts, dict):
        return CalibrationRead(None, RegimeRejection.MALFORMED)
    bucket = accounts.get(_state_key(account_key))
    regimes = bucket.get("regimes") if isinstance(bucket, dict) else None
    if not isinstance(regimes, list):
        return CalibrationRead(None, RegimeRejection.ABSENT)

    # The LAST open regime, matching `_cctally_quota_model._open_regime`. A
    # store carrying several open records is malformed history rather than a
    # choice this reader makes, and the newest one is what a writer would
    # have updated in place.
    open_regime = None
    for candidate in reversed(regimes):
        if isinstance(candidate, dict) and candidate.get("effectiveUntil") \
                is None:
            open_regime = candidate
            break
    if open_regime is None:
        return CalibrationRead(None, RegimeRejection.ABSENT)

    outcome = validate_regime(
        open_regime, account_key=account_key,
        expected_fingerprint=expected_fingerprint,
        expected_revision=expected_revision,
        require_prediction_ready=require_prediction_ready)
    if isinstance(outcome, RegimeRejection):
        status = None
        if outcome is RegimeRejection.DETECTION_ONLY:
            stored = open_regime.get("status")
            status = str(stored) if stored is not None else None
        return CalibrationRead(None, outcome, status)
    scope = _state_key(account_key)
    lineage = regime_lineage_of(open_regime, scope=scope)
    facts = read_correction_facts(stats_conn, account_scope=scope,
                                  lineages=[lineage])
    assessment = ql.assess(lineage, facts, coord_key=_coordinate_order)
    if assessment.state == "tainted" or isinstance(
            open_regime.get("supersededByCorrection"), dict):
        return CalibrationRead(None, RegimeRejection.RECALIBRATION_NEEDED,
                               refusal_status(open_regime, facts))
    if assessment.state != "clean":
        return CalibrationRead(None, RegimeRejection.LINEAGE_UNAVAILABLE)
    return CalibrationRead(outcome, None)


def _era_start(regime):
    """The start of the regime's own coefficient era, or None (an unreadable
    era leaves the legacy window's start open)."""
    era = qm.era_by_id(regime.get("coefficientEra")) \
        if isinstance(regime, dict) else None
    return None if era is None else qm.era_bounds(era)[0]


def regime_lineage_of(regime, *, scope) -> dict:
    """The lineage one stored regime is assessed under (spec 5.3): its own
    verifying `fitLineage`, else the conservative legacy lineage over its
    bucket's scope."""
    return ql.regime_lineage(regime, scope=scope,
                             era_start=_era_start(regime))


def assess_regime(regime, facts, *, scope) -> "ql.Assessment":
    """One stored regime's lineage assessment (spec 5.3) under
    :func:`regime_lineage_of`. None facts are `unavailable`."""
    return ql.assess(regime_lineage_of(regime, scope=scope), facts,
                     coord_key=_coordinate_order)


def refusal_status(regime, facts) -> "str | None":
    """The `recalibrationAttempt.status` a refusal states, when that
    attempt's basis is the current completed basis; None otherwise."""
    attempt = regime.get("recalibrationAttempt") \
        if isinstance(regime, dict) else None
    if not isinstance(attempt, dict) or facts is None:
        return None
    basis = attempt.get("basis")
    if not isinstance(basis, list) or \
            frozenset(str(b) for b in basis) != facts.basis:
        return None
    status = attempt.get("status")
    return None if status is None else str(status)


# --------------------------------------------------------------------------
# #1015: the correction facts a fit's lineage is assessed against
# --------------------------------------------------------------------------

#: The distinct reviewed ops named by the history, by a loose scan of its
#: primary key (one index probe per op, never a scan of the decision rows).
_HISTORY_OPS_SQL = (
    "WITH RECURSIVE d(op) AS ("
    "SELECT MIN(decision_op_id) FROM claude_weekly_correction_history "
    "UNION ALL "
    "SELECT (SELECT MIN(decision_op_id) FROM claude_weekly_correction_history "
    "WHERE decision_op_id > d.op) FROM d WHERE d.op IS NOT NULL) "
    "SELECT op FROM d WHERE op IS NOT NULL"
)
_OP_COORDS_SQL = (
    "SELECT decision_op_id, op_segment, op_offset "
    "FROM claude_weekly_correction_ops"
)
#: Fails to prepare when the observation index is missing (an index older
#: than epoch 1022), even when no correction row needs reading.
_INDEX_PROBE_SQL = (
    "SELECT observation_id, account_key, captured_at_utc "
    "FROM claude_weekly_observation_index LIMIT 0"
)
#: One reviewed op's decision rows joined to their observation's index row,
#: narrowed to what a `_lib_quota_lineage.RowDemand` keeps. A row is kept
#: when it is unresolved (no index row); when its capture is not one of the
#: canonical UTC text forms or not a valid calendar instant (the kernel reads
#: it as unresolved, so its account and window are never consulted); or when
#: it is in the caller's account scope and inside the demand's account and
#: its second-floored capture box. The box compares the first 19 characters
#: of a canonical UTC text (`YYYY-MM-DDTHH:MM:SS`), whose text order is time
#: order, against second-floored bounds, so it only ever widens the exact
#: windows `assess` applies afterwards.
_CORRECTION_ROWS_SQL = (
    "SELECT h.observation_id, h.disposition, i.account_key, i.captured_at_utc "
    "FROM claude_weekly_correction_history h "
    "LEFT JOIN claude_weekly_observation_index i "
    "ON i.observation_id = h.observation_id "
    "WHERE h.decision_op_id = :op AND ("
    "i.observation_id IS NULL "
    "OR ((:scope = '*' OR i.account_key = :scope) "
    "AND (:account IS NULL OR i.account_key = :account) "
    "AND (:lo IS NULL OR i.captured_at_utc >= :lo) "
    "AND (:hi IS NULL OR i.captured_at_utc < :hi)) "
    "OR ((:scope = '*' OR i.account_key = :scope) AND NOT ("
    "substr(i.captured_at_utc, 1, 19) GLOB "
    "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]' "
    "AND (substr(i.captured_at_utc, 20) IN ('Z', '+00:00') "
    "OR substr(i.captured_at_utc, 20) GLOB '.[0-9][0-9][0-9][0-9][0-9][0-9]Z' "
    "OR substr(i.captured_at_utc, 20) GLOB '.[0-9][0-9][0-9][0-9][0-9][0-9]+00:00' "
    "OR substr(i.captured_at_utc, 20) GLOB '.[0-9][0-9][0-9]Z' "
    "OR substr(i.captured_at_utc, 20) GLOB '.[0-9][0-9][0-9]+00:00') "
    "AND substr(i.captured_at_utc, 1, 4) <> '0000' "
    "AND datetime(substr(i.captured_at_utc, 1, 19)) "
    "= replace(substr(i.captured_at_utc, 1, 19), 'T', ' '))))"
)
_BATCH_ROWS_SQL = (
    "SELECT batch_id, commit_segment, commit_offset "
    "FROM correction_batch_lineage"
)
_FOOTPRINT_ROWS_SQL = (
    "SELECT batch_id, provider, account_key FROM correction_batch_footprint"
)


def _coordinate_order(coord) -> tuple:
    """Journal order of a ``(segment, offset)`` coordinate; the same key as
    `_cctally_journal.journal_coordinate_key`, without importing the journal
    glue into a per-prompt reader."""
    return (_lib_journal.segment_sort_key(coord[0]), int(coord[1]))


def _box_text(instant, *, ceil: bool) -> "str | None":
    """A second-floored box bound as the 19-character canonical prefix;
    ``ceil`` moves it one second up so the exclusive comparison keeps every
    capture inside that second. None (open) when it cannot be formed."""
    import datetime as dt
    floored = instant.replace(microsecond=0)
    if ceil:
        try:
            floored += dt.timedelta(seconds=1)
        except OverflowError:
            return None
    return (f"{floored.year:04d}-{floored.month:02d}-{floored.day:02d}T"
            f"{floored.hour:02d}:{floored.minute:02d}:{floored.second:02d}")


class _FactsBase:
    """The cheap, always-read half of one snapshot's correction facts: every
    completed batch with its footprint, and each reviewed op's coordinate.
    ``None`` from :func:`_read_base` when a decision names an op with no
    coordinate (D3) or a projection is missing."""

    __slots__ = ("batches", "basis", "op_coords")

    def __init__(self, batches, op_coords):
        self.batches = batches
        self.basis = frozenset(batch.batch_id for batch in batches)
        self.op_coords = op_coords


def _read_base(conn: sqlite3.Connection) -> "_FactsBase | None":
    footprints: dict = {}
    for batch_id, provider, account_key in conn.execute(_FOOTPRINT_ROWS_SQL):
        footprints.setdefault(str(batch_id), set()).add(
            (str(provider), str(account_key)))
    batches = sorted(
        (BatchFact(str(batch_id), (str(segment), int(offset)),
                   frozenset(footprints.get(str(batch_id), ())))
         for batch_id, segment, offset in conn.execute(_BATCH_ROWS_SQL)),
        key=lambda batch: (_coordinate_order(batch.commit_coord),
                           batch.batch_id))
    known = {str(op): (str(segment), int(offset))
             for op, segment, offset in conn.execute(_OP_COORDS_SQL)}
    op_coords = {}
    for (op,) in conn.execute(_HISTORY_OPS_SQL):
        coord = known.get(str(op))
        if coord is None:
            # A decision the index cannot place in journal order cannot be
            # ordered against a batch: the assessment is unavailable, never
            # silently narrower — whatever the lineages being assessed.
            return None
        op_coords[str(op)] = coord
    conn.execute(_INDEX_PROBE_SQL).fetchall()
    return _FactsBase(tuple(batches), op_coords)


def _read_rows(conn: sqlite3.Connection, base: _FactsBase,
               account_scope: str, demand) -> tuple:
    """The correction rows ``demand`` keeps, in journal order of the op."""
    if demand is None:
        return ()
    lo = hi = None
    if demand.empty:
        lo, hi = "~", None      # no capture text sorts at or above "~"
    else:
        if demand.start is not None:
            lo = _box_text(demand.start, ceil=False)
        if demand.until is not None:
            hi = _box_text(demand.until, ceil=True)
    params = {"scope": account_scope, "account": demand.account,
              "lo": lo, "hi": hi}
    corrections = []
    for op_id, coord in base.op_coords.items():
        if demand.op_before is not None and \
                _coordinate_order(coord) >= demand.op_before:
            continue
        params["op"] = op_id
        for observation_id, disposition, account_key, captured_at in \
                conn.execute(_CORRECTION_ROWS_SQL, params):
            corrections.append(CorrectionRow(
                op_id, str(observation_id), str(disposition),
                None if account_key is None else str(account_key),
                None if captured_at is None else str(captured_at), coord))
    corrections.sort(key=lambda row: (_coordinate_order(row.op_coord),
                                      row.op_id, row.observation_id))
    return tuple(corrections)


def _facts_from(conn, base, account_scope, lineages) -> CorrectionFacts:
    demand = ql.ALL_ROWS if lineages is None else ql.row_demand(
        lineages, base.batches, coord_key=_coordinate_order)
    return CorrectionFacts(
        _read_rows(conn, base, account_scope, demand), base.batches,
        base.basis)


def _read_facts(conn: sqlite3.Connection, account_scope: str,
                lineages=None) -> "CorrectionFacts | None":
    base = _read_base(conn)
    if base is None:
        return None
    return _facts_from(conn, base, account_scope, lineages)


class _LazyFacts:
    """One read snapshot's correction facts, read only as far as the
    lineages assessed so far need (R6). :meth:`facts_for` returns facts that
    assess every lineage ever passed to it exactly as the full facts would;
    a lineage outside that set widens the demand and re-reads the rows.
    `base` is None when the facts are unavailable."""

    def __init__(self, conn, account_scope):
        self._conn = conn
        self._scope = account_scope
        self.base = _read_base(conn)
        self._covered: list = []
        self._facts = None

    def facts_for(self, lineages) -> "CorrectionFacts | None":
        if self.base is None:
            return None
        new = [lin for lin in lineages
               if not any(lin == seen for seen in self._covered)]
        if new or self._facts is None:
            self._covered.extend(new)
            self._facts = _facts_from(self._conn, self.base, self._scope,
                                      self._covered)
        return self._facts


def correction_facts_source(conn: sqlite3.Connection,
                            account_scope: str = "*") -> "_LazyFacts | None":
    """A lazy correction-facts source over ``conn`` for consumers that learn
    their lineages one at a time (R6), or None when the facts are
    unavailable (a decision without an op coordinate, a missing projection,
    a read error). The caller owns the read snapshot: hold one transaction
    across every use."""
    try:
        source = _LazyFacts(conn, account_scope)
    except sqlite3.Error:
        return None
    return None if source.base is None else source


def read_correction_facts(conn: "sqlite3.Connection | None" = None, *,
                          account_scope: str,
                          lineages=None) -> "CorrectionFacts | None":
    """The published correction facts for one account scope, or None.

    #1015 spec 5.3. ``account_scope`` is an account key, or ``"*"`` for the
    merged view. The corrections are every reviewed decision row whose
    observation the index attributes to that scope, plus every unresolved
    reference (no index row; ``account_key`` and ``captured_at`` None), which
    the lineage predicate must see to call the fit unavailable. The batches
    are every completed correction batch with its footprint, in journal
    order; the basis is their ids.

    ``lineages`` (R6) narrows the corrections to the rows those lineages can
    be affected by (`_lib_quota_lineage.row_demand`): the returned facts
    assess each of them exactly as the full facts do, and no other lineage.
    A set whose every basis holds each batch touching Claude reads no
    correction row at all. The batches, the basis and the D3 refusal are
    those of the full facts in every case.

    It reads only the published stats projections, in one read snapshot.
    With ``conn`` None it opens through
    `_cctally_store.stats_open_readonly_failfast` and closes what it opened,
    returning None whenever that opener does; a caller's connection is left
    open and its transaction state untouched. None also when a projection is
    missing (an index older than epoch 1022) or a decision row has no op
    coordinate.
    """
    owned = conn is None
    if owned:
        import _cctally_store
        conn = _cctally_store.stats_open_readonly_failfast()
        if conn is None:
            return None
    try:
        began = not conn.in_transaction
        if began:
            conn.execute("BEGIN")
        try:
            return _read_facts(conn, account_scope,
                               None if lineages is None else list(lineages))
        finally:
            if began and conn.in_transaction:
                conn.execute("ROLLBACK")
    except sqlite3.Error:
        return None
    finally:
        if owned:
            conn.close()


# --------------------------------------------------------------------------
# #1015 spec 5.6/5.7: effective transition supersession and the marker
# --------------------------------------------------------------------------

#: The one supersession reason this binary writes or derives.
SUPERSESSION_REASON = "reviewed-correction"

_EXPLICIT_SUPERSESSION_SQL = (
    "SELECT provider, account_key, effective_from, reason, corrections, "
    "superseded_at_utc FROM meter_rate_change_supersessions"
)
_TRANSITION_LINEAGE_SQL = (
    "SELECT provider, account_key, effective_from, detected_at_utc, "
    "fit_lineage FROM meter_rate_change_events"
)
_IDENTITY_CLAUSE = " WHERE provider = ? AND account_key = ? AND effective_from = ?"


@dataclasses.dataclass(frozen=True)
class Supersession:
    """One transition identity's effective supersession (spec 5.7).

    `superseded_at` is the explicit event's sweep clock, or None for a
    supersession derived from the transition's lineage alone (no event yet).
    `corrections` are the op ids; `observations` pairs each with its
    observation count."""

    reason: str
    superseded_at: "str | None"
    corrections: tuple
    observations: tuple
    explicit: bool


@dataclasses.dataclass(frozen=True)
class SupersessionState:
    """Every effectively superseded transition identity, or `available`
    False when the store or its correction facts could not be read."""

    by_identity: dict
    available: bool
    #: Identities whose lineage assessment was `unavailable` (an unresolved
    #: reference): never asserted superseded, never asserted clean.
    unavailable: frozenset = frozenset()


def _explicit_supersession(reason, corrections_json, superseded_at):
    try:
        parsed = json.loads(corrections_json or "[]")
    except ValueError:
        parsed = []
    pairs = tuple((str(c.get("opId")), int(c.get("observations") or 0))
                  for c in parsed if isinstance(c, dict))
    return Supersession(
        reason=str(reason or SUPERSESSION_REASON),
        superseded_at=None if superseded_at is None else str(superseded_at),
        corrections=tuple(op for op, _n in pairs), observations=pairs,
        explicit=True)


def transition_lineage_for(account_key, detected_at, fit_lineage=None) -> dict:
    """The lineage one transition is assessed under: its frozen
    `transitionLineage` when it verifies (a dict or its canonical JSON), else
    the conservative legacy lineage `(-inf, detected_at]` over the row's
    account, the `unattributed` sentinel reading as every account."""
    import _lib_accounts
    lineage = fit_lineage
    if isinstance(lineage, str):
        try:
            lineage = json.loads(lineage)
        except ValueError:
            lineage = None
    if ql.transition_lineage_verifies(lineage):
        return dict(lineage)
    return ql.legacy_transition_lineage(
        account_key=account_key, detected_at=detected_at,
        unattributed=_lib_accounts.UNATTRIBUTED)


def _derived_supersession(lineage, facts) -> "Supersession | str | None":
    assessment = ql.assess(lineage, facts, coord_key=_coordinate_order)
    if assessment.state == "unavailable":
        return "unavailable"
    if assessment.state != "tainted":
        return None
    pairs = tuple((str(op), int(n)) for op, n in assessment.corrections)
    return Supersession(
        reason=SUPERSESSION_REASON, superseded_at=None,
        corrections=tuple(op for op, _n in pairs), observations=pairs,
        explicit=False)


class _Unset:
    pass


_UNSET = _Unset()


def rate_change_supersession(conn, identity, *, lineage=None,
                             detected_at=None, facts=_UNSET) \
        -> "Supersession | None":
    """`identity`'s effective supersession on `conn`, or None (spec 5.7).

    An explicit `meter_rate_change_supersessions` row wins. Otherwise the
    identity is assessed under `lineage` when that frozen lineage verifies,
    else under its row's stored lineage, else (a row with a null lineage)
    under the conservative legacy lineage. An identity with neither a
    verifying lineage nor a row has nothing to assess and is not
    superseded. Unreadable facts, a missing table or any read error answer
    None: the caller keeps its pre-#1015 behaviour and the read paths refuse
    on their own. ``facts`` may be facts covering the resolved lineage, or a
    snapshot's lazy facts source (`correction_facts_source`), which reads
    the rows that lineage needs (R6); unset, they are read for it alone."""
    try:
        began = not conn.in_transaction
        if began:
            conn.execute("BEGIN")
        try:
            explicit = conn.execute(
                "SELECT reason, corrections, superseded_at_utc FROM "
                "meter_rate_change_supersessions" + _IDENTITY_CLAUSE,
                tuple(identity)).fetchone()
            if explicit is not None:
                return _explicit_supersession(*explicit)
            if not ql.transition_lineage_verifies(lineage):
                row = conn.execute(
                    "SELECT detected_at_utc, fit_lineage FROM "
                    "meter_rate_change_events" + _IDENTITY_CLAUSE,
                    tuple(identity)).fetchone()
                if row is None:
                    return None
                lineage = transition_lineage_for(identity[1], row[0], row[1])
            if facts is _UNSET:
                facts = _read_facts(conn, "*", [lineage])
            elif isinstance(facts, _LazyFacts):
                facts = facts.facts_for([lineage])
            if facts is None:
                return None
            derived = _derived_supersession(lineage, facts)
            return derived if isinstance(derived, Supersession) else None
        finally:
            if began and conn.in_transaction:
                conn.execute("ROLLBACK")
    except sqlite3.Error:
        return None


def read_rate_change_supersessions(conn: "sqlite3.Connection | None" = None,
                                   *, account_keys=None) -> SupersessionState:
    """Every effectively superseded transition identity (spec 5.7).

    One read snapshot over the explicit rows, the transition rows (limited
    to `account_keys` when given) and the correction facts. With `conn`
    None it opens through the fail-fast read-only opener and closes what it
    opened; a caller's connection is left open and its transaction state
    untouched. The derived form makes correction -> rebuild -> read safe
    before any sweep, and a rebuild before or after the explicit event gives
    the same answer."""
    owned = conn is None
    if owned:
        import _cctally_store
        conn = _cctally_store.stats_open_readonly_failfast()
        if conn is None:
            return SupersessionState({}, False)
    keys = None if account_keys is None else {str(k) for k in account_keys}
    try:
        began = not conn.in_transaction
        if began:
            conn.execute("BEGIN")
        try:
            by_identity: dict = {}
            for (provider, account, effective, reason, corrections,
                 superseded_at) in conn.execute(_EXPLICIT_SUPERSESSION_SQL):
                if keys is not None and str(account) not in keys:
                    continue
                by_identity[(str(provider), str(account), str(effective))] = \
                    _explicit_supersession(reason, corrections, superseded_at)
            pending = []
            for provider, account, effective, detected_at, lineage in \
                    conn.execute(_TRANSITION_LINEAGE_SQL):
                identity = (str(provider), str(account), str(effective))
                if identity in by_identity or (
                        keys is not None and identity[1] not in keys):
                    continue
                pending.append((identity, transition_lineage_for(
                    identity[1], detected_at, lineage)))
            # R6: the rows the assessed transition lineages need, no more.
            facts = _read_facts(conn, "*", [lin for _i, lin in pending])
            if facts is None:
                return SupersessionState(by_identity, False)
            unavailable = set()
            for identity, lineage in pending:
                derived = _derived_supersession(lineage, facts)
                if derived == "unavailable":
                    unavailable.add(identity)
                elif derived is not None:
                    by_identity[identity] = derived
            return SupersessionState(by_identity, True, frozenset(unavailable))
        finally:
            if began and conn.in_transaction:
                conn.execute("ROLLBACK")
    except sqlite3.Error:
        return SupersessionState({}, False)
    finally:
        if owned:
            conn.close()


#: `assess_active_rate_change` states.
MARKER_ACTIVE = "active"
MARKER_NONE = "none"
MARKER_UNAVAILABLE = "unavailable"


def assess_active_rate_change(account_key=None, *, regimes=None,
                              stats_conn=None) -> tuple:
    """`(state, active)`; see `_assess_marker`."""
    state, active, _clean = _assess_marker(
        account_key, regimes=regimes, stats_conn=stats_conn)
    return state, active


def _assess_marker(account_key=None, *, regimes=None,
                   stats_conn=None) -> tuple:
    """`(state, active, pair)` of §6.6's marker over CLEAN regimes (spec 5.6).

    `active_rate_change` over the stored regimes first: when it finds no
    open successor (the steady state) nothing else is read, so the marker
    costs one small read only where a change is active. Otherwise the
    correction facts are read (the caller's stats connection, or the
    fail-fast opener) and the active pair of the clean regimes is found
    newest first (`active_pair_among`), assessing only the regimes that can
    decide it (R6): the same answer as pairing every clean regime, without
    assessing closed history behind the active pair. The surviving active
    transition is suppressed when its identity is superseded. The rows are
    read only as far as the assessed lineages need, starting with the
    unfiltered active pair and its successor's frozen transition lineage.
    Unreadable facts yield `unavailable`: a change that cannot be verified
    is not asserted."""
    import _lib_meter_rate_change as mrc
    glue = _cctally_quota_model
    if regimes is None:
        state = glue.read_stored_state_readonly()
        regimes = [] if state is None else glue.stored_regimes(
            state, account_key)
    regimes = list(regimes or ())
    if mrc.active_rate_change(regimes) is None:
        return MARKER_NONE, None, ()
    owned = stats_conn is None
    conn = stats_conn
    if owned:
        import _cctally_store
        conn = _cctally_store.stats_open_readonly_failfast()
        if conn is None:
            return MARKER_UNAVAILABLE, None, ()
    try:
        began = not conn.in_transaction
        if began:
            conn.execute("BEGIN")
        try:
            return _marker_in_snapshot(conn, account_key, regimes)
        finally:
            if began and conn.in_transaction:
                conn.execute("ROLLBACK")
    except sqlite3.Error:
        return MARKER_UNAVAILABLE, None, ()
    finally:
        if owned:
            conn.close()


def _marker_in_snapshot(conn, account_key, regimes) -> tuple:
    import _lib_meter_rate_change as mrc
    glue = _cctally_quota_model
    scope = _state_key(account_key)
    facts = _LazyFacts(conn, "*")
    if facts.base is None:
        return MARKER_UNAVAILABLE, None, ()
    lineages: dict = {}

    def lineage(regime):
        if id(regime) not in lineages:
            lineages[id(regime)] = regime_lineage_of(regime, scope=scope)
        return lineages[id(regime)]

    def transition(regime):
        frozen = regime.get(ql.TRANSITION_LINEAGE_KEY)
        return frozen if ql.transition_lineage_verifies(frozen) else None

    # Prefetch what the all-clean answer reads: the unfiltered active pair
    # and its successor's frozen lineage, so the common case is one read.
    unfiltered = mrc.active_pair_among(regimes, lambda _r: True)
    if unfiltered is not None:
        facts.facts_for([lin for lin in (
            lineage(unfiltered[0]), lineage(unfiltered[1]),
            transition(unfiltered[1])) if lin is not None])

    def is_clean(regime) -> bool:
        if isinstance(regime.get("supersededByCorrection"), dict):
            return False
        return ql.assess(lineage(regime), facts.facts_for([lineage(regime)]),
                         coord_key=_coordinate_order).state == "clean"

    pair = mrc.active_pair_among(regimes, is_clean)
    if pair is None:
        return MARKER_NONE, None, ()
    active = mrc.active_rate_change(pair)
    successor = pair[1]
    identity = ("claude", glue._alert_account_key(account_key),
                active["effective_from"])
    if rate_change_supersession(
            conn, identity, lineage=successor.get(ql.TRANSITION_LINEAGE_KEY),
            facts=facts) is not None:
        return MARKER_NONE, None, ()
    return MARKER_ACTIVE, active, tuple(pair)


def clean_active_rate_change(account_key=None, *, regimes=None,
                             stats_conn=None) -> "dict | None":
    """§6.6's marker predicate over clean, unsuperseded regimes, or None
    (none active, superseded, or unverifiable)."""
    _state, active = assess_active_rate_change(
        account_key, regimes=regimes, stats_conn=stats_conn)
    return active


def marker_regimes(account_key=None, *, regimes=None,
                   stats_conn=None) -> tuple:
    """The regimes a regime-pair marker consumer may read: the clean active
    pair `(predecessor, successor)` when its transition survives the
    assessment, else (). `active_rate_change` over it is the marker over
    every clean regime (R6: closed history behind the pair is never
    assessed)."""
    _state, _active, clean = _assess_marker(
        account_key, regimes=regimes, stats_conn=stats_conn)
    return clean
