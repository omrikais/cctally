"""I/O glue for `cctally quota` (#661 S1).

The arithmetic lives in the pure kernel `bin/_lib_quota_model.py`, which has
no clock, no file access and no database access. This module supplies
everything the kernel refuses to do for itself: it parses instants, reads both
stores coherently, resolves account scope, owns the coefficient-era catalogue,
persists the fitted calibration, renders text and JSON, and maps the result to
an exit code.

The split follows `_lib_pricing_check.py` / `_cctally_pricing_check.py`.

Spec: docs/superpowers/specs/2026-08-28-661-s1-quota-model-and-calibration.md
(each part is normative over the parts before it; Part V is the current one).
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import textwrap

import _cctally_core
import _cctally_store
import _lib_quota_model as qm
from _lib_quota_model import (  # re-exported for callers and tests
    BlockingReason, CalibrationEvidence, CalibrationStatus, Verdict,
)

UTC = dt.timezone.utc


def _cctally():
    """Resolve the current `cctally` module at call time."""
    return sys.modules["cctally"]


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


# ---------------------------------------------------------------------------
# The coefficient-era catalogue (spec section 6).
#
# The kernel's token-class coefficients were fitted on Opus-5-dominated
# traffic. `docs/quota-model.md` records the changeover and states that the
# tool "refuses to treat earlier data as calibrated": the Opus 4.8 era meters
# at a materially different units-per-point, which is a BUDGET difference the
# shared coefficients cannot absorb. Section 6 therefore says such eras are
# not fitted and persist as gaps carrying `unvalidated-coefficient-era`, a
# cause distinct from `unsupported-model-mix`.
#
# This catalogue is the glue's, not the kernel's, exactly as section 6 and
# Part III section 28 require: the kernel has no producer for that status and
# receives it through `analyse(extra_statuses=...)`.
# ---------------------------------------------------------------------------
#: First UTC date of the earliest validated coefficient era. Every
#: observation before it is covered by no era and is never fitted; the
#: analysis itself starts at the start of the era the clock selects (#978).
SUPPORTED_COMPOSITION_FROM: dt.date = qm.COEFFICIENT_ERAS[0].start

#: The history before every validated era, published first in
#: `method.coefficientEras` so a later reader can re-test the boundary rather
#: than inherit it. It carries `id: None` and no weights, because no
#: coefficient set is validated for it.
UNVALIDATED_ERA_RECORD: dict = {
    "id": None,
    "from": None,
    "until": SUPPORTED_COMPOSITION_FROM.isoformat(),
    "state": "unvalidated",
    "provenance": (
        "docs/quota-model.md: the Opus 4.8 era fits at about 1,989,101 "
        "units per point against 2.4M for Opus 5, a budget difference the "
        "shared token-class coefficients do not absorb. The shipped "
        "estimator gives 1,265,184 to 1,479,027 over the same weeks, and "
        "spec section 6 declines to adjudicate between the two."
    ),
}


def published_coefficient_eras() -> list:
    """The complete era catalogue in order, as `method.coefficientEras`.

    The unvalidated record first, then each kernel era with its interval,
    provenance and token-class weights. Built from `qm.COEFFICIENT_ERAS` on
    every call, so the published table can never drift from the one the
    kernel weights with.
    """
    # #991: each era also names the reference family its meter-evidence
    # rate bounds are relative to, and the bounds; the unvalidated history
    # has neither.
    unvalidated = dict(UNVALIDATED_ERA_RECORD)
    unvalidated.update(referenceFamily=None, familyRateBounds=[])
    out = [unvalidated]
    for era in qm.COEFFICIENT_ERAS:
        out.append({
            "id": era.id,
            "from": era.start.isoformat(),
            "until": None if era.end is None else era.end.isoformat(),
            "state": "validated",
            "provenance": era.provenance,
            "tokenClassWeights": era.weight_map(),
            "referenceFamily": era.reference_family,
            "familyRateBounds": _rate_bounds_json(era),
        })
    return out


def _rate_bounds_json(era) -> list:
    """An era's family rate bounds on the wire, in table order."""
    if era is None:
        return []
    return [{"family": b.family, "low": b.low, "high": b.high,
             "provenance": b.provenance} for b in era.family_rate_bounds]


def select_era(now: "dt.datetime") -> "qm.CoefficientEra | None":
    """The one era an analysis at `now` is fitted in (#978 spec 4.3).

    The clock decides, never the data: the real clock, or `CCTALLY_AS_OF` in
    harnesses. None before 2026-07-25, where no era is validated.
    """
    return qm.era_for_instant(now)


#: Snapshot sources that are synthetic by construction and are never read as
#: observations (spec section 4). A closed committed set: `record-credit`
#: writes a post-credit reading by construction, and the two repair sources
#: were observed once each on the production store with no tracked writer.
SYNTHETIC_SNAPSHOT_SOURCES: tuple = (
    "record-credit", "remediation", "manual-recovery",
)

#: Sources this repository knows how to produce. An unrecognized value is
#: RETAINED and counted in a diagnostic rather than silently dropped, because
#: dropping it would remove a genuine observation on the strength of a name.
KNOWN_SNAPSHOT_SOURCES: frozenset = frozenset(
    {"statusline", "tampermonkey", "api", "userscript"}
)


# ---------------------------------------------------------------------------
# Instants.
# ---------------------------------------------------------------------------
class MalformedRetainedInstant(ValueError):
    """A retained timestamp cannot be interpreted as an instant."""


def parse_instant(value, label: str = "timestamp"):
    """Parse a stored ISO-8601 string to an aware UTC datetime.

    The kernel rejects naive datetimes, and text ordering mis-sorts: `+`
    (0x2B) sorts before `.` (0x2E) against fractional-second stamps, and
    `value[:10]` buckets by LOCAL date. So every stored string is parsed here
    and compared as an instant afterwards.

    A value carrying no offset is read as UTC, which is what every writer in
    this repository stores; it is not read as host-local.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except (ValueError, OverflowError) as exc:
        raise MalformedRetainedInstant(
            f"{label}: retained value {value!r} is not an ISO-8601 instant"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_date_argument(value, label: str):
    """Parse a `--since` / `--watch-from` argument to an aware UTC instant.

    A bare date means that date's first instant in UTC. Raises `ValueError`,
    which the command turns into exit 2 — argument errors only.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label}: empty value")
    try:
        if len(text) == 10:
            return dt.datetime.combine(
                dt.date.fromisoformat(text), dt.time(0, 0), tzinfo=UTC)
        return parse_instant(text, label)
    except ValueError as exc:
        raise ValueError(f"{label}: {text!r} is not an ISO-8601 date or "
                         f"timestamp ({exc})") from exc


# ---------------------------------------------------------------------------
# The two-store read (spec section 16).
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class LoadResult:
    """One account's parsed population, or a typed reason there is none."""

    entries: tuple = ()
    snapshots: tuple = ()
    credits: tuple = ()
    newest_entry_at: "dt.datetime | None" = None
    analysis_start: "dt.datetime | None" = None
    diagnostics: dict = dataclasses.field(default_factory=dict)
    #: `None` on a usable read; a `CalibrationStatus` the glue contributes
    #: through `analyse(extra_statuses=...)` otherwise.
    status: "CalibrationStatus | None" = None
    cause: "str | None" = None
    #: #1015 spec 5.2/5.5: the correction facts and the completed batch ids
    #: (the fit's `basis`), read inside the same coherence bracket, on the
    #: same stats connection and generation as the fit inputs. None facts
    #: mean "not read" (an unusable load).
    correction_facts: object = None
    correction_basis: frozenset = frozenset()


def _account_clause(column: str, account_key):
    """SQL fragment + params scoping one column to `account_key`.

    `None` is the merged view and adds no clause. The reserved
    `unattributed` sentinel matches BOTH the literal stamp and a NULL, which
    is this repository's read rule.
    """
    if account_key is None:
        return "", []
    import _lib_accounts
    if account_key == _lib_accounts.UNATTRIBUTED:
        return f" AND ({column} IS NULL OR {column} = ?)", [account_key]
    return f" AND {column} = ?", [account_key]


def _store_probe(conn, path) -> str:
    """A cheap fingerprint of one store's current state.

    `PRAGMA data_version` advances when ANOTHER connection commits, which is
    exactly the event that would make two reads describe two different states.
    The file size is folded in so a change the pragma cannot see on a fresh
    connection is still visible.
    """
    try:
        version = conn.execute("PRAGMA data_version").fetchone()[0]
    except (sqlite3.Error, IndexError, TypeError):
        version = "?"
    try:
        size = path.stat().st_size
    except OSError:
        size = -1
    return f"{version}:{size}"


def _probe_bundle(stats_conn, cache_conn) -> tuple:
    """Both stores' signatures, taken as ONE observation.

    `bin/_cctally_diagnosis_sources.py` probes each component around its own
    read. That protocol cannot see the hazard section 16 names, because a
    cache ingest landing BETWEEN the stats read and the cache read falls
    before the cache component's own opening probe. The pair is therefore
    taken around the whole read rather than around each half, which is
    strictly stronger and costs one extra pragma per attempt.
    """
    return (
        _store_probe(stats_conn, _cctally_core.DB_PATH),
        _store_probe(cache_conn, _cctally_core.CACHE_DB_PATH),
    )


#: How far below the analysis start the SQL text prefilter reaches. Stored
#: timestamps may carry a non-UTC offset, and text comparison then orders
#: them by local wall time: `2026-09-22T03:00:00+05:00` sorts at or after
#: `2026-09-22T00:00:00+00:00` while naming an instant before it, and
#: `2026-09-21T22:30:00-05:00` sorts before it while naming an instant after
#: it. Any real offset is under a day, so a one-day margin keeps every
#: in-range row, and the exact bound is then applied to the parsed instant.
#: The kernel refuses an out-of-era instant outright (#978 spec 4.2,
#: invariant 5), so the bound has to be exact rather than textual.
_TEXT_PREFILTER_MARGIN = dt.timedelta(days=1)


def _text_prefilter_floor(start) -> str:
    """The coarse SQL lower bound for an exact instant bound `start`."""
    return (start - _TEXT_PREFILTER_MARGIN).isoformat()


def _read_stats_component(conn, account_key, start):
    """Meter readings and authoritative credit instants for one account.

    Returns `(snapshots, credits, diagnostics)`. Synthetic sources are
    excluded at the query as a closed committed set; an unrecognized source is
    retained and counted.
    """
    # `weekly_observation_held = 0` (#769 S11, #824): these rows ARE the meter
    # readings the coefficients are fitted against. A held row repeats the
    # latest genuine reading under a later capture instant, so admitting one
    # would feed the fit a flat interval that no upstream meter reported —
    # a fabricated observation rather than a missing one.
    placeholders = ",".join("?" for _ in SYNTHETIC_SNAPSHOT_SOURCES)
    sql = (
        "SELECT id, captured_at_utc, week_start_at, week_start_date,"
        " weekly_percent, source FROM weekly_usage_snapshots"
        f" WHERE weekly_observation_held = 0 AND source NOT IN ({placeholders})"
    )
    params: list = list(SYNTHETIC_SNAPSHOT_SOURCES)
    if start is not None:
        sql += " AND captured_at_utc >= ?"
        params.append(_text_prefilter_floor(start))
    clause, extra = _account_clause("account_key", account_key)
    sql += clause
    params.extend(extra)

    snapshots = []
    unrecognised: dict = {}
    legacy_anchors = 0
    for rowid, captured, week_at, week_date, percent, source in conn.execute(
            sql, params):
        at = parse_instant(captured, "captured_at_utc")
        if at is None:
            continue
        if start is not None and at < start:
            continue
        anchor = parse_instant(week_at, "week_start_at")
        if anchor is None:
            # A pre-`week_start_at` row. Its date-only boundary is read as
            # that date's first UTC instant rather than dropped: dropping it
            # would remove a genuine observation, and the kernel canonicalizes
            # every anchor to the nearest hour anyway.
            anchor = parse_instant(f"{str(week_date)[:10]}T00:00:00+00:00",
                                   "week_start_date")
            if anchor is None:
                continue
            legacy_anchors += 1
        if source not in KNOWN_SNAPSHOT_SOURCES:
            unrecognised[source] = unrecognised.get(source, 0) + 1
        snapshots.append(qm.SnapshotRecord(
            at=at, week_start=anchor, percent=float(percent),
            source=str(source), rowid=int(rowid)))

    credits = []
    for table, column, kind in (
            ("week_reset_events", "effective_reset_at_utc", "reset"),
            ("weekly_credit_floors", "effective_at_utc", "floor")):
        csql = f"SELECT {column} FROM {table} WHERE 1=1"
        cparams: list = []
        cclause, cextra = _account_clause("account_key", account_key)
        csql += cclause
        cparams.extend(cextra)
        for (raw,) in conn.execute(csql, cparams):
            at = parse_instant(raw, column)
            if at is not None and (start is None or at >= start):
                credits.append(qm.CreditRecord(at=at, kind=kind))

    diagnostics = {
        "unrecognisedSnapshotSources": unrecognised,
        "legacyDateOnlyWeekAnchors": legacy_anchors,
    }
    return snapshots, credits, diagnostics


def _read_cache_component(conn, account_key, start, end=None):
    """Priced requests for one account, plus the store-wide ingest tail.

    Rows are bounded to `[start, end)` on the parsed instant before the
    fast-mode count, so `fastModeEntriesExcluded` describes the analysed
    window; `end` is a closed era's exclusive end, or None for an open one.

    `newest_entry_at` is deliberately NOT account-scoped. It answers "has the
    ingest finished covering this day", which is a property of the store; an
    account-scoped maximum would report every day of a dormant account as
    `no-local-history`.
    """
    sql = (
        "SELECT timestamp_utc, model, input_tokens, output_tokens,"
        " cache_create_tokens, cache_create_1h_tokens, cache_read_tokens,"
        " speed"
        " FROM session_entries WHERE 1=1"
    )
    params: list = []
    if start is not None:
        sql += " AND timestamp_utc >= ?"
        params.append(_text_prefilter_floor(start))
    clause, extra = _account_clause("account_key", account_key)
    sql += clause
    params.extend(extra)
    sql += " ORDER BY timestamp_utc, id"

    entries = []
    fast_excluded = 0
    for row in conn.execute(sql, params):
        at = parse_instant(row[0], "timestamp_utc")
        if at is None:
            continue
        if start is not None and at < start:
            continue
        if end is not None and at >= end:
            continue
        if row[7] == "fast":
            fast_excluded += 1
            continue
        entries.append(qm.EntryRecord(
            at=at, model=str(row[1] or ""), fresh=row[2] or 0,
            output=row[3] or 0, cache_create_total=row[4] or 0,
            cache_1h=row[5], cache_read=row[6] or 0))
    newest = parse_instant(
        conn.execute("SELECT MAX(timestamp_utc) FROM session_entries")
        .fetchone()[0], "timestamp_utc")
    return entries, newest, fast_excluded


def _retained_snapshot_span(conn, account_key):
    """`(earliest, latest)` captured instants ignoring the era floor.

    The `unvalidated-coefficient-era` finding is a statement about retained
    rows, so it needs the span the floor hides. An empty store returns
    `(None, None)` and says nothing.
    """
    # Same population as `_read_stats_component` (#769 S11, #824). The finding
    # this span qualifies is a statement about the readings the model used, so
    # the two must count the same rows; a span computed over a wider set would
    # report an era the fit never saw.
    placeholders = ",".join("?" for _ in SYNTHETIC_SNAPSHOT_SOURCES)
    sql = ("SELECT MIN(captured_at_utc), MAX(captured_at_utc)"
           " FROM weekly_usage_snapshots"
           " WHERE weekly_observation_held = 0"
           f" AND source NOT IN ({placeholders})")
    params: list = list(SYNTHETIC_SNAPSHOT_SOURCES)
    clause, extra = _account_clause("account_key", account_key)
    sql += clause
    params.extend(extra)
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None, None
    return parse_instant(row[0]), parse_instant(row[1])


def era_floor_instant(era) -> "dt.datetime":
    """The first instant `era`'s coefficients are validated for."""
    return qm.era_bounds(era)[0]


def resolve_analysis_start(since, era):
    """`max(--since, the selected era's start)`.

    A `--since` earlier than the era does not widen the window: section 6
    says an era whose composition the coefficients are not validated for is
    not fitted, and #978 spec 4.3 says neither `--since` nor `--watch-from`
    reopens a preceding era, so a flag cannot licence fitting one.
    """
    floor = era_floor_instant(era)
    if since is None:
        return floor
    return max(since, floor)


def week_spans_era(week: "CurrentWeek", era) -> bool:
    """Is the subscription week not wholly inside `era`?

    Either the week starts before the era, or, for a closed era a historical
    clock selected, it ends after the era's exclusive end. Such a week's
    units would come from two eras, so its modelled figures are withheld
    (#978 spec 4.3).
    """
    start, end = qm.era_bounds(era)
    return week.anchor < start or (end is not None and week.end > end)


def current_week_units(population, era) -> float:
    """Summed weighted units of the current week's general-meter entries.

    A dedicated-pool family contributes nothing, and an entry whose
    cache-write split is unknown is skipped, as `population_units` does. An
    entry `era` does not cover raises `CoefficientEraMismatch` instead of
    being skipped (#978 spec 4.2, invariant 5): the week's units are never
    silently clipped to the part one era covers.
    """
    total = 0.0
    for entry in population:
        if qm.family_participation(
                qm.normalize_family(entry.model)) != "general":
            continue
        weighed = qm.weigh_entry(entry, era)
        if weighed.cause == qm.ERA_UNAVAILABLE:
            raise qm.CoefficientEraMismatch(
                f"entry.at {entry.at.isoformat()} lies outside coefficient "
                f"era {era.id!r}")
        if weighed.units is None:
            continue
        total += weighed.units
    return total


def _confine(rows, end):
    """The rows strictly before a closed era's exclusive `end`.

    The lower bound is already applied at the query; an open era (`end`
    None) is bounded by the clock exactly as before #978.
    """
    if end is None:
        return list(rows)
    return [row for row in rows if row.at < end]


def load_population(account_key, *, since, now, era) -> LoadResult:
    """Read both stores coherently and return one account's population.

    The stores are opened through the guarded helpers, so `CCTALLY_DATA_DIR`,
    the current schemas, migrations, corruption handling and the busy timeout
    all apply. Every failure is caught and reported as a typed `unavailable`
    result; an uncaught `OperationalError` from a store read is never allowed
    to reach the user.

    `era` is the era `select_era(now)` chose. Snapshots, credits and entries
    are confined to `[analysis start, era end)` before anything downstream
    segments, fences or fits them (#978 spec 4.3). A None era — a clock
    before every validated era — reads nothing and returns the
    `unvalidated-coefficient-era` status, because no coefficient set could
    weigh what it would read.
    """
    qm.require_aware(now, "now")
    if era is None:
        return LoadResult(
            status=CalibrationStatus.UNVALIDATED_COEFFICIENT_ERA,
            cause="history-predates-supported-composition")
    start = resolve_analysis_start(since, era)
    era_end = qm.era_bounds(era)[1]
    stats_conn = None
    cache_conn = None
    try:
        try:
            stats_conn = _cctally_core.open_db()
            cache_conn = _cctally().open_cache_db()
        except Exception:
            return LoadResult(analysis_start=start,
                              status=CalibrationStatus.UNAVAILABLE,
                              cause="store-unavailable")

        payload = None
        for attempt in (0, 1):
            before = _probe_bundle(stats_conn, cache_conn)
            try:
                earliest, _latest = _retained_snapshot_span(
                    stats_conn, account_key)
                snapshots, credits, diagnostics = _read_stats_component(
                    stats_conn, account_key, start)
                entries, newest, fast_excluded = _read_cache_component(
                    cache_conn, account_key, start, era_end)
                facts = _read_lineage_facts(stats_conn, account_key)
            except sqlite3.Error:
                return LoadResult(analysis_start=start,
                                  status=CalibrationStatus.UNAVAILABLE,
                                  cause="store-unavailable")
            except (MalformedRetainedInstant, ValueError, TypeError):
                return LoadResult(analysis_start=start,
                                  status=CalibrationStatus.UNAVAILABLE,
                                  cause="malformed-retained-instant")
            after = _probe_bundle(stats_conn, cache_conn)
            if before == after:
                # #825/#832 spec 6: the stats half of a population this
                # external plan carries into ingest must come from the current
                # interpretation. The coherence bracket proves the reads saw
                # one unchanged store (a republication moves the probe), so the
                # identity of that store is checked at the bracket's edge.
                try:
                    with _cctally_store.current_stats_snapshot(stats_conn):
                        pass
                except _cctally_store.StatsIndexNotCurrent:
                    return LoadResult(analysis_start=start,
                                      status=CalibrationStatus.UNAVAILABLE,
                                      cause="index-not-current")
                if facts is None:
                    # A current index whose correction projections cannot be
                    # read (a damaged index): a fit published now could not
                    # name the corrections its inputs reflect (#1015).
                    return LoadResult(analysis_start=start,
                                      status=CalibrationStatus.UNAVAILABLE,
                                      cause="store-unavailable")
                payload = (earliest, snapshots, credits, diagnostics,
                           entries, newest, fast_excluded, facts)
                break
        if payload is None:
            # A store that moves twice while we read it is under active
            # write, and publishing a fit over it would describe a state that
            # never existed as a whole — in the direction that manufactures a
            # rate change.
            return LoadResult(analysis_start=start,
                              status=CalibrationStatus.UNAVAILABLE,
                              cause="generation-incoherent")
    finally:
        for conn in (stats_conn, cache_conn):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    earliest, snapshots, credits, diagnostics, entries, newest, \
        fast_excluded, facts = payload
    snapshots = _confine(snapshots, era_end)
    credits = _confine(credits, era_end)
    entries = _confine(entries, era_end)
    diagnostics = dict(diagnostics)
    diagnostics["analysisStart"] = start.isoformat()
    diagnostics["fastModeEntriesExcluded"] = fast_excluded
    if not snapshots and earliest is not None and earliest < start:
        # Retained history exists, and all of it predates the era the shipped
        # coefficients are validated for. That is not thin evidence: it is
        # evidence the model has no validated coefficients for, and reporting
        # it as `insufficient-history` would tell the user to wait for days
        # that would never help.
        return LoadResult(
            analysis_start=start, newest_entry_at=newest,
            diagnostics=diagnostics,
            status=CalibrationStatus.UNVALIDATED_COEFFICIENT_ERA,
            cause="history-predates-supported-composition",
            correction_facts=facts, correction_basis=facts.basis)
    return LoadResult(
        entries=tuple(entries), snapshots=tuple(snapshots),
        credits=tuple(credits), newest_entry_at=newest,
        analysis_start=start, diagnostics=diagnostics,
        correction_facts=facts, correction_basis=facts.basis)


def _read_lineage_facts(stats_conn, account_key):
    """The correction facts for one account's scope, on the bracket's own
    stats connection (#1015 spec 5.2), or None when they cannot be read."""
    import _cctally_quota_calibration
    return _cctally_quota_calibration.read_correction_facts(
        stats_conn, account_scope=_state_key(account_key))


# ---------------------------------------------------------------------------
# Account scoping (spec section 17).
# ---------------------------------------------------------------------------
def accounts_are_decorated() -> bool:
    """The #341 R8 gate: does the Claude provider render account decoration?

    True only above one REAL account. A lone `unattributed` bucket, or a
    single real account, decorates nothing and keeps today's output.
    """
    import _cctally_account
    try:
        conn = _cctally_core.open_db()
    except Exception:
        return False
    try:
        return _cctally_account.provider_is_decorated(conn, "claude")
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _real_account_keys() -> list:
    import _cctally_account
    import _lib_accounts
    try:
        conn = _cctally_core.open_db()
    except Exception:
        return []
    try:
        rows = _cctally_account.load_accounts(conn, "claude")
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [str(row["account_key"]) for row in rows
            if row["account_key"] not in (_lib_accounts.UNATTRIBUTED,
                                          _lib_accounts.VENDOR_WIDE)]


def _unattributed_bucket_has_rows() -> bool:
    import _lib_accounts
    try:
        conn = _cctally_core.open_db()
    except Exception:
        return False
    try:
        placeholders = ",".join("?" for _ in SYNTHETIC_SNAPSHOT_SOURCES)
        # HELD-INCLUSIVE, decided rather than overlooked (#769 S11, #824).
        # This asks whether the bucket contains any real observation at all,
        # not what its weekly value is, and a held row is a real write by a
        # real tick. The answer cannot differ in practice either way, because a
        # held row implies a non-held basis in the same bucket.
        row = conn.execute(
            "SELECT 1 FROM weekly_usage_snapshots WHERE source NOT IN "
            f"({placeholders}) AND (account_key IS NULL OR account_key = ?)"
            " LIMIT 1",
            list(SYNTHETIC_SNAPSHOT_SOURCES) + [_lib_accounts.UNATTRIBUTED],
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return row is not None


def resolve_accounts(args) -> tuple:
    """Which populations this invocation analyses, and an exit code or None.

    `--account` narrows to one, resolved case-insensitively through the shared
    ref resolver; an ambiguous or unknown ref is a native usage error at exit
    2 with the candidates on stderr.

    Without `--account`, the answer depends on the R8 gate rather than on a
    merge. At one real account, or a lone unattributed bucket, the single
    analysed population is the merged view `None` — which is byte-identical to
    today's behaviour and is not a merge of two meters. Above one real
    account, each account is analysed independently, because two accounts have
    independent weekly meters and one budget fitted across both is meaningless.
    """
    import _cctally_account
    import _lib_accounts
    ref = getattr(args, "account", None)
    if ref is not None:
        key, code = _cctally_account.resolve_account_filter(args, "claude")
        if code is not None:
            return [], code
        return [key], None
    if not accounts_are_decorated():
        return [None], None
    keys = _real_account_keys()
    if _unattributed_bucket_has_rows():
        keys.append(_lib_accounts.UNATTRIBUTED)
    return keys, None


def account_display_label(account_key) -> "str | None":
    """The label the account decoration renders, or None for the merged view."""
    if account_key is None:
        return None
    import _cctally_account
    try:
        conn = _cctally_core.open_db()
    except Exception:
        return str(account_key)[:8]
    try:
        return _cctally_account.display_account_label(conn, account_key)
    except sqlite3.Error:
        return str(account_key)[:8]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Persistence (spec sections 7 and 20).
#
# The fitted calibration is machine-owned internal state and lives in neither
# database. `cache.db` is declared fully re-derivable and `cache-sync
# --rebuild` clears keys there; a plain `stats.db` table is re-materialized
# from the journal on rebuild; a journal-backed record with a stats fold is
# forbidden, because the thirteen-migration stats registry is frozen and a
# schema change is an epoch bump. Living in neither is what satisfies the
# durability requirement by construction.
# ---------------------------------------------------------------------------
#: Bumped when the shape below changes incompatibly. A file stamped ABOVE this
#: value is version-ahead and is preserved, never overwritten.
CALIBRATION_STATE_SCHEMA_VERSION: int = 1

CALIBRATION_FILENAME: str = "quota-calibrations.json"
CALIBRATION_LOCK_FILENAME: str = "quota-calibrations.lock"

#: The state key for the merged view — the single population a one-account or
#: lone-unattributed install analyses. `_lib_accounts.VENDOR_WIDE` already
#: means "all accounts", so it is reused rather than a fourth sentinel minted.
MERGED_STATE_KEY: str = "*"


def calibration_path():
    return _cctally_core.APP_DIR / CALIBRATION_FILENAME


def calibration_lock_path():
    return _cctally_core.APP_DIR / CALIBRATION_LOCK_FILENAME


def _state_key(account_key) -> str:
    return MERGED_STATE_KEY if account_key is None else str(account_key)


def empty_calibration_state() -> dict:
    return {"schemaVersion": CALIBRATION_STATE_SCHEMA_VERSION, "accounts": {}}


@dataclasses.dataclass(frozen=True)
class LoadedCalibrations:
    """The stored state, plus what had to be done to read it.

    The plan's interface block names a bare `dict`. A bare dict cannot carry
    the quarantine path, and spec section 20 requires the command to report
    `unavailable` WITH that path — a rename the user is never told about
    would be indistinguishable from the silent overwrite the section forbids.
    """

    state: dict
    quarantined: "str | None" = None
    unreadable: bool = False


@contextlib.contextmanager
def calibration_lock():
    """Exclusive `flock` around the calibration file's read-modify-write.

    LOCK ORDER: this lock is a LEAF. No database connection, no other `flock`
    and no open SQLite transaction may be held while it is acquired, and it
    takes nothing else while held. The command therefore closes both stores
    before it persists anything.

    Blocking rather than non-blocking, following `config_writer_lock`: the
    write is millisecond-scale, so a brief wait is preferable to silently
    dropping a writer's update.
    """
    with _leaf_lock(calibration_lock_path()):
        yield


@contextlib.contextmanager
def _leaf_lock(path):
    """A blocking exclusive `flock` on `path`, held for the yielded block.

    LEAF means the same thing for every caller: nothing else may be held while
    it is acquired, and it takes nothing else while held.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _quarantine(path, now) -> str:
    """Rename a malformed or version-ahead file aside and return its new path.

    Silently rewriting it would destroy the only surviving record of budgets
    whose source rows have since been pruned, so the file is preserved under a
    timestamped suffix and a fresh one is started beside it.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.quarantined-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(
            f"{path.name}.quarantined-{stamp}.{counter}")
        counter += 1
    os.replace(str(path), str(candidate))
    return str(candidate)


def load_calibrations(*, now=None) -> LoadedCalibrations:
    """Read the stored state, quarantining a file this binary cannot own.

    `now` supplies the quarantine suffix and is required whenever a
    quarantine is possible; it defaults to the wall clock only so a read-only
    caller need not supply one.
    """
    path = calibration_path()
    if not path.exists():
        return LoadedCalibrations(empty_calibration_state())
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return LoadedCalibrations(empty_calibration_state(), unreadable=True)
    when = now or dt.datetime.now(UTC)
    try:
        state = json.loads(raw)
    except ValueError:
        return LoadedCalibrations(empty_calibration_state(),
                                  quarantined=_quarantine(path, when))
    version = state.get("schemaVersion") if isinstance(state, dict) else None
    if (not isinstance(state, dict) or not isinstance(version, int)
            or isinstance(version, bool)
            or version > CALIBRATION_STATE_SCHEMA_VERSION
            or not isinstance(state.get("accounts"), dict)):
        return LoadedCalibrations(empty_calibration_state(),
                                  quarantined=_quarantine(path, when))
    return LoadedCalibrations(state)


def save_calibrations(state: dict) -> None:
    """Write the state atomically, more strictly than `save_config` does.

    `_cctally_config.save_config` is a related precedent rather than the
    specification: it uses a PID-only temporary name at mode 0644 and does NOT
    fsync the parent directory, so a crash after the rename can lose the
    rename itself.

    The caller holds `calibration_lock()`.
    """
    _atomic_write_json(calibration_path(), state)


def _atomic_write_json(path, document: dict) -> None:
    """Create with `O_EXCL` under a name unique per process AND attempt, write
    at 0600, fsync the contents, `os.replace` into position, then fsync the
    parent directory.

    Shared by the calibration state and the #695 delivery ledger. A partially
    written ledger is indistinguishable from a malformed one and would
    disable the delivery mechanism, so it gets the same protocol rather than
    a weaker one. The caller holds that file's own leaf lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_temporaries(path)
    payload = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    attempt = 0
    while True:
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{attempt}")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            attempt += 1
            if attempt > 64:
                raise
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
    # The rename itself is only durable once the DIRECTORY entry is synced.
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _sweep_stale_temporaries(path) -> None:
    """Remove temporary files a crashed writer left behind.

    Safe because the caller holds the exclusive lock, so no other writer owns
    one. Without this a crash at the wrong instant leaks a file per attempt.
    """
    prefix = f"{path.name}.tmp."
    try:
        names = list(path.parent.iterdir())
    except OSError:
        return
    for candidate in names:
        if candidate.name.startswith(prefix):
            try:
                candidate.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# #695 — the metering-rate notification-delivery ledger.
#
# The pure document semantics live in `bin/_lib_rate_change_delivery.py`. This
# section owns the file, its leaf lock, the atomic write, the stats read that
# says what has been recorded, the one-time seed and the compare-and-set.
# ---------------------------------------------------------------------------
def _rate_change_delivery():
    """The pure ledger kernel, via the call-time sibling accessor."""
    return _cctally()._load_sibling("_lib_rate_change_delivery")


def delivery_path():
    return _cctally_core.APP_DIR / _rate_change_delivery().FILENAME


def delivery_lock_path():
    return _cctally_core.APP_DIR / _rate_change_delivery().LOCK_FILENAME


@contextlib.contextmanager
def delivery_lock():
    """Exclusive `flock` around the #695 delivery ledger.

    LOCK ORDER: a LEAF, exactly like `calibration_lock`. It is legal to
    acquire it after a stats lock has been taken and fully RELEASED — the law
    forbids holding a later lock while acquiring an earlier one, not
    re-acquiring a released leaf — and `cmd_quota` relies on that, because the
    mark must follow the commit it describes.
    """
    with _leaf_lock(delivery_lock_path()):
        yield


@dataclasses.dataclass(frozen=True)
class LoadedDelivery:
    """The ledger, plus whether this run may use it.

    `usable=False` is NOT "empty". An empty reading would make every recorded
    identity look owed and fire the whole history, which is the failure the
    ledger exists to prevent, so an unusable ledger suppresses the seed and
    the sweep instead.
    """

    state: dict
    usable: bool = True
    reason: "str | None" = None
    #: True when this run could not COMPLETE THE SEED even though the file
    #: itself parsed. The remedy differs and must be reported differently: a
    #: stats read that fails during a rebuild is the common case, and telling
    #: the user to repair or remove a healthy ledger would be wrong advice.
    seed_deferred: bool = False


def load_delivery_state() -> LoadedDelivery:
    """Read and validate the ledger. Never quarantines, never overwrites."""
    d = _rate_change_delivery()
    path = delivery_path()
    if not path.exists():
        return LoadedDelivery(d.empty_state())
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return LoadedDelivery(d.empty_state(), usable=False,
                              reason=f"could not be read ({exc})")
    try:
        document = json.loads(raw)
    except ValueError:
        return LoadedDelivery(d.empty_state(), usable=False,
                              reason="is not valid JSON")
    validated = d.validate(document)
    if validated is None:
        return LoadedDelivery(d.empty_state(), usable=False,
                              reason="does not match the expected schema")
    return LoadedDelivery(validated)


def save_delivery_state(state: dict) -> None:
    """Atomically replace the ledger. The caller holds `delivery_lock()`."""
    _atomic_write_json(delivery_path(), state)


def recorded_rate_change_identities(account_keys=None) -> tuple:
    """Every `(provider, account_key, effective_from)` with a durable row.

    The AUTHORITATIVE source of what has been recorded (#695 section 5.1).
    The calibration file's enumerated pairs must not be used for this: a
    `reset_calibration` or a quarantine removes them while the row survives,
    which would strand an owed identity forever.

    Raises on a store it cannot read; both callers decide what a failure
    means, and neither may treat it as "nothing is recorded".
    """
    keys = None if account_keys is None else sorted(
        {str(k) for k in account_keys})
    if keys is not None and not keys:
        return ()
    conn = None
    try:
        conn = _cctally_core.open_db()
        if keys is None:
            rows = conn.execute(
                "SELECT provider, account_key, effective_from "
                "FROM meter_rate_change_events").fetchall()
        else:
            marks = ",".join("?" for _ in keys)
            rows = conn.execute(
                "SELECT provider, account_key, effective_from "
                f"FROM meter_rate_change_events WHERE account_key IN ({marks})",
                keys).fetchall()
        return tuple(sorted(
            (str(p), str(a), str(e)) for p, a, e in rows))
    finally:
        if conn is not None:
            conn.close()


def claim_delivery(identity) -> str:
    """Compare and set one identity into the ledger. Returns a CLAIM_* code.

    Only `CLAIM_WON` authorizes a dispatch. `CLAIM_WRITE_FAILED` deliberately
    leaves the identity owed, so a later run retries it rather than losing the
    notification to a transient write error.
    """
    d = _rate_change_delivery()
    with delivery_lock():
        loaded = load_delivery_state()
        if not loaded.usable:
            return d.CLAIM_UNUSABLE
        if tuple(identity) in d.decided_set(loaded.state):
            return d.CLAIM_ALREADY_DECIDED
        try:
            save_delivery_state(
                d.with_decided(loaded.state, [tuple(identity)]))
        except OSError:
            return d.CLAIM_WRITE_FAILED
        return d.CLAIM_WON


def release_delivery_claim(identity) -> bool:
    """Hand back this run's own won claim for `identity`, whose notification
    was NOT handed off (#1015 spec 5.7: the supersession recheck could not
    take the stats maintenance read guard). The identity is owed again, so
    the owed sweep of a later run retries it. False when the ledger could
    not be rewritten; the claim then stands, exactly as a crash after the
    mark would leave it."""
    d = _rate_change_delivery()
    with delivery_lock():
        loaded = load_delivery_state()
        if not loaded.usable:
            return False
        if tuple(identity) not in d.decided_set(loaded.state):
            return True
        try:
            save_delivery_state(d.without_decided(loaded.state,
                                                  [tuple(identity)]))
        except OSError:
            return False
        return True


def seed_delivery_state() -> LoadedDelivery:
    """Mark every currently recorded identity as decided, once (#695 sec 4).

    Without this, the first run of the upgraded binary sees every historical
    row as owed and fires the whole history.

    THREE phases, and the stats read sits between two separate lock holds
    because the ledger lock is a leaf and may hold no database connection. The
    phase-3 re-read is what makes two concurrent seeders safe: overwriting
    with the phase-2 snapshot would discard a mark another process wrote in
    between. A row a `hook-tick` inserts between phases 2 and 3 falls outside
    the snapshot and is therefore SWEPT rather than seeded away, which is the
    safe direction.

    A stats failure leaves the flag unset and reports the ledger unusable for
    this run, so the command degrades to no sweep rather than to an error.
    """
    from _cctally_db import StatsRebuildDeferred
    d = _rate_change_delivery()
    with delivery_lock():
        loaded = load_delivery_state()
        if not loaded.usable or loaded.state.get("seededFromStats"):
            return loaded
    try:
        snapshot = recorded_rate_change_identities()
    except (Exception, StatsRebuildDeferred) as exc:  # noqa: BLE001
        return LoadedDelivery(
            d.empty_state(), usable=False, seed_deferred=True,
            reason=f"the recorded transitions could not be read ({exc})")
    with delivery_lock():
        loaded = load_delivery_state()
        if not loaded.usable or loaded.state.get("seededFromStats"):
            return loaded
        state = d.with_decided(loaded.state, snapshot, seeded=True)
        try:
            save_delivery_state(state)
        except OSError as exc:
            return LoadedDelivery(d.empty_state(), usable=False,
                                  reason=f"could not be written ({exc})")
        return LoadedDelivery(state)


@dataclasses.dataclass(frozen=True)
class PersistMode:
    """Everything the pure reducer needs that is not the analysis itself.

    `kind` is `"automatic"` or `"diagnostic"`. A diagnostic run — one passing
    `--watch-from`, or a `--since` narrower than the stored regime's span —
    reports its result and writes nothing, so the durable history stays a
    function of unmodified runs rather than of whatever the operator last
    asked to inspect.
    """

    kind: str
    account_key: "str | None"
    now: "dt.datetime"
    fingerprint: str
    regime_start: "dt.datetime"
    #: The id of the coefficient era the clock selected (#978), written on
    #: every regime. None when the clock precedes every validated era, which
    #: makes the run persist nothing.
    coefficient_era: "str | None"
    #: #1015 spec 5.5: the correction facts read inside `load_population`'s
    #: coherence bracket, which `reduce_state` assesses every stored regime
    #: against, and the completed batch ids a fit written by this run
    #: records as its `basis`. None facts mean "not assessed": a constructed
    #: mode keeps the pre-#1015 reduction (lineage is still written).
    correction_facts: object = None
    correction_basis: frozenset = frozenset()


def persist_mode(result, *, now) -> PersistMode:
    """The `PersistMode` one account's sweep reduces under (#1015 spec 5.5).

    The correction facts and the basis come from the account's own
    `LoadResult`, so they describe the generation the fit was computed from
    and are never read again after fitting."""
    return PersistMode(
        kind=result.mode_kind, account_key=result.account_key, now=now,
        fingerprint=qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
        regime_start=result.load.analysis_start,
        coefficient_era=None if result.era is None else result.era.id,
        correction_facts=result.load.correction_facts,
        correction_basis=frozenset(result.load.correction_basis or ()))


def _interval_json(interval):
    if interval is None:
        return None
    return {"lo": interval.lo, "hi": interval.hi}


def _regime_from(evidence, *, effective_from, mode, status):
    """One durable regime record built from an AVAILABLE evidence value."""
    return {
        "effectiveFrom": effective_from.astimezone(UTC).isoformat(),
        "effectiveUntil": None,
        "fingerprint": mode.fingerprint,
        "algorithmRevision": qm.QUOTA_MODEL_ALGORITHM_REVISION,
        "coefficientEra": mode.coefficient_era,
        "unitsPerPoint": evidence.value,
        "interval": _interval_json(evidence.interval),
        "support": ({"days": evidence.support.days,
                     "segments": evidence.support.segments}
                    if evidence.support is not None else None),
        "status": status,
        "asOf": mode.now.astimezone(UTC).isoformat(),
        "qualifications": list(evidence.qualifications),
    }


def _decorate(regime, analysis, *, fit_family_shares):
    """Attach the composition the regime was fitted under.

    `fit_family_shares` is the fit-weighted family composition of the fit
    THIS regime records (#991 spec 4.5): the baseline q for a baseline
    regime, the published q for a successor or a no-split regime. A
    predecessor is never decorated again, so it keeps what it stored.
    """
    regime["familyShares"] = dict(analysis.family_shares)
    regime["classShares"] = dict(analysis.class_shares)
    regime["familyRadius"] = analysis.family_radius
    regime["classRadius"] = analysis.class_radius
    regime["fitFamilyShares"] = (None if fit_family_shares is None
                                 else dict(fit_family_shares))
    return regime


def _open_regime(regimes):
    for regime in reversed(regimes):
        if regime.get("effectiveUntil") is None:
            return regime
    return None


def read_stored_state_readonly() -> "dict | None":
    """The persisted calibration state, read WITHOUT quarantining anything.

    `load_calibrations` renames a malformed or version-ahead file aside
    through `_quarantine`, so a read-only caller — `doctor` is the documented
    one — must not use it. This returns None rather than an empty state on
    any failure, so a caller can tell "nothing readable" from "nothing
    stored"; the two render differently.

    No lock is taken. `save_calibrations` writes through `os.replace` in the
    same directory, so a lock-free reader always sees a complete old or new
    inode, and a flock here would let a writer stall a read-only command.
    """
    try:
        raw = calibration_path().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(state, dict) or not isinstance(
            state.get("accounts"), dict):
        return None
    return state


def stored_regimes(state: dict, account_key) -> list:
    """The stored regimes for one account, oldest first. Never mutated."""
    accounts = state.get("accounts") or {}
    bucket = accounts.get(_state_key(account_key)) or {}
    regimes = bucket.get("regimes") or []
    return [dict(r) for r in regimes if isinstance(r, dict)]


# #825/#832 spec 5.4 "Calibration sidecar". Recovery (#689) journals a
# transition an earlier run persisted and failed to record. A transition a
# previous-release process persisted must never be republished as a
# current-stamped `meter_rate_change`, so every regime THIS binary writes
# carries the writer generation plus a digest over its own content: a legacy
# process's in-place `update()` of a stamped regime keeps the stamp keys but
# breaks the digest, so it reads as legacy too.
_REGIME_WRITER_KEY = "wpg"
_REGIME_WRITER_DIGEST_KEY = "wpgDigest"
# #797/#823/#840 spec 4.6 B5: recovery also requires revision-specific
# provenance. A revision-1 process (the previous release) writes a regime
# with a verifying `wpg` stamp, so that stamp alone cannot tell its
# transitions from this binary's. Every regime this binary writes also
# carries `wpr` and a revision-specific digest over its content without any
# of the four writer keys; a previous-release in-place `update()` keeps
# `wpr` but cannot recompute that digest.
_REGIME_REVISION_KEY = "wpr"
_REGIME_REVISION_DIGEST_KEY = "wprDigest"
_REGIME_WRITER_KEYS = (_REGIME_WRITER_KEY, _REGIME_WRITER_DIGEST_KEY,
                       _REGIME_REVISION_KEY, _REGIME_REVISION_DIGEST_KEY)
# #1019/#1014 spec 4.7 C5: and revision-3 provenance. 1.116.1 writes a regime
# whose `wpg` and `wpr` digests verify, so every regime this binary writes
# also carries `wpx` and a revision-3 digest over its content without any of
# the six writer keys. 1.116.1 strips only its four keys when it stamps, so
# its in-place `update()` keeps a stale `wpxDigest`, and a regime it created
# has none; neither is recovered as current-writer state. The `wpr` and `wpg`
# digests are computed as before (over the `wpx` keys too), so 1.116.1 still
# verifies this binary's stamps.
_REGIME_SUCCESSOR_KEY = "wpx"
_REGIME_SUCCESSOR_DIGEST_KEY = "wpxDigest"
_REGIME_ALL_WRITER_KEYS = _REGIME_WRITER_KEYS + (
    _REGIME_SUCCESSOR_KEY, _REGIME_SUCCESSOR_DIGEST_KEY)


def _regime_content_digest(regime: dict) -> str:
    # The revision-1 digest, unchanged: over everything but the two `wpg`
    # keys, so the previous release still verifies this binary's stamp.
    core = {k: v for k, v in regime.items()
            if k not in (_REGIME_WRITER_KEY, _REGIME_WRITER_DIGEST_KEY)}
    data = json.dumps(core, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def _regime_revision_digest(regime: dict) -> str:
    import _lib_account_protocol
    core = {k: v for k, v in regime.items() if k not in _REGIME_WRITER_KEYS}
    data = json.dumps(
        {"revision": _lib_account_protocol.PROTOCOL_REVISION,
         "regime": core},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def _regime_successor_digest(regime: dict) -> str:
    import _lib_account_protocol
    core = {k: v for k, v in regime.items()
            if k not in _REGIME_ALL_WRITER_KEYS}
    data = json.dumps(
        {"revision": _lib_account_protocol.SUCCESSOR_REVISION,
         "regime": core},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def stamp_regime(regime: dict) -> dict:
    """``regime`` stamped as written by the current protocol generation,
    revision and successor revision."""
    import _lib_account_protocol
    out = {k: v for k, v in regime.items()
           if k not in _REGIME_ALL_WRITER_KEYS}
    out[_REGIME_SUCCESSOR_KEY] = _lib_account_protocol.SUCCESSOR_REVISION
    out[_REGIME_SUCCESSOR_DIGEST_KEY] = _regime_successor_digest(out)
    out[_REGIME_REVISION_KEY] = _lib_account_protocol.PROTOCOL_REVISION
    out[_REGIME_REVISION_DIGEST_KEY] = _regime_revision_digest(out)
    out[_REGIME_WRITER_KEY] = _lib_account_protocol.PROTOCOL_GENERATION
    out[_REGIME_WRITER_DIGEST_KEY] = _regime_content_digest(out)
    return out


def regime_writer_is_current(regime) -> bool:
    import _lib_account_protocol
    return (isinstance(regime, dict)
            and regime.get(_REGIME_WRITER_KEY)
            == _lib_account_protocol.PROTOCOL_GENERATION
            and regime.get(_REGIME_WRITER_DIGEST_KEY)
            == _regime_content_digest(regime)
            and _lib_account_protocol.has_current_revision(
                {_lib_account_protocol.REVISION_KEY:
                 regime.get(_REGIME_REVISION_KEY)})
            and regime.get(_REGIME_REVISION_DIGEST_KEY)
            == _regime_revision_digest(regime)
            and _lib_account_protocol.has_successor_revision(
                {_lib_account_protocol.SUCCESSOR_REVISION_KEY:
                 regime.get(_REGIME_SUCCESSOR_KEY)})
            and regime.get(_REGIME_SUCCESSOR_DIGEST_KEY)
            == _regime_successor_digest(regime))


def stamp_current_writes(stored: dict, reduced: dict, account_key) -> dict:
    """``reduced`` with every regime this run created or changed stamped.

    A regime identical to the stored one at its position is left exactly as
    it was — a legacy regime this run did not touch is never laundered into a
    current one."""
    before = stored_regimes(stored, account_key)
    after = stored_regimes(reduced, account_key)
    regimes = []
    for index, regime in enumerate(after):
        if index < len(before) and regime == before[index]:
            regimes.append(regime)
        else:
            regimes.append(stamp_regime(regime))
    out = dict(reduced)
    accounts = dict(out.get("accounts") or {})
    accounts[_state_key(account_key)] = {
        **(accounts.get(_state_key(account_key)) or {}), "regimes": regimes}
    out["accounts"] = accounts
    return out


def recovery_candidates(state: dict, account_key, *, detected_at,
                        facts=None) -> tuple:
    """The persisted transitions #689 recovery may journal.

    Every qualified pair of CLEAN regimes (#1015 spec 5.6) whose SUCCESSOR —
    the regime that constitutes the transition — carries a verifying
    current-writer stamp AND a verifying frozen `transitionLineage`. A
    transition a previous-release process persisted (or edited in place) is
    withheld. The predecessor is ordinary history: every regime from before
    the upgrade is legacy-written, and the first current change after it must
    still be recoverable.

    #1015 spec 5.6/5.7: recovery trusts the transition's own lineage, never
    the successor's `fitLineage` (a later no-change refit replaces that). An
    identity that lineage finds superseded under `facts` is not recovered,
    so a refit's newer basis can never rehabilitate an earlier detection.
    What is returned is the frozen DESCRIPTOR, so the recovered event's
    numbers (rates, severity, evidence), windows and basis are the
    detection's own. A writer-verified successor with NO `transitionLineage`
    (written by a release before #1015) is recovered as a legacy transition
    instead: from the regime pair, unless a correction up to this run's clock
    supersedes it. `detected_at` is still the recovery clock, as #689's
    contract states for a recovered row; the detection's own clock stays in
    the frozen lineage the event journals.
    """
    mrc = _cctally()._load_sibling("_lib_meter_rate_change")
    ql = _lineage_kernel()
    import _cctally_quota_calibration as qcg
    import _lib_accounts
    scope = _state_key(account_key)
    alert_key = _alert_account_key(account_key)
    regimes = clean_regimes(stored_regimes(state, account_key), facts, scope)
    out = []
    legacy_starts = set()
    pairs = mrc._adjacent_pairs(regimes)
    for start in sorted(pairs):
        _predecessor, successor = pairs[start]
        if not regime_writer_is_current(successor):
            continue
        lineage = successor.get(ql.TRANSITION_LINEAGE_KEY)
        if lineage is None:
            # F8: a writer-verified successor a release before #1015 wrote
            # (#1019 stamps, no frozen lineage) recovers as a legacy
            # transition (spec 5.7): basis [] and a window ending at this
            # recovery run's clock, so any earlier correction in its scope
            # supersedes it; its values come from the regime pair, exactly
            # as recovery read them before #1015.
            legacy = ql.legacy_transition_lineage(
                account_key=alert_key, detected_at=detected_at,
                unattributed=_lib_accounts.UNATTRIBUTED)
            if facts is None or ql.assess(
                    legacy, facts,
                    coord_key=qcg._coordinate_order).state == "clean":
                legacy_starts.add(start)
            continue
        if not ql.transition_lineage_verifies(lineage):
            continue
        if facts is not None and ql.assess(
                lineage, facts,
                coord_key=qcg._coordinate_order).state != "clean":
            continue
        transition = _transition_from_descriptor(lineage, detected_at)
        if transition is not None:
            out.append(transition)
    if legacy_starts:
        out.extend(
            t for t in mrc.enumerate_transitions(
                regimes, provider="claude", account_key=alert_key,
                detected_at=detected_at)
            if mrc._instant(t.effective_from) in legacy_starts)
        out.sort(key=lambda t: mrc._instant(t.effective_from))
    return tuple(out)


def _transition_from_descriptor(lineage, detected_at):
    """The `RateChangeTransition` a frozen `transitionLineage` describes,
    stamped at `detected_at`, or None when its descriptor cannot be read."""
    mrc = _cctally()._load_sibling("_lib_meter_rate_change")
    d = lineage.get("descriptor") or {}
    try:
        days = d.get("baseline_withheld_days")
        return mrc.RateChangeTransition(
            provider=str(d["provider"]), account_key=str(d["account_key"]),
            effective_from=str(d["effective_from"]),
            previous_units_per_point=float(d["previous_units_per_point"]),
            new_units_per_point=float(d["new_units_per_point"]),
            severity=str(d["severity"]), detected_at=str(detected_at),
            withholding_status=d.get("withholding_status"),
            detector_input_causes=d.get("detector_input_causes"),
            composition_provenance=d.get("composition_provenance"),
            baseline_withheld_days=None if days is None else int(days),
            fit_lineage=dict(lineage))
    except (KeyError, TypeError, ValueError):
        return None


def recorded_regime(state: dict, account_key) -> "dict | None":
    """The open regime a run would present as the durable prior, or None."""
    return _open_regime(stored_regimes(state, account_key))


def _canonical_enum_values(members) -> "str | None":
    """A frozenset of typed enum members as a canonical JSON array (#690).

    Enum VALUES, never `repr` or `str` of the member, so a sixth
    `CompositionProvenance` origin added later cannot silently alias onto an
    existing one. Sorted and separator-pinned, so one set has exactly one
    serialization and two rows recording the same evidence compare equal.

    None in, None out — and that is the whole three-state contract: None means
    the evidence was never assessed or is unrecoverable, while `"[]"` means it
    WAS assessed and found no such origin. A path that cannot judge must not
    publish the empty set, because a reader would take it as a measurement.
    """
    if members is None:
        return None
    try:
        values = sorted(getattr(m, "value", m) for m in members)
    except TypeError:
        return None
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"))


def _baseline_withheld_days(analysis) -> "int | None":
    """`analysis.baseline_fit.population["withheld"]`, or None (#692).

    The count of observations in `fenced` that carry a cause and fall strictly
    BEFORE `window_start` — exactly the pre-split evidence loss the permit
    predicate cannot see, because `analyse` sets
    `window_start = detector.split_date` on a confirmed change, so the
    published `detector_input_causes` covers only the successor window.

    Read from the fit and NEVER inferred from `detector_input_causes`, from
    diagnostics text, or from the successor status. A missing or unreadable
    population yields None rather than 0: zero is a measurement meaning a
    clean baseline, and reporting it for an unknown would be a fabricated
    reassurance.
    """
    fit = getattr(analysis, "baseline_fit", None)
    population = getattr(fit, "population", None)
    if not isinstance(population, dict):
        return None
    try:
        return int(population["withheld"])
    except (KeyError, TypeError, ValueError):
        return None


def _analysis_evidence(analysis) -> tuple:
    """`(detector_input_causes, composition_provenance, baseline_days)` (#690).

    TOTAL over any input, for the reason `transition_persistence_permitted`
    states about itself: `persist_and_detect` is public glue whose `analysis`
    argument is not type-checked, and a test that stubs the reducer away
    reaches this line with a bare object. Every field is read through
    `getattr` with a None default, so an analysis that does not publish one
    yields the honest "not assessed" state instead of raising and taking the
    whole persistence step down.
    """
    return (
        _canonical_enum_values(getattr(analysis, "detector_input_causes", None)),
        _canonical_enum_values(getattr(analysis, "composition_provenance", None)),
        _baseline_withheld_days(analysis),
    )


def _transition_persistence_admitted(analysis) -> bool:
    """The COMPLETE #688 admission condition, in one place (#691).

    Two functions asked this question and each wrote its own answer.
    `reduce_state` spelled the permit predicate AND the successor-fit limb;
    `persist_and_detect` spelled the permit predicate alone when stamping the
    descriptor's `withholding_status`. Two spellings of one condition, in two
    functions, with no shared definition, is the arrangement that silently
    diverges — and the reducer's extra limb is invisible from the stamp site,
    so nothing at the stamp would have reported the disagreement.

    The two cannot disagree today, which is why extracting this is a
    refactor and not a fix. `qm.transition_persistence_permitted` requires a
    WITHHELD verdict under a blocking status, and that is mutually exclusive
    with the `fitted_ok` limb that is the only other route to a save, so the
    stamp site is unreachable with the permit true and the successor fit
    unavailable. The value of one definition is that a future edit widening
    either limb reaches both call sites, in the direction — permitting —
    that is the harmful one on this axis.

    Total over any input, because the predicate it wraps is: `watch_fit` is
    only dereferenced once the permit has already accepted the object as a
    `QuotaAnalysis`.
    """
    return (qm.transition_persistence_permitted(analysis)
            and analysis.watch_fit.state == "available")


def reduce_state(stored: dict, analysis, mode: PersistMode) -> dict:
    """The durable history as a pure function of `(stored, analysis, mode)`.

    Pure: no clock, no file access, no mutation of `stored`. That is what
    makes the history a function of its inputs rather than of call order, and
    it is why every transition below is testable without a filesystem.

    Transitions (spec section 20):

    * A diagnostic run writes nothing.
    * A confirmed rate change closes the open regime at the split's first
      instant and opens a successor from it. The first run that discovers a
      split persists BOTH sides, so the baseline is created if it is absent.
    * A trustworthy fit with no detected change updates the open regime in
      place, or opens the first one.
    * A stored regime under a different fingerprint, or a different
      coefficient era (#978), is marked `stale` and a new regime is APPENDED;
      its own value is never rewritten, because that value was true under its
      own constants. A run whose clock selects an earlier era than the open
      regime's writes nothing.
    * A withheld analysis that `qm.transition_persistence_permitted` admits
      persists both sides of the split exactly as a confirmed change does,
      with the successor stamped under the withholding status (#688). This
      is the one transition keyed off the DETECTOR rather than the verdict.
    * A non-trustworthy analysis writes nothing.
    """
    if mode.kind != "automatic":
        return stored
    key = _state_key(mode.account_key)
    regimes = stored_regimes(stored, mode.account_key)
    stored_bucket = (stored.get("accounts") or {}).get(key)
    bucket = dict(stored_bucket) if isinstance(stored_bucket, dict) else {}
    archive = [dict(a) for a in bucket.get(_ARCHIVE_KEY) or ()
               if isinstance(a, dict)]
    now_iso = mode.now.astimezone(UTC).isoformat()
    facts = mode.correction_facts
    # #1015 spec 5.5 item 1: every stored regime is assessed against the
    # facts, over the state this reducer was handed (the locked re-read), and
    # a tainted one is annotated whatever else this run does.
    assessments = _assess_regimes(regimes, facts, key)
    annotated = _annotate(regimes, assessments, now_iso)
    tainted = {id(r) for r, a in zip(regimes, assessments)
               if not _is_clean(r, a)}
    before_clean = [dict(r) for r in regimes if id(r) not in tainted]

    def finish(changed: bool) -> dict:
        if not changed:
            return stored
        out_bucket = dict(bucket)
        out_bucket["regimes"] = regimes
        if archive:
            out_bucket[_ARCHIVE_KEY] = archive
        if out_bucket == stored_bucket:
            return stored
        accounts = dict(stored.get("accounts") or {})
        accounts[key] = out_bucket
        return {"schemaVersion": CALIBRATION_STATE_SCHEMA_VERSION,
                "accounts": accounts}

    confirmed = (analysis.verdict is Verdict.RATE_CHANGE_DETECTED
                 and analysis.detector.split_date is not None)
    fitted_ok = (analysis.status is CalibrationStatus.OK
                 and analysis.fitted.state == "available")
    successor_ok = (confirmed and analysis.watch_fit.state == "available")
    # #688: the durable transition keys off DETECTION for the one class of
    # withholding that concerns predicting forward. Additive rather than a
    # replacement: a bare detector-only predicate would newly admit
    # `fragmented-history` and `unstable-fit`, which `resolve_outcome`
    # deliberately refuses.
    permitted = _transition_persistence_admitted(analysis)
    confirmed = confirmed or permitted
    successor_ok = successor_ok or permitted
    open_regime = _open_regime(regimes)
    if not fitted_ok and not successor_ok:
        # #1015 spec 5.5 item 2: a withheld analysis over a tainted open
        # regime does not close it; it records the attempt, so the refusal
        # can state why no recalibration happened.
        attempted = (facts is not None and open_regime is not None
                     and id(open_regime) in tainted
                     and _record_attempt(open_regime, analysis, mode))
        return finish(annotated or attempted)
    mode_era = qm.era_by_id(mode.coefficient_era)
    if mode_era is None:
        # No validated era: nothing this run fitted could name one.
        return finish(annotated)

    # #978 spec 4.5: a run whose clock selects an EARLIER era than the open
    # regime's — a `CCTALLY_AS_OF` inside the Opus 5 era against a stored
    # Opus 5.5 calibration — never replaces the present with the past.
    open_era = (qm.era_by_id(open_regime.get("coefficientEra"))
                if open_regime is not None else None)
    if open_era is not None and open_era.start > mode_era.start:
        return finish(annotated)
    # A stored regime fitted under different constants, or in a different
    # coefficient era (#978), is marked stale and left otherwise untouched.
    # It is marked HERE, together with the append below, because section 20
    # also says a run that writes nothing leaves the stored state alone — and
    # a run reaching this point is writing. The era comparison uses `.get()`,
    # so an old-build regime with no era rolls over here too (its fingerprint
    # already differs).
    if open_regime is not None and (
            open_regime.get("fingerprint") != mode.fingerprint
            or open_regime.get("coefficientEra") != mode.coefficient_era):
        open_regime["status"] = CalibrationStatus.STALE.value
        open_regime["effectiveUntil"] = mode.now.astimezone(UTC).isoformat()
        open_regime = None

    if confirmed:
        boundary = dt.datetime.combine(
            analysis.detector.split_date, dt.time(0, 0), tzinfo=UTC)
        boundary_iso = boundary.isoformat()
        # An open regime that already STARTS at the boundary is the successor
        # this run is re-discovering, not a predecessor to close. Closing it
        # would set its `effectiveUntil` to its own `effectiveFrom` and the
        # append below would then add a third regime on every subsequent run.
        open_from = (parse_instant(open_regime.get("effectiveFrom"))
                     if open_regime is not None else None)
        predecessor = (open_regime if open_from is not None
                       and open_from < boundary else None)
        baseline_available = analysis.baseline_fit.state == "available"
        if predecessor is not None and id(predecessor) in tainted:
            # #1015 spec 5.5 item 4: a tainted open regime is closed at the
            # boundary with its status unchanged and is never a comparison
            # predecessor. This run's own clean baseline is persisted beside
            # it, so the successor's predecessor is a current-run fit.
            predecessor["effectiveUntil"] = boundary_iso
            if baseline_available:
                regimes.append(_baseline_regime(analysis, mode, boundary))
        elif predecessor is not None:
            predecessor["effectiveUntil"] = boundary_iso
            predecessor["status"] = CalibrationStatus.OK.value
        elif open_regime is None and baseline_available:
            # The first run that discovers a split persists both sides, so a
            # store with no prior regime still records what the rate WAS.
            regimes.append(_baseline_regime(analysis, mode, boundary))
        if successor_ok:
            successor = _decorate(_regime_from(
                analysis.watch_fit, effective_from=boundary, mode=mode,
                status=analysis.status.value), analysis,
                fit_family_shares=analysis.fit_family_shares)
            existing = next((r for r in regimes
                             if r.get("effectiveFrom") == boundary_iso
                             and r.get("effectiveUntil") is None), None)
            target = _place_fit(regimes, existing, successor, tainted,
                                archive, now_iso)
            _attach_fit_lineage(
                target, mode, "successor", s=boundary,
                estimator=_estimator(
                    getattr(analysis, "published_day_bounds", None)))
    elif fitted_ok:
        fresh = _decorate(_regime_from(
            analysis.fitted,
            effective_from=(
                parse_instant(open_regime["effectiveFrom"])
                if open_regime is not None
                and open_regime.get("effectiveFrom") else mode.regime_start),
            mode=mode, status=CalibrationStatus.OK.value), analysis,
            fit_family_shares=analysis.fit_family_shares)
        target = _place_fit(regimes, open_regime, fresh, tainted, archive,
                            now_iso)
        _attach_fit_lineage(
            target, mode, "nosplit",
            estimator=_estimator(
                getattr(analysis, "published_day_bounds", None)))
    else:
        return finish(annotated)

    _attach_transition_lineage(before_clean, regimes, analysis, mode)
    return finish(True)


# ---------------------------------------------------------------------------
# #1015: correction-to-fit lineage in the reducer (spec 5.2, 5.5, 5.6).
# ---------------------------------------------------------------------------
_ANNOTATION_KEY = "supersededByCorrection"
_ATTEMPT_KEY = "recalibrationAttempt"
_ARCHIVE_KEY = "archivedFits"


def _lineage_kernel():
    import _lib_quota_lineage
    return _lib_quota_lineage


def _assess_regimes(regimes, facts, scope) -> list:
    """One assessment per regime, or None for each when `facts` is None
    (a constructed mode that does not assess)."""
    if facts is None:
        return [None] * len(regimes)
    import _cctally_quota_calibration as qcg
    return [qcg.assess_regime(r, facts, scope=scope) for r in regimes]


def _is_clean(regime, assessment) -> bool:
    """Spec 5.6: only a regime assessed `clean` and never annotated takes
    part in pairing, detection, recovery and the marker. The annotation is
    terminal: a fit's basis never grows, so a tainted fit stays tainted."""
    if isinstance(regime.get(_ANNOTATION_KEY), dict):
        return False
    return assessment is None or assessment.state == "clean"


def clean_regimes(regimes, facts, scope) -> list:
    """`regimes` without every tainted, unavailable or annotated regime."""
    return [r for r, a in zip(regimes, _assess_regimes(regimes, facts, scope))
            if _is_clean(r, a)]


def _correction_json(corrections) -> list:
    return [{"opId": str(op), "observations": int(count)}
            for op, count in corrections]


def _annotate(regimes, assessments, now_iso) -> bool:
    """Spec 5.5 item 1. True when an annotation was added."""
    changed = False
    for regime, assessment in zip(regimes, assessments):
        if assessment is None or assessment.state != "tainted":
            continue
        if isinstance(regime.get(_ANNOTATION_KEY), dict):
            continue
        lineage = regime.get(_lineage_kernel().FIT_LINEAGE_KEY)
        fit_id = (lineage.get("fitId") if isinstance(lineage, dict)
                  and _lineage_kernel().fit_lineage_verifies(regime)
                  else _legacy_fit_id(regime))
        regime[_ANNOTATION_KEY] = {
            "fitId": fit_id,
            "corrections": _correction_json(assessment.corrections),
            "assessedAt": now_iso}
        changed = True
    return changed


def _legacy_fit_id(regime) -> str:
    ql = _lineage_kernel()
    return ql.legacy_lineage(regime, None)["fitId"]


def _record_attempt(regime, analysis, mode) -> bool:
    """Spec 5.5 item 2. True when the attempt record changed. An attempt
    with the same status over the same basis is the same attempt, so a
    repeated withheld sweep leaves the record (and the file) unchanged."""
    status = getattr(getattr(analysis, "status", None), "value", None)
    basis = sorted(str(b) for b in mode.correction_basis)
    prior = regime.get(_ATTEMPT_KEY)
    if isinstance(prior, dict) and prior.get("status") == status \
            and prior.get("basis") == basis:
        return False
    regime[_ATTEMPT_KEY] = {"status": status, "basis": basis,
                            "asOf": mode.now.astimezone(UTC).isoformat()}
    return True


def _place_fit(regimes, target, fresh, tainted, archive, now_iso) -> dict:
    """Write one fresh fit over `target` (or append it) and return the
    record that now holds it.

    A clean target is updated in place, as before #1015. A tainted one is
    first archived whole, with its annotation, outside `regimes` so it can
    never form an adjacent pair, and is then REPLACED at the same
    `effectiveFrom` with no annotation and no attempt (spec 5.5 item 3).
    `transitionLineage` is immutable and survives either way (spec 5.7).
    """
    if target is None:
        regimes.append(fresh)
        return fresh
    if id(target) in tainted:
        archive.append({"regime": dict(target), "archivedAt": now_iso})
        frozen = target.get(_lineage_kernel().TRANSITION_LINEAGE_KEY)
        target.clear()
        target.update(fresh)
        if frozen is not None:
            target[_lineage_kernel().TRANSITION_LINEAGE_KEY] = frozen
        return target
    target.update(fresh)
    return target


def _baseline_regime(analysis, mode, boundary) -> dict:
    baseline = _decorate(_regime_from(
        analysis.baseline_fit, effective_from=mode.regime_start,
        mode=mode, status=CalibrationStatus.OK.value), analysis,
        fit_family_shares=analysis.baseline_fit_family_shares)
    baseline["effectiveUntil"] = boundary.isoformat()
    _attach_fit_lineage(
        baseline, mode, "baseline", s=boundary,
        estimator=_estimator(getattr(analysis, "baseline_day_bounds", None)))
    return baseline


def _estimator(bounds) -> "dict | None":
    """`fitLineage.estimator`: the half-open UTC span of the fit's days."""
    if not bounds:
        return None
    first, last = bounds
    start = dt.datetime.combine(first, dt.time(0, 0), tzinfo=UTC)
    until = dt.datetime.combine(last, dt.time(0, 0), tzinfo=UTC) \
        + dt.timedelta(days=1)
    return {"from": start.isoformat(), "until": until.isoformat()}


def _attach_fit_lineage(regime, mode, kind, *, s=None, estimator) -> None:
    """Spec 5.2: the immutable `fitLineage` of the fit `regime` now holds,
    computed over its final content."""
    ql = _lineage_kernel()
    regime.pop(ql.FIT_LINEAGE_KEY, None)
    windows = ql.dependency_windows(kind, a=mode.regime_start, s=s,
                                    t=mode.now)
    regime[ql.FIT_LINEAGE_KEY] = ql.fit_lineage(
        scope=_state_key(mode.account_key), basis=mode.correction_basis,
        windows=windows, estimator=estimator, regime=regime)


def _transition_descriptor(transition) -> dict:
    """Every value the `meter_rate_change` event journals other than the
    lineage itself, exactly as detected (spec 5.7)."""
    return {
        "provider": transition.provider,
        "account_key": transition.account_key,
        "effective_from": transition.effective_from,
        "previous_units_per_point": transition.previous_units_per_point,
        "new_units_per_point": transition.new_units_per_point,
        "severity": transition.severity,
        "detected_at": transition.detected_at,
        "withholding_status": transition.withholding_status,
        "detector_input_causes": transition.detector_input_causes,
        "composition_provenance": transition.composition_provenance,
        "baseline_withheld_days": transition.baseline_withheld_days,
    }


def _stamp_disclosure(transitions, analysis) -> tuple:
    """The detection-time disclosure stamped onto fresh transitions (#688,
    #690, #692), computed once from the analysis."""
    withheld = (analysis.status.value
                if _transition_persistence_admitted(analysis) else None)
    causes, provenance, baseline_days = _analysis_evidence(analysis)
    return tuple(
        dataclasses.replace(
            t, withholding_status=withheld, detector_input_causes=causes,
            composition_provenance=provenance,
            baseline_withheld_days=baseline_days)
        for t in transitions)


def _fresh_transitions(before_clean, after_regimes, analysis, mode,
                       scope) -> tuple:
    """The transitions this reduction creates over CLEAN regimes only."""
    mrc = _cctally()._load_sibling("_lib_meter_rate_change")
    after_clean = clean_regimes(after_regimes, mode.correction_facts, scope)
    return _stamp_disclosure(mrc.detect_transitions(
        before_clean, after_clean, provider="claude",
        account_key=_alert_account_key(mode.account_key),
        detected_at=mode.now.astimezone(UTC).isoformat()), analysis), \
        after_clean


def _attach_transition_lineage(before_clean, regimes, analysis, mode) -> None:
    """Spec 5.7: the successor of every transition this reduction creates
    receives its frozen `transitionLineage` in the same reduction: the union
    of both sides' dependency windows and the detector coverage `[A, T]`,
    the detecting analysis's scope and basis, and the descriptor."""
    ql = _lineage_kernel()
    mrc = _cctally()._load_sibling("_lib_meter_rate_change")
    import _cctally_quota_calibration as qcg
    scope = _state_key(mode.account_key)
    fresh, after_clean = _fresh_transitions(
        before_clean, regimes, analysis, mode, scope)
    if not fresh:
        return
    pairs = mrc._adjacent_pairs(after_clean)
    coverage = ql.window(mode.regime_start, mode.now, until_inclusive=True)
    for transition in fresh:
        predecessor, successor = pairs[mrc._instant(transition.effective_from)]
        if ql.TRANSITION_LINEAGE_KEY in successor:
            continue
        windows = list(ql.regime_lineage(
            predecessor, scope=scope,
            era_start=qcg._era_start(predecessor))["dependencyWindows"])
        windows += list(ql.regime_lineage(
            successor, scope=scope,
            era_start=qcg._era_start(successor))["dependencyWindows"])
        windows.append(coverage)
        successor[ql.TRANSITION_LINEAGE_KEY] = ql.transition_lineage(
            windows=windows, scope=scope, basis=mode.correction_basis,
            descriptor=_transition_descriptor(transition))


def reset_calibration(account_key, *, now=None) -> bool:
    """Remove one account's regimes. True when something was stored.

    Idempotent: resetting an absent calibration succeeds and reports that
    nothing was stored, which is what makes it safe to put in a script.
    """
    with calibration_lock():
        loaded = load_calibrations(now=now)
        accounts = dict(loaded.state.get("accounts") or {})
        key = _state_key(account_key)
        if key not in accounts:
            return False
        accounts.pop(key)
        save_calibrations({
            "schemaVersion": CALIBRATION_STATE_SCHEMA_VERSION,
            "accounts": accounts})
        return True


def _alert_account_key(account_key) -> str:
    """The account identity §6.3's key carries.

    A merged read has no account, and `unattributed` is the estate's sentinel
    for exactly that, so it is used rather than an empty string — the column
    is NOT NULL and every other account-scoped alert family already spells the
    absence this way.
    """
    import _lib_accounts
    if account_key is None:
        return _lib_accounts.UNATTRIBUTED
    return str(account_key)


def persist_and_detect(analysis, mode: PersistMode) -> tuple:
    """`(quarantine path or None, fresh_transitions, persisted_candidates)`
    under one lock.

    The read, the reduction and the write all happen inside one exclusive
    lock, because the reducer is a read-modify-write and the atomic rename
    alone protects readers rather than writers.

    #661 S2 §6.5 step 1: the metering-rate transition is decided HERE, from
    the persisted state before and after this write. The trigger is the
    persistence transition itself rather than a second detector run, so two
    consumers cannot disagree about whether a change happened. The descriptor
    is RETURNED rather than acted on, because step 2 requires this leaf lock
    to be released before any stats lock is taken, and this function is the
    lock's whole extent.

    THREE values, and the third is enumerated whether or not this run wrote
    anything (#689). `fresh_transitions` is what the before-and-after
    comparison produced and keeps its #688 `withholding_status` stamp.
    `persisted_candidates` is every qualified adjacent pair in the
    post-reduction state, with no freshness filter at all. A run that writes
    nothing is exactly the run recovery depends on: the run that lost its
    recording already wrote the regimes, so every later run finds the state
    unchanged and produces no fresh descriptor for it.

    Both sets are computed under this lock from the same post-write state.
    Re-reading the file after the lock is released would let this run attach
    its own timestamp and disclosure to a transition another process wrote.
    """
    with calibration_lock():
        loaded = load_calibrations(now=mode.now)
        if loaded.unreadable:
            # An EMPTY TUPLE, never `None`. `cmd_quota` iterates the second
            # element, so a `None` here raised `TypeError: 'NoneType' object
            # is not iterable` on a file this binary could not read — a
            # permissions problem, an I/O error, or a concurrent quarantine
            # rename removing the primary name between the existence check
            # and the read. No transition is detectable without a before
            # state, and "no transitions" is an empty sequence. THREE
            # elements, for the same reason: `cmd_quota` iterates the third
            # as well.
            return None, (), ()
        scope = _state_key(mode.account_key)
        # #1015 spec 5.6: the before and after states are filtered to their
        # CLEAN regimes identically, so a tainted regime never pairs.
        before = clean_regimes(stored_regimes(loaded.state, mode.account_key),
                               mode.correction_facts, scope)
        reduced = reduce_state(loaded.state, analysis, mode)
        detected_at = mode.now.astimezone(UTC).isoformat()
        transitions: tuple = ()
        if reduced is not loaded.state:
            reduced = stamp_current_writes(
                loaded.state, reduced, mode.account_key)
            save_calibrations(reduced)
            # #688: `detect_transitions` compares two stored states and does
            # not know WHY this run was admitted, so the disclosure is
            # stamped onto what it returns rather than passed into it
            # (`_stamp_disclosure`). #690 / #692: the rest of the disclosure,
            # captured from the SAME analysis and at the same moment.
            # `withholding_status` stays gated on admission, because it names
            # the status a run was withheld under and an ordinary confirmed
            # change was not withheld at all. The other three are stamped
            # whenever the analysis publishes them, on both paths.
            #
            # #1015 spec 5.7: each fresh transition carries the frozen
            # `transitionLineage` the reducer wrote on its successor, which
            # the v2 payload journals as `fit_lineage`.
            fresh, after_clean = _fresh_transitions(
                before, stored_regimes(reduced, mode.account_key), analysis,
                mode, scope)
            transitions = _with_transition_lineage(fresh, after_clean)
        # #689: enumerated UNCONDITIONALLY, and that is the whole point. The
        # run that lost its recording already wrote the regimes, so every
        # later run finds the state unchanged and takes neither branch above.
        # Recovery therefore cannot be derived from freshness; it comes from
        # what is persisted right now.
        candidates = recovery_candidates(
            reduced, mode.account_key, detected_at=detected_at,
            facts=mode.correction_facts)
        return loaded.quarantined, transitions, candidates


def _with_transition_lineage(transitions, regimes) -> tuple:
    """Each transition with its successor's frozen `transitionLineage`."""
    mrc = _cctally()._load_sibling("_lib_meter_rate_change")
    ql = _lineage_kernel()
    pairs = mrc._adjacent_pairs(regimes)
    out = []
    for transition in transitions:
        pair = pairs.get(mrc._instant(transition.effective_from))
        lineage = (pair[1].get(ql.TRANSITION_LINEAGE_KEY)
                   if pair is not None else None)
        out.append(dataclasses.replace(transition, fit_lineage=lineage)
                   if isinstance(lineage, dict) else transition)
    return tuple(out)


#: Keys per presence query. Three bind parameters each, so this stays far
#: below SQLite's variable limit however long the regime list grows.
_PRESENCE_CHUNK = 200


def unrecorded_rate_change_transitions(transitions) -> tuple:
    """Those candidates with neither a durable row nor a terminal latch (#689).

    TWO axes, because the physical row alone is not sufficient. A completed
    tombstone or a higher revision legitimately leaves effective metadata with
    no row, and a row-only lookup would re-select that key on every run. The
    two then cost different amounts. A higher revision reaches a revision
    mismatch in `_classify_live_effective_event` and raises
    `CorrectionRebuildRequired` — which defaults to `recovery_eligible=False`
    and is not overridden at the raise site — so the invocation discards its
    whole ingest cycle. A completed tombstone instead withholds the emission
    and prints one line, because `_prior_meter_rate_change_event` returns None
    for any non-active status before convergence is reached. Both repeat on
    every run, which is what makes them unbounded. No supported `db rederive`
    family can target an `mrc:` event today, so this guard is defensive; it is
    taken because it costs one column and the failure it prevents is
    unbounded.

    A metadata row that is ACTIVE at revision 0 with no physical row is NOT
    terminal: that is the duplicate-with-missing-row case, and the emitter
    materializes the row from the RETAINED event under that same prefix.
    It does so directly when the re-offered payload happens to be
    byte-identical, and through the withheld-conflict convergence path when a
    later command clock makes it divergent, which is the ordinary case.

    #750 S2 states "under that same prefix" explicitly, because #690's
    payload version could otherwise silently change it. A v2 emission for an
    identity the journal already holds at v1 is a second logical event: it
    appends a line, leaves two active revision-0 records describing one row,
    reaches neither the convergence nor its diagnostic, and re-fires a
    notification for history. `record_meter_rate_change` therefore chooses
    the prefix by whether the natural key holds ANY v1 record, and re-emits a
    v1 record as v1. It tests existence rather than liveness because its
    other caller — `cmd_quota`'s fresh detection — is not gated by this
    function, so a v1 state this function files as recorded can still reach
    the emitter and must stay latched there. There is ONE exception,
    and it is terminal for the same reason a tombstone is. Metadata carrying
    no retained record (`event_json IS NULL`) cannot be materialized from:
    the recovery run's payload carries a later clock, so it hashes
    differently and classifies as a conflict, and
    `_effective_event_for_convergence` then fails closed and raises
    `JournalProtocolError`. `_run_stats_ingest_once` re-raises it under
    `mode="authoritative"`, so the WHOLE ingest cycle is discarded with the
    cursor unmoved and every other leg it would have run is deferred; the
    raise reaches `record_rate_change_transition`, which absorbs it into its
    failure line. Nothing is lost permanently, because the journal is
    append-only and the cursor did not advance, but the cost of re-offering
    the key is one discarded cycle plus that line on every run forever, which
    is the unbounded failure this axis exists to prevent.

    Unknown is treated as PRESENT, never absent. Returning no candidates on a
    failure is what stops a store that cannot answer from driving an ingest
    attempt on every run. That catch is defense for a race, for direct use of
    this helper, and for an epoch-current store whose table is missing or
    malformed — it is not what protects an ordinary pre-1011 command, which
    raises `StatsRebuildDeferred` inside `load_population` long before this.
    """
    candidates = tuple(transitions)
    if not candidates:
        return ()
    import _lib_journal
    from _cctally_db import StatsRebuildDeferred
    mrc = _cctally()._load_sibling("_lib_meter_rate_change")
    conn = None
    try:
        conn = _cctally_core.open_db()
        recorded: set = set()
        for i in range(0, len(candidates), _PRESENCE_CHUNK):
            chunk = candidates[i:i + _PRESENCE_CHUNK]
            rows = ",".join("(?,?,?)" for _ in chunk)
            params = [part for t in chunk for part in t.identity()]
            for provider, account, effective in conn.execute(
                    "SELECT provider, account_key, effective_from "
                    "FROM meter_rate_change_events WHERE "
                    f"(provider, account_key, effective_from) IN (VALUES {rows})",
                    params):
                recorded.add((str(provider), str(account), str(effective)))
            # #690: BOTH payload versions, for one natural key. Journal
            # selection and correction are keyed on `event_id` alone, so two
            # prefixes are two independent logical events; asking about only
            # one would let a tombstoned or higher-revision v1 record become
            # invisible to the v2 lookup and be resurrected under the v2 id
            # the first time a v2-capable binary runs. A terminal latch under
            # EITHER prefix therefore suppresses recovery under the other,
            # which is what keeps the #689 terminal-latch contract intact
            # across the version boundary.
            by_id = {}
            for t in chunk:
                identity = t.identity()
                for prefix in (mrc.EVT_ID_PREFIX, mrc.EVT_ID_PREFIX_V2):
                    by_id[_lib_journal.evt_id(prefix, *identity)] = identity
            marks = ",".join("?" for _ in by_id)
            for event_id, rev, status, event_json in conn.execute(
                    "SELECT event_id, rev, status, event_json "
                    "FROM journal_effective_events "
                    f"WHERE event_id IN ({marks})", list(by_id)):
                if (str(status) != "active" or int(rev) != 0
                        or event_json is None):
                    recorded.add(by_id[str(event_id)])
        # #1015 spec 5.7: a superseded identity is terminally recorded.
        # R6: one snapshot, and only the rows each candidate's lineage needs.
        import _cctally_quota_calibration as qcg
        began = not conn.in_transaction
        if began:
            conn.execute("BEGIN")
        try:
            facts = qcg.correction_facts_source(conn)
            if facts is not None:
                for t in candidates:
                    if t.identity() not in recorded and \
                            qcg.rate_change_supersession(
                                conn, t.identity(), lineage=t.fit_lineage,
                                detected_at=t.detected_at,
                                facts=facts) is not None:
                        recorded.add(t.identity())
        finally:
            if began and conn.in_transaction:
                conn.execute("ROLLBACK")
        return tuple(t for t in candidates if t.identity() not in recorded)
    except (Exception, StatsRebuildDeferred):   # noqa: BLE001
        return ()
    finally:
        if conn is not None:
            conn.close()


def rate_change_notifications_enabled() -> bool:
    """Whether a recorded transition also PUSHES (spec §6.2).

    Both switches must be on, exactly as the quota threshold axis requires:
    the global `alerts.enabled` and the family's own
    `alerts.rate_change_enabled`, each default-off. A config read that raises
    answers False rather than propagating — a malformed alerts block must not
    turn a recording path into an error path, because §6.2's whole point is
    that recording happens with no configuration at all.
    """
    c = _cctally()
    try:
        block = _cctally_core._get_alerts_config(c.load_config())
    except Exception:                                  # noqa: BLE001
        return False
    return bool(block.get("enabled")) and bool(block.get("rate_change_enabled"))


def record_rate_change_transition(transition, *, now,
                                  notification_owed: bool = False):
    """Steps 3 to 6 of spec §6.5. `(result, deferred)` for this identity, or
    `None` when the cycle did not run or the store could not be written.

    `run_stats_ingest` is the sole stats writer and it enforces journal-first,
    then commit — so the descriptor is passed THROUGH it rather than written
    here. `mode="authoritative"` because the caller must observe its own
    transition rather than leaving it to whichever process next holds the
    ingest lock: a user who just ran `cctally quota` and saw the change
    reported would otherwise find no event recorded.

    It no longer DISPATCHES (#695). The payload comes back on the result and
    `_settle_rate_change` fires it only after winning the delivery ledger's
    compare-and-set, which is what makes a lost dispatch recoverable instead
    of terminal. Returning `bool(result.ran)` was never enough either: `ran`
    reports that the cycle ran, not that anything happened to this identity.

    `notification_owed` is the caller's assertion that its ledger holds no
    decision for this identity, and it is the ONLY thing that lets an
    already-recorded row notify.

    A stats index mid-epoch-rebuild, a busy lock or a corrupt store must not
    turn `cctally quota` into an error: the calibration is already persisted,
    and the command reports what it found.

    A failed recording IS recovered, by a later run (#689). It is not
    recovered by freshness: once this write has happened the pair is present
    on both sides of every later comparison, so `detect_transitions` never
    returns it again. It is recovered because `cmd_quota` also enumerates the
    persisted adjacent pairs, resolves which already have a durable row or a
    terminal latch, and re-offers only the rest. `record_meter_rate_change`
    classifies before it appends, so re-offering a recorded identity under a
    later command clock cannot append a second, byte-different line.

    The catch is `Exception` plus the two deferral signals, and NOT
    `BaseException` (#661 S2 Stage C review). `StatsRebuildDeferred` derives
    directly from `BaseException` — deliberately, so that no ordinary
    `except Exception` absorbs it — which made `except BaseException` the
    obvious way to cover it and swallowed `KeyboardInterrupt` and
    `SystemExit` with it. A Ctrl-C during the ingest then printed "could not
    record the metering-rate change" and the command carried on. Those are
    different kinds of event: one says this store is busy, the other says
    this process is ending, and only the first is this function's to absorb.
    Catching the shared parent rather than the epoch subclass keeps the
    "widened ONCE" property `StatsRebuildDeferred`'s own docstring states.
    """
    import _cctally_journal as jr
    from _cctally_db import StatsRebuildDeferred
    try:
        result = jr.run_stats_ingest(
            mode="authoritative",
            meter_rate_change={
                "transition": transition,
                "notify": rate_change_notifications_enabled(),
                "created_at": now.astimezone(UTC).isoformat(),
                "notification_owed": bool(notification_owed),
            },
        )
    except (Exception, StatsRebuildDeferred) as exc:   # noqa: BLE001
        eprint(f"quota: could not record the metering-rate change: {exc}")
        return None
    if not result.ran:
        return None
    return result.meter_rate_change_result, list(result.deferred_alerts)


def _settle_rate_change(transition, outcome, status) -> "str | None":
    """Mark the ledger, then dispatch — never the other way round (#695 §6).

    `alerted_at` is written before the notifier `Popen` for every other axis,
    and this family follows that contract with the ledger standing in for the
    column. The consequence is stated rather than hidden: a crash after the
    mark is terminal, and a crash before it leaves the identity owed for the
    next run.

    An UNUSABLE ledger cannot elect a dispatcher, so the payload is dispatched
    unmarked. Losing a notification is worse than the duplicate that a later
    repaired ledger could produce, and §3.1 records both consequences that
    degradation actually carries.

    Returns the compare-and-set outcome when one was attempted, and None when
    there was nothing to claim. The sweep stops on `unusable` (§3.2): a ledger
    that cannot elect a dispatcher for this identity cannot elect one for the
    next either, so continuing would drive a full authoritative ingest per
    remaining identity and discard every result at the claim.
    """
    import _cctally_journal as jr
    d = _rate_change_delivery()
    if outcome is None:
        return
    result, deferred = outcome
    if result is None or not result.notification_decided:
        return
    if not status.usable:
        # An unusable ledger records nothing, so a deferred handoff leaves
        # the identity owed for the sweep once the ledger is usable again.
        if deferred and _superseded_at_handoff(transition) is False:
            (jr.ALERT_DISPATCHER or jr._dispatch_pending_alerts)(deferred)
        return
    claim = claim_delivery(transition.identity())
    if claim != d.CLAIM_WON:
        return claim
    # #1015 spec 5.7: the ledger is claimed and its leaf lock released
    # (`claim_delivery` returns from its `with` block), and only then is
    # supersession rechecked under the stats maintenance read guard. A
    # superseded identity is settled — the decision stays recorded and is
    # never cleared to enable a refire — with no notification. A recheck
    # that cannot take the read guard (a maintenance owner holds it) defers
    # the handoff: nothing is dispatched, this run's own claim is handed
    # back, and the identity stays owed for a later run's sweep.
    if deferred:
        superseded = _superseded_at_handoff(transition)
        if superseded is None:
            release_delivery_claim(transition.identity())
            return HANDOFF_DEFERRED
        if not superseded:
            (jr.ALERT_DISPATCHER or jr._dispatch_pending_alerts)(deferred)
    return claim


#: `_settle_rate_change`'s outcome when the won claim was handed back
#: because the supersession recheck could not take its read guard.
HANDOFF_DEFERRED = "handoff_deferred"


def _superseded_at_handoff(transition) -> "bool | None":
    """Spec 5.7's last check before a notification is handed off.

    The fail-fast reader takes the stats maintenance flock SHARED and
    non-blocking, which is the read guard: a destructive maintenance owner
    is not raced. When that guard (or a published index) is unavailable the
    answer is None, and the caller defers the handoff rather than
    dispatching unchecked. Correction facts that cannot be read on an open
    index answer False — the emitter already refused a superseded identity
    inside the recording cycle, so this only narrows the window between
    that commit and the handoff."""
    import _cctally_quota_calibration as qcg
    import _cctally_store
    conn = _cctally_store.stats_open_readonly_failfast()
    if conn is None:
        return None
    try:
        return qcg.rate_change_supersession(
            conn, transition.identity(), lineage=transition.fit_lineage,
            detected_at=transition.detected_at) is not None
    finally:
        conn.close()


def _owed_rate_change_transitions(account_keys, status, *, now,
                                  settled=()) -> tuple:
    """Durable rows with no delivery decision, as descriptors (#695 §5.1).

    Only the IDENTITY comes from here. Every other field of the alert is
    reconstructed inside the emitter from the same durable row, because the
    row is what was actually recorded; this descriptor exists to carry the
    identity and the command clock through `run_stats_ingest`. The zero rates
    and the `info` severity below are therefore never published — the
    emitter's owed branch returns before `event_payload` on an existing row.

    A stats read that fails yields NOTHING rather than raising: the command
    reports what it found, and a store it cannot read is not an error path
    here any more than it is in `unrecorded_rate_change_transitions`.

    `settled` carries the identities this invocation already offered to
    `_settle_rate_change`. They are excluded because the ledger snapshot is
    taken once, before the account loop, so a mark the loop just wrote is not
    in `status.state` and the identity would otherwise be re-offered here —
    one wasted authoritative ingest, and a spurious `could not record` line if
    that ingest happens to fail. A `write_failed` identity is excluded too:
    §3.2 gives it to a LATER run, not to this one.
    """
    from _cctally_db import StatsRebuildDeferred
    mrc = _cctally()._load_sibling("_lib_meter_rate_change")
    d = _rate_change_delivery()
    try:
        recorded = recorded_rate_change_identities(account_keys)
    except (Exception, StatsRebuildDeferred):          # noqa: BLE001
        return ()
    # #1015 spec 5.7: a superseded identity is never owed a notification.
    superseded = _superseded_identities(account_keys)
    recorded = tuple(i for i in recorded if tuple(i) not in superseded)
    detected_at = now.astimezone(UTC).isoformat()
    return tuple(
        mrc.RateChangeTransition(
            provider=provider, account_key=account, effective_from=effective,
            previous_units_per_point=0.0, new_units_per_point=0.0,
            severity=mrc.SEVERITY_INFO, detected_at=detected_at)
        for provider, account, effective in d.owed(
            recorded,
            d.decided_set(status.state) | {tuple(i) for i in settled}))


def _superseded_identities(account_keys) -> frozenset:
    """Every effectively superseded identity of `account_keys`, read on a
    guarded connection, or the empty set when it cannot be read."""
    import _cctally_quota_calibration as qcg
    from _cctally_db import StatsRebuildDeferred
    conn = None
    try:
        conn = _cctally_core.open_db()
        state = qcg.read_rate_change_supersessions(
            conn, account_keys=account_keys)
        return frozenset(state.by_identity)
    except (Exception, StatsRebuildDeferred):          # noqa: BLE001
        return frozenset()
    finally:
        if conn is not None:
            conn.close()


def reconcile_rate_change_supersessions(account_keys, *, now) -> int:
    """Journal the explicit supersession of every effectively superseded
    transition identity of `account_keys` that has none (#1015 spec 5.7).

    It runs after `persist_and_detect` released the calibration lock and
    before any transition is recorded, on every automatic invocation,
    withheld analyses included, and it reads the durable rows rather than
    the calibration file, so an identity whose account was reset is
    reconciled too. Through the sole stats writer, as
    `record_rate_change_transition` is; a crash between the sidecar write and
    the journal append is repaired by the next invocation. Returns how many
    supersessions were offered. A store that cannot be read or written is
    not an error here: the command reports what it found.
    """
    import _cctally_journal as jr
    import _cctally_quota_calibration as qcg
    import _lib_journal
    from _cctally_db import StatsRebuildDeferred
    mrc = _cctally()._load_sibling("_lib_meter_rate_change")
    conn = None
    try:
        conn = _cctally_core.open_db()
        state = qcg.read_rate_change_supersessions(
            conn, account_keys=account_keys)
        pending = []
        for identity, supersession in sorted(state.by_identity.items()):
            if supersession.explicit:
                continue
            row = conn.execute(
                "SELECT detected_at_utc, fit_lineage FROM "
                "meter_rate_change_events WHERE provider = ? AND "
                "account_key = ? AND effective_from = ?", identity).fetchone()
            if row is None:
                continue
            lineage = qcg.transition_lineage_for(identity[1], row[0], row[1])
            if lineage.get("legacy"):
                lineage = {"legacy": True,
                           "window": lineage["dependencyWindows"][0]}
            target = None
            for prefix in (mrc.EVT_ID_PREFIX_V2, mrc.EVT_ID_PREFIX):
                eid = _lib_journal.evt_id(prefix, *identity)
                if conn.execute(
                        "SELECT 1 FROM journal_effective_events "
                        "WHERE event_id = ?", (eid,)).fetchone() is not None:
                    target = eid
                    break
            pending.append({
                "provider": identity[0], "account_key": identity[1],
                "effective_from": identity[2], "target_event_id": target,
                "fit_lineage": lineage,
                "corrections": [{"opId": op, "observations": n}
                                for op, n in supersession.observations],
                "superseded_at_utc": now.astimezone(UTC).isoformat()})
    except (Exception, StatsRebuildDeferred) as exc:   # noqa: BLE001
        eprint(f"quota: could not read metering-rate supersessions: {exc}")
        return 0
    finally:
        if conn is not None:
            conn.close()
    if not pending:
        return 0
    try:
        jr.run_stats_ingest(
            mode="authoritative",
            meter_rate_change={
                "supersessions": pending,
                "created_at": now.astimezone(UTC).isoformat()})
    except (Exception, StatsRebuildDeferred) as exc:   # noqa: BLE001
        eprint("quota: could not record the metering-rate supersession: "
               f"{exc}")
        return 0
    return len(pending)


def persist(analysis, mode: PersistMode) -> "str | None":
    """`persist_and_detect`'s quarantine half, for callers with no stats leg.

    It unpacks all THREE values and deliberately discards both descriptor
    collections (#689). This surface takes no stats leg at all — no presence
    lookup, no ingest and no notification — so a caller reaching it gets the
    calibration write and the quarantine path and nothing else.
    """
    quarantined, _fresh, _candidates = persist_and_detect(analysis, mode)
    return quarantined


# ---------------------------------------------------------------------------
# The analysis (spec sections 21, 22, 23, 35).
# ---------------------------------------------------------------------------
#: Which outcome an invocation reports when it analysed several accounts.
#: A FINDING wins over degradation, which is `pricing-check`'s precedent and
#: the reason spec section 8 gives exit 1 to a confirmed change at all: a
#: degraded leg never masks a finding. Below that, an unhealthy account
#: outranks a merely thin one, because it names something to fix.
EXIT_SEVERITY: dict = {0: 0, 4: 1, 3: 2, 1: 3, 2: 4}


@dataclasses.dataclass(frozen=True)
class CurrentWeek:
    """The window the published consumption describes.

    `units_start` is where the unit total begins and is NOT always the week
    anchor: a credited week's continuing series uses post-credit captures, so
    counting units from the anchor would divide a whole week's tokens by a
    meter that was reset partway through. It matches `_floored_week_max`'s
    choice, made consistently here.
    """

    anchor: "dt.datetime"
    end: "dt.datetime"
    units_start: "dt.datetime"
    observed_percent: "int | None"


def current_week_window(segments, *, now) -> "CurrentWeek | None":
    """The subscription week the clock is in, or None when none is retained.

    None rather than a guess: the kernel then withholds the projection with
    the qualification `week-window-unknown`, which says the caller supplied no
    window rather than telling the user their history is missing.
    """
    if not segments:
        return None
    last = max(segments, key=lambda s: s.rows[-1].at)
    anchor = last.week_anchor
    end = anchor + dt.timedelta(days=7)
    if now >= end:
        return None
    in_week = [s for s in segments if s.week_anchor == anchor]
    # One segment on this anchor means the week ran uninterrupted, so the
    # units start where the week does. Several means a floor credit opened a
    # new slice under the same week identity, and the live slice is the last.
    # A reset credit re-anchors, so its successor is the only segment on its
    # own anchor and the anchor IS the credit instant.
    units_start = anchor if len(in_week) == 1 else in_week[-1].rows[0].at
    rows = sorted(last.rows, key=qm._snapshot_sort_key)
    observed = qm.integer_percent(rows[-1].percent) if rows else None
    return CurrentWeek(anchor, end, units_start, observed)


@dataclasses.dataclass(frozen=True)
class AccountResult:
    """Everything one account's renderers need, already computed."""

    account_key: "str | None"
    label: "str | None"
    analysis: object
    load: LoadResult
    week: "CurrentWeek | None"
    recorded: "dict | None"
    stale_prior: bool
    mode_kind: str
    #: The analysis run with `fingerprint_matches=True`. The reducer reads
    #: THIS one, so a stale prior cannot stop a fresh trustworthy fit from
    #: being accepted; `analysis` is what the user is shown.
    clean: object = None
    quarantined: "str | None" = None
    #: The coefficient era the clock selected, or None before every
    #: validated era (#978). The method metadata names it.
    era: "qm.CoefficientEra | None" = None
    #: #1015 spec 5.6: the recorded prior's lineage assessment,
    #: `{"state": "clean"|"recalibration-needed"|"unavailable",
    #: "corrections": [...]}`, or None when nothing is recorded.
    recorded_lineage: "dict | None" = None


def recorded_prior_lineage(recorded, facts, account_key) -> "dict | None":
    """The recorded prior presented with its assessment (spec 5.6)."""
    if recorded is None:
        return None
    annotation = recorded.get(_ANNOTATION_KEY)
    if isinstance(annotation, dict):
        corrections = annotation.get("corrections")
        return {"state": qm.RECALIBRATION_NEEDED,
                "corrections": list(corrections)
                if isinstance(corrections, list) else []}
    if facts is None:
        return {"state": "unavailable", "corrections": []}
    import _cctally_quota_calibration as qcg
    assessment = qcg.assess_regime(recorded, facts,
                                   scope=_state_key(account_key))
    if assessment.state == "tainted":
        return {"state": qm.RECALIBRATION_NEEDED,
                "corrections": _correction_json(assessment.corrections)}
    return {"state": assessment.state, "corrections": []}


def recorded_prior_is_stale(recorded, era) -> bool:
    """Is the recorded calibration from other constants or another era?

    Spec 4.3's status table: either one is the `stale` overlay, as a
    fingerprint mismatch already was. A run with no selected era compares
    against no era id, so any recorded regime is stale to it.
    """
    if recorded is None:
        return False
    if recorded.get("fingerprint") != qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT:
        return True
    return recorded.get("coefficientEra") != (
        None if era is None else era.id)


def analyse_account(account_key, *, now, since, watch_from,
                    stored_state) -> AccountResult:
    """Read one account's population and run the kernel over it."""
    # #978 spec 4.3: ONE era per analysis, chosen once from the clock. The
    # population is confined to it, so every kernel call below sees only
    # in-era instants and weighs them with this era's coefficients.
    era = select_era(now)
    load = load_population(account_key, since=since, now=now, era=era)
    extra = [load.status] if load.status is not None else []
    # A clock before every validated era read nothing, so the population is
    # empty and no weight is ever applied. The kernel still requires an era
    # to type its arguments; the earliest one is an inert placeholder here.
    kernel_era = era if era is not None else qm.COEFFICIENT_ERAS[0]
    segments = qm.build_segments(load.snapshots, load.credits)
    series = qm.build_daily_series(
        segments, load.entries, now=now, newest_entry_at=load.newest_entry_at,
        era=kernel_era)
    week = current_week_window(segments, now=now)
    week_withheld = None
    if week is None:
        forecast_population: tuple = ()
        week_units = None
        observed = None
        week_start = week_end = None
    else:
        horizon = min(now, week.end)
        forecast_population = tuple(
            e for e in load.entries if week.units_start <= e.at < horizon
            and qm.era_contains(kernel_era, e.at))
        observed = week.observed_percent
        week_start, week_end = week.units_start, week.end
        if week_spans_era(week, kernel_era):
            # The week's units would come from two eras, and the in-era part
            # is not the whole week, so consumption, the residual, headroom
            # and the projection are withheld. The observed reading and an
            # independently valid fit are still published.
            week_withheld = "unvalidated-coefficient-era"
            week_units = None
        else:
            week_units = current_week_units(forecast_population, kernel_era)

    recorded = recorded_regime(stored_state, account_key)
    stale_prior = recorded_prior_is_stale(recorded, era)
    mode_kind = "automatic"
    if watch_from is not None:
        mode_kind = "diagnostic"
    elif since is not None and recorded is not None:
        recorded_from = parse_instant(recorded.get("effectiveFrom"))
        if recorded_from is not None and since > recorded_from:
            mode_kind = "diagnostic"

    common = dict(
        now=now, newest_at=load.newest_entry_at, era=kernel_era,
        current_week_withheld=week_withheld,
        forecast_population=forecast_population,
        observed_percent=observed, current_week_units=week_units,
        current_week_start=week_start, current_week_end=week_end,
        override_split=watch_from.date() if watch_from is not None else None,
        extra_statuses=tuple(extra),
        unattributed=qm.unattributed_units(segments, load.entries, now=now,
                                           era=kernel_era),
        in_progress_excluded=qm.in_progress_dates(segments, now=now),
        diagnostics={"snapshots": len(load.snapshots),
                     "segments": len(segments),
                     "credits": len(load.credits)},
    )
    # `fingerprint_matches` describes the calibration the command would ACT
    # on, which is the fresh fit whenever one is trustworthy. Passing False
    # unconditionally under a stale prior would deadlock: `stale` outranks
    # everything below it, so the status could never reach `ok` and no new fit
    # could ever be accepted under the current constants. The fresh analysis
    # therefore runs first, and a stale prior only decides the REPORT when
    # that fresh analysis has no trustworthy fit of its own to publish.
    clean = qm.analyse(series, fingerprint_matches=True, **common)
    reported = clean
    if stale_prior and clean.status is not CalibrationStatus.OK:
        reported = qm.overlay_status(clean, CalibrationStatus.STALE)
    return AccountResult(
        account_key=account_key,
        label=account_display_label(account_key),
        analysis=reported, load=load, week=week, recorded=recorded,
        stale_prior=stale_prior, mode_kind=mode_kind, clean=clean, era=era,
        recorded_lineage=recorded_prior_lineage(
            recorded, load.correction_facts, account_key))


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------
def _iso(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        import _lib_json_envelope
        return _lib_json_envelope._iso_z(value)
    return value.isoformat()


def _evidence_json(evidence) -> dict:
    """One `CalibrationEvidence` on the wire.

    A withheld value carries a typed code and NO value, no interval and no
    support — it is absent, never zero — and no confidence field exists at
    all, because the design never defined what a confidence would mean here.
    """
    return {
        "state": evidence.state,
        "value": evidence.value,
        "interval": (None if evidence.interval is None
                     else {"lo": evidence.interval.lo,
                           "hi": evidence.interval.hi}),
        "support": (None if evidence.support is None
                    else {"days": evidence.support.days,
                          "segments": evidence.support.segments}),
        "population": dict(evidence.population),
        "code": evidence.code,
        "qualifications": list(evidence.qualifications),
    }


def _recorded_json(recorded, stale_prior, lineage=None) -> "dict | None":
    if recorded is None:
        return None
    out = dict(recorded)
    for key in ("effectiveFrom", "effectiveUntil", "asOf"):
        if out.get(key) is not None:
            out[key] = _iso(parse_instant(out[key], key))
    out["fingerprintMatchesCurrent"] = not stale_prior
    if lineage is not None:
        # #1015 spec 5.6: additive; the analysis status and exit code are
        # unchanged.
        out["lineage"] = lineage
    return out


def _sorted_shares(shares) -> "dict | None":
    if shares is None:
        return None
    return {k: shares[k] for k in sorted(shares)}


def _family_materiality_json(analysis, era) -> dict:
    """`composition.familyMateriality` (#991 spec 4.8): the reference the
    evidence-bounded family test measures against, and the evidence."""
    return {
        "reference": "fit-weighted",
        "fitFamilyShares": _sorted_shares(analysis.fit_family_shares),
        "referenceFamily": None if era is None else era.reference_family,
        "familyRateBounds": _rate_bounds_json(era),
        "maxRelativeEffect": qm.MIX_SUPPORT["family_max_relative_effect"],
    }


def _forecast_probe_json(analysis) -> dict:
    """`composition.forecastProbe` (#991 spec 4.8).

    The vectors are the exact forecast aggregate, published for the
    `no-reference` and `probed` states. Every measurement is the evaluator's
    and is null unless the state is `probed`; `supported` is the analysis'
    `forecast_supported` in every state.
    """
    support = (analysis.forecast_support
               if analysis.forecast_probe_state == "probed" else None)
    effect = None if support is None else support.family_effect
    unavailable = None
    if effect is not None and effect.unavailable is not None:
        unavailable = {"reason": effect.unavailable.value,
                       "families": sorted(effect.families)}
    return {
        "state": analysis.forecast_probe_state,
        "familyShares": _sorted_shares(analysis.forecast_family_shares),
        "classShares": _sorted_shares(analysis.forecast_class_shares),
        "familyDistance": None if support is None else support.family_distance,
        "familyDistanceReference": "baseline-family-shares",
        "familyWithinRadius": (None if support is None
                               else support.family_within_radius),
        "familyEffectBound": None if effect is None else effect.bound,
        "familyEffectReference": "fit-family-shares",
        "familyEffectUnavailable": unavailable,
        "familyAdmittedBy": (None if support is None
                             else support.family_admitted_by),
        "classDistance": None if support is None else support.class_distance,
        "classWithinRadius": (None if support is None
                              else support.class_within_radius),
        "supported": analysis.forecast_supported,
    }


def account_payload(result: AccountResult) -> dict:
    """One account's complete payload, camelCase throughout."""
    analysis = result.analysis
    load = result.load
    diagnostics = dict(analysis.diagnostics)
    # The detector block is derived from `analysis.detector` rather than read
    # out of `diagnostics`, which is the same source the text renderer uses.
    # Reading the dict key made the wire's disclosure depend on a diagnostic
    # any caller could omit, and section 24 requires it published.
    diagnostics.pop("detector", None)
    detector = qm.detector_diagnostics(analysis.detector)
    era = result.era
    supported_from = None if era is None else era.start.isoformat()
    supported_until = (None if era is None or era.end is None
                       else era.end.isoformat())
    return {
        "status": analysis.status.value,
        "verdict": analysis.verdict.value,
        "exitCode": analysis.exit_code,
        "scope": {
            "accountKey": result.account_key,
            "accountLabel": result.label,
            "merged": result.account_key is None,
            "analysisStart": _iso(load.analysis_start),
            "invocation": result.mode_kind,
        },
        "calibration": {
            "recorded": _recorded_json(result.recorded, result.stale_prior,
                                       result.recorded_lineage),
            "fitted": _evidence_json(analysis.fitted),
        },
        "analysis": {
            "baseline": {"fit": _evidence_json(analysis.baseline_fit)},
            "watch": {"fit": _evidence_json(analysis.watch_fit)},
        },
        "currentWeek": {
            "start": _iso(result.week.units_start) if result.week else None,
            "end": _iso(result.week.end) if result.week else None,
            "observedPercent": analysis.observed_percent,
            "observedMinusModelled": diagnostics.get("observedMinusModelled"),
            "consumption": _evidence_json(analysis.consumption),
            "projection": _evidence_json(analysis.projection),
            "headroom": _evidence_json(analysis.headroom),
        },
        "composition": {
            "baseline": {"familyShares": dict(analysis.family_shares),
                         "classShares": dict(analysis.class_shares)},
            "current": {"familyShares": dict(analysis.current_family_shares),
                        "classShares": dict(analysis.current_class_shares)},
            "familyRadiusEffective": analysis.family_radius,
            "classRadiusEffective": analysis.class_radius,
            "familyRadiusEmpirical": diagnostics.get("familyRadiusEmpirical"),
            "classRadiusEmpirical": diagnostics.get("classRadiusEmpirical"),
            "radiusRule": "max(observed spread, materiality floor)",
            "familyMateriality": _family_materiality_json(analysis, era),
            "forecastProbe": _forecast_probe_json(analysis),
        },
        "health": {
            "entriesThrough": _iso(load.newest_entry_at),
            "snapshots": diagnostics.get("snapshots"),
            "segments": diagnostics.get("segments"),
            "credits": diagnostics.get("credits"),
            "eligibleDays": diagnostics.get("eligibleDays"),
            "withheldDays": diagnostics.get("withheldDays"),
            "eligibilityFence": diagnostics.get("eligibilityFence"),
            "unattributedUnits": diagnostics.get("unattributedUnits"),
            "unattributedEntries": diagnostics.get("unattributedEntries"),
            "inProgressDayExcluded": diagnostics.get("inProgressDayExcluded"),
            "forecastPopulationEntries": diagnostics.get(
                "forecastPopulationEntries"),
            "rejectedNumericInputs": diagnostics.get("rejectedNumericInputs"),
            "unrecognisedSnapshotSources": load.diagnostics.get(
                "unrecognisedSnapshotSources"),
            "legacyDateOnlyWeekAnchors": load.diagnostics.get(
                "legacyDateOnlyWeekAnchors"),
            "fastModeEntriesExcluded": load.diagnostics.get(
                "fastModeEntriesExcluded"),
            "storeCause": load.cause,
            "calibrationFileQuarantinedTo": result.quarantined,
        },
        "method": {
            "algorithmRevision": qm.QUOTA_MODEL_ALGORITHM_REVISION,
            "constantsFingerprint": qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
            "verifiedAt": qm.QUOTA_MODEL_VERIFIED_AT,
            "supportedCompositionFrom": supported_from,
            "coefficientSupport": {
                "supportedCompositionFrom": supported_from,
                "supportedCompositionUntil": supported_until,
                "coefficientEra": None if era is None else era.id,
                "fastModeParticipation": qm.FAST_MODE_PARTICIPATION,
            },
            "coefficientEras": published_coefficient_eras(),
            "detector": detector,
            "dedicatedPoolScope": qm.DEDICATED_POOL_SCOPE_NOTE,
        },
        "blocking": [reason.value for reason in analysis.blocking],
    }


def build_payload(results, *, now, decorated) -> dict:
    """The whole invocation's JSON, stamped through the shared envelope.

    With one analysed population the account payload is flattened to the top
    level, which keeps spec section 8's stable key list at the top level and
    keeps a single-account install's output shaped as that section describes.
    Above one real account the accounts are published side by side, because
    section 17 says they are reported separately rather than merged.
    """
    worst = max(results, key=lambda r: EXIT_SEVERITY[r.analysis.exit_code])
    body = {
        "generatedAt": _iso(now),
        "status": worst.analysis.status.value,
        "verdict": worst.analysis.verdict.value,
        "exitCode": worst.analysis.exit_code,
    }
    if decorated and len(results) > 1:
        body["accounts"] = [account_payload(r) for r in results]
    else:
        body.update(account_payload(results[0]))
        body["status"] = results[0].analysis.status.value
        body["verdict"] = results[0].analysis.verdict.value
        body["exitCode"] = results[0].analysis.exit_code
    import _lib_json_envelope
    return _lib_json_envelope.stamp_schema_version(body)


def _fmt_units(value) -> str:
    return "-" if value is None else f"{value:,.0f}"


def _fmt_pct(value) -> str:
    return "-" if value is None else f"{value:.2f}%"


def _evidence_line(label, evidence, formatter) -> str:
    if evidence.state != "available":
        marks = (f"  ({', '.join(evidence.qualifications)})"
                 if evidence.qualifications else "")
        return f"  {label:26} withheld: {evidence.code}{marks}"
    interval = evidence.interval
    span = ""
    if interval is not None:
        span = (f"   [{formatter(interval.lo)} , "
                f"{formatter(interval.hi) if interval.hi is not None else '-'}]")
    marks = (f"  ({', '.join(evidence.qualifications)})"
             if evidence.qualifications else "")
    return f"  {label:26} {formatter(evidence.value)}{span}{marks}"


def _supported_composition_text(era) -> str:
    """The selected era's interval, as the DATA line names it (#978)."""
    if era is None:
        return f"none validated before {SUPPORTED_COMPOSITION_FROM.isoformat()}"
    if era.end is None:
        return f"{era.start.isoformat()} onward"
    return f"{era.start.isoformat()} to {era.end.isoformat()}"


def render_text(results, *, now, decorated) -> list:
    """The human report, as a list of lines."""
    out: list = []
    for result in results:
        analysis = result.analysis
        payload_detector = qm.detector_diagnostics(analysis.detector)
        if decorated and len(results) > 1:
            out.append(f"=== {result.label or 'merged'} ===")
        out.append("cctally quota — weekly quota consumption and metering rate")
        out.append("")
        out.append("DATA")
        out.append(f"  entries through            "
                   f"{_iso(result.load.newest_entry_at) or '-'}")
        out.append(f"  analysis start             "
                   f"{_iso(result.load.analysis_start) or '-'}"
                   f"   (supported composition: "
                   f"{_supported_composition_text(result.era)})")
        out.append(f"  eligible days              "
                   f"{analysis.diagnostics.get('eligibleDays', 0)}"
                   f"   ({analysis.diagnostics.get('withheldDays', 0)} withheld)")
        if result.load.cause:
            out.append(f"  ! store                    {result.load.cause}")
        if result.quarantined:
            out.append(f"  ! stored calibration       preserved at "
                       f"{result.quarantined}")
        out.append("")
        out.append("CALIBRATION   effective blended weighted units per weekly point")
        out.append(_evidence_line("fitted", analysis.fitted, _fmt_units))
        if result.recorded is not None:
            recorded_value = result.recorded.get("unitsPerPoint")
            stale = "  (stale — fitted under different constants)" \
                if result.stale_prior else ""
            if (result.recorded_lineage or {}).get("state") \
                    == qm.RECALIBRATION_NEEDED:
                # #1015 spec 5.6: U3's sentence on a tainted prior.
                import _lib_quota_copy
                stale += ("  — " + _lib_quota_copy._LONG_FORM[
                    qm.RECALIBRATION_NEEDED])
            out.append(f"  {'recorded prior':26} "
                       f"{_fmt_units(recorded_value)}{stale}")
        out.append("  The fitted value is not a provider budget. It is one "
                   "effective blended rate,")
        out.append("  valid for the observed model and token blend, and it is "
                   "the provider's budget")
        out.append("  minus an unmeasured, workload-dependent amount.")
        out.append("")
        out.append("CURRENT WEEK")
        if result.week is None:
            out.append("  no current subscription week is retained")
        else:
            out.append(f"  window                     "
                       f"{_iso(result.week.units_start)} .. "
                       f"{_iso(result.week.end)}")
            out.append(f"  observed meter             "
                       f"{analysis.observed_percent if analysis.observed_percent is not None else '-'}%")
        out.append(_evidence_line("modelled consumption", analysis.consumption,
                                  _fmt_pct))
        out.append(_evidence_line("headroom to 100%", analysis.headroom,
                                  _fmt_pct))
        out.append(_evidence_line("projection to week end",
                                  analysis.projection, _fmt_pct))
        difference = analysis.diagnostics.get("observedMinusModelled")
        if difference is not None:
            out.append(f"  {'observed minus modelled':26} "
                       f"{difference:+.2f} points")
        out.append("")
        out.append("COMPOSITION SUPPORT   radii are EFFECTIVE: "
                   "max(observed spread, materiality floor)")
        out.append(f"  family radius              "
                   f"{_fmt_radius(analysis.family_radius)}"
                   f"   (observed "
                   f"{_fmt_radius(analysis.diagnostics.get('familyRadiusEmpirical'))})")
        out.append(f"  token-class radius         "
                   f"{_fmt_radius(analysis.class_radius)}"
                   f"   (observed "
                   f"{_fmt_radius(analysis.diagnostics.get('classRadiusEmpirical'))})")
        out.extend(_forecast_family_lines(analysis))
        out.append(f"  {qm.DEDICATED_POOL_SCOPE_NOTE}")
        out.append("")
        out.append("DETECTOR")
        out.append(f"  split                      "
                   f"{payload_detector['splitDate'] or 'none'}")
        out.append(f"  eligible days scanned      "
                   f"{payload_detector['scannedEligibleDays']}"
                   f"   (Holm family {payload_detector['holmFamilySize']})")
        if payload_detector["historyTruncated"]:
            out.append(
                f"  only the most recent {payload_detector['maxAutoScanDays']}"
                f" of {payload_detector['inputEligibleDays']} eligible days "
                f"were scanned;")
            out.append(
                f"  the scan starts at "
                f"{payload_detector['scanStartDate']} and the rank-sum "
                f"population is that retained set.")
        if payload_detector["rawP"] is not None:
            out.append(f"  p (raw / Holm)             "
                       f"{payload_detector['rawP']:.6f} / "
                       f"{payload_detector['holmP']:.6f}")
        out.append(_evidence_line("baseline fit", analysis.baseline_fit,
                                  _fmt_units))
        out.append(_evidence_line("successor fit", analysis.watch_fit,
                                  _fmt_units))
        out.append("")
        out.append(f"VERDICT   {analysis.verdict.value}"
                   f"   (status {analysis.status.value}, "
                   f"exit {analysis.exit_code})")
        for reason in analysis.blocking:
            out.append(f"  withheld because: {reason.value}")
        if analysis.verdict is Verdict.WITHHELD:
            # Spec section 9: a withheld verdict must name a rate change as
            # NOT RULED OUT rather than denied. "No rate change" is a finding;
            # this is the absence of one, and conflating them tells a user
            # their metering is unchanged when nothing was established.
            out.append("  This is not a finding of no rate change. The "
                       "evidence needed to decide is")
            out.append("  missing, so no verdict is reported either way.")
        if result.mode_kind == "diagnostic":
            out.append("  diagnostic run — nothing was persisted")
        out.append("")
    return out


def _fmt_radius(value) -> str:
    return "-" if value is None else f"{value:.4f}"


#: The width the COMPOSITION SUPPORT lines were approved at (#991 spec 4.9):
#: the `not bounded:` line wraps onto a continuation line indented to the
#: value column when it would run past it.
_COMPOSITION_TEXT_WIDTH = 80
#: The value column of every `  <label:26> value` line.
_VALUE_COLUMN = 2 + 26 + 1

#: The distance line for each probe state the evaluator did not reach.
_UNPROBED_DISTANCE = {
    "not-probed": "-   (no forecast population)",
    "unaggregatable": "-   (forecast population not aggregatable)",
    "no-reference": "-   (no reference composition)",
}


def _forecast_family_lines(analysis) -> list:
    """The two forecast-family lines of COMPOSITION SUPPORT (#991 spec 4.9)."""
    support = (analysis.forecast_support
               if analysis.forecast_probe_state == "probed" else None)
    if support is None:
        return [f"  {'forecast family distance':26} "
                f"{_UNPROBED_DISTANCE.get(analysis.forecast_probe_state, '-')}",
                f"  {'forecast family effect':26} -"]
    side = "inside" if support.family_within_radius else "outside"
    lines = [f"  {'forecast family distance':26} "
             f"{_fmt_radius(support.family_distance)}"
             f"   ({side} the family radius)"]
    effect = support.family_effect
    limit = f"{float(qm.MIX_SUPPORT['family_max_relative_effect']) * 100:.2f}%"
    label = f"  {'forecast family effect':26} "
    if support.family_within_radius:
        lines.append(f"{label}-   (not needed: inside the family radius)")
    elif support.family_admitted_by == "materiality":
        lines.append(f"{label}{effect.bound * 100:.2f}%   (within {limit}: "
                     f"admitted by family rate bounds)")
    elif effect.bound is not None:
        lines.append(f"{label}{effect.bound * 100:.2f}%   (above {limit})")
    elif effect.unavailable is \
            qm.FamilyEffectUnavailable.UNEVIDENCED_FAMILY_CHANGE:
        text = (f"not bounded: {', '.join(sorted(effect.families))} "
                f"changed share without rate bounds")
        wrapped = textwrap.wrap(
            text, width=_COMPOSITION_TEXT_WIDTH - _VALUE_COLUMN,
            break_long_words=False, break_on_hyphens=False)
        lines.append(f"{label}{wrapped[0]}")
        lines.extend(" " * _VALUE_COLUMN + rest for rest in wrapped[1:])
    else:
        reason = (effect.unavailable.value if effect.unavailable is not None
                  else "-")
        lines.append(f"{label}-   (not evaluated: {reason})")
    return lines


# ---------------------------------------------------------------------------
# The command (spec sections 8, 17, 23, 24).
# ---------------------------------------------------------------------------
def _reset(args, keys, *, now) -> int:
    """`--reset-calibration`, which is selected-account-only."""
    if getattr(args, "account", None) is None and accounts_are_decorated():
        eprint("quota: --reset-calibration needs --account on an install with "
               "more than one Claude account; refusing to clear every regime")
        for key in keys:
            eprint(f"  {key}  {account_display_label(key)}")
        return 2
    key = keys[0] if keys else None
    removed = reset_calibration(key, now=now)
    label = account_display_label(key) or "the merged view"
    if removed:
        print(f"quota: cleared the stored calibration for {label}")
    else:
        print(f"quota: nothing was stored for {label}")
    return 0


@dataclasses.dataclass(frozen=True)
class SweepOutcome:
    """What one account's sweep did (#1015 spec 5.5)."""

    #: The quarantine path `persist_and_detect` reported, or None.
    quarantined: "str | None"
    #: The fresh transitions this sweep's persistence created.
    transitions: tuple
    #: Every identity this sweep offered to `_settle_rate_change`.
    settled: frozenset


def sweep_account(result: AccountResult, *, now, delivery) -> SweepOutcome:
    """One account's automatic sweep, in `cmd_quota`'s order (spec 5.5):
    build `PersistMode`, `persist_and_detect`, supersession reconciliation
    (5.7), fresh recording, #689 recovery, settlement.

    Persistence runs AFTER the stores are closed, because the calibration
    lock is a leaf in the lock order and takes nothing else. #661 S2 §6.5
    steps 1-2: the descriptor is decided under the calibration file's leaf
    lock, and that lock is released before any stats lock is taken —
    `persist_and_detect` returns from its `with` block first.
    """
    import _lib_accounts
    mode = persist_mode(result, now=now)
    quarantined, transitions, candidates = persist_and_detect(
        result.clean, mode)
    # #1015 spec 5.7: every effectively superseded transition identity of
    # this account (and of the `unattributed` sentinel a merged fit records
    # under) gets its explicit supersession event BEFORE anything is
    # recorded, on every automatic invocation, withheld analyses included.
    if mode.kind == "automatic":
        reconcile_rate_change_supersessions(
            (_alert_account_key(result.account_key),
             _lib_accounts.UNATTRIBUTED), now=now)
    settled: set = set()
    for transition in transitions:
        # §6.5 steps 3-6: through the sole stats writer, so the append, the
        # row and the cursor commit together and the notification dispatches
        # only after that commit. One write can create more than one adjacent
        # pair, and each is recorded: the row's UNIQUE key dedups, so a pair
        # a prior run already recorded folds to a no-op rather than a second
        # alert.
        settled.add(transition.identity())
        _settle_rate_change(
            transition, record_rate_change_transition(transition, now=now),
            delivery)
    # #689: a transition an EARLIER run persisted and then failed to record.
    # It can never be fresh again, so it is recovered from the persisted
    # state instead. Fresh descriptors are excluded because they were just
    # recorded above, and only the remainder pays for the presence lookup.
    fresh = {t.identity() for t in transitions}
    for transition in unrecorded_rate_change_transitions(
            tuple(c for c in candidates if c.identity() not in fresh)):
        settled.add(transition.identity())
        _settle_rate_change(
            transition, record_rate_change_transition(transition, now=now),
            delivery)
    return SweepOutcome(quarantined=quarantined, transitions=transitions,
                        settled=frozenset(settled))


def cmd_quota(args) -> int:
    """`cctally quota` — weekly quota consumption and metering-rate change.

    Exit codes come from `QuotaAnalysis.exit_code`, never from `STATUS_EXIT`.
    Spec section 23 superseded section 19's rule that the status alone decides
    the verdict and the exit code: the status describes the current predictive
    calibration and the verdict describes the detector, so a confirmed change
    whose only shortfall is the successor regime's day count reports
    `insufficient-history` with `rate-change-detected` at exit 1 — which
    `STATUS_EXIT` maps to 4. Exit 2 is argument errors only.
    """
    import _lib_accounts
    now = _cctally_core._command_as_of()
    try:
        since = parse_date_argument(getattr(args, "since", None), "--since")
        watch_from = parse_date_argument(
            getattr(args, "watch_from", None), "--watch-from")
    except ValueError as exc:
        eprint(f"quota: {exc}")
        return 2

    keys, code = resolve_accounts(args)
    if code is not None:
        return code
    if getattr(args, "reset_calibration", False):
        return _reset(args, keys, now=now)

    decorated = accounts_are_decorated()
    loaded = load_calibrations(now=now)
    # #695: once per invocation, before anything is recorded. A first run over
    # a store that predates this change marks every existing identity decided,
    # so the upgrade does not fire the whole history. `delivery` is carried
    # rather than re-read, which is what keeps an unusable ledger to ONE
    # stderr line per invocation instead of one per account.
    delivery = seed_delivery_state()
    if not delivery.usable and delivery.seed_deferred:
        # The FILE is fine; this run could not finish seeding it. Repairing or
        # removing a healthy ledger is the wrong remedy, and a stats read that
        # fails during a rebuild is the common way to reach this.
        eprint("quota: undelivered rate-change notifications will not be "
               f"retried on this run: {delivery.reason}")
    elif not delivery.usable:
        eprint(f"quota: {delivery_path().name} {delivery.reason}; "
               "undelivered rate-change notifications will not be retried "
               "until it is repaired or removed")
    swept_keys: list = []
    settled: set = set()
    results = []
    for account_key in keys or [None]:
        result = analyse_account(
            account_key, now=now, since=since, watch_from=watch_from,
            stored_state=loaded.state)
        outcome = sweep_account(result, now=now, delivery=delivery)
        quarantined = outcome.quarantined or loaded.quarantined
        results.append(dataclasses.replace(result, quarantined=quarantined))
        swept_keys.append(_alert_account_key(account_key))
        settled |= outcome.settled

    # #696: the `unattributed` sentinel joins `swept_keys` on every
    # successful analysis, bare or filtered, whatever accounts this run
    # analysed. It is added unconditionally, but that is not a promise that
    # the sweep RUNS — the `delivery.usable` gate below suppresses the whole
    # sweep on a ledger this invocation could not read or seed, and the
    # sentinel is suppressed with everything else.
    # `resolve_accounts` decides which populations are ANALYSED, and it
    # appends the sentinel only when `_unattributed_bucket_has_rows()` finds
    # one in `weekly_usage_snapshots` — a different table from the
    # `meter_rate_change_events` the sweep reads, and nothing keeps the two in
    # step. An install that spent its single-account lifetime recording under
    # `_alert_account_key(None)` and then grew a second account has sentinel
    # rows and no sentinel snapshot, so every one of them would fall outside
    # the sweep. `recorded_rate_change_identities` normalises its argument
    # through a set, so a key the loop already appended folds to a no-op.
    swept_keys.append(_lib_accounts.UNATTRIBUTED)

    # #695: the owed sweep. It reads the DURABLE ROWS, restricted to the
    # accounts this invocation analysed plus the always-swept `unattributed`
    # sentinel, because `candidates` comes from the calibration file and a
    # reset or a quarantine would strand an identity whose row still exists.
    # It runs after the loop because it is one query over one account set
    # rather than per-account work.
    if delivery.usable:
        for transition in _owed_rate_change_transitions(
                swept_keys, delivery, now=now, settled=settled):
            # §3.2: `unusable` suppresses the REST of the sweep. Another
            # process can corrupt or truncate the ledger after this
            # invocation's snapshot was taken, and every remaining identity
            # would then pay for a full authoritative ingest whose result the
            # claim discards.
            if _settle_rate_change(
                    transition,
                    record_rate_change_transition(
                        transition, now=now, notification_owed=True),
                    delivery) == _rate_change_delivery().CLAIM_UNUSABLE:
                break

    if getattr(args, "json", False):
        print(json.dumps(build_payload(results, now=now, decorated=decorated),
                         indent=2))
    else:
        for line in render_text(results, now=now, decorated=decorated):
            print(line)
    worst = max(results, key=lambda r: EXIT_SEVERITY[r.analysis.exit_code])
    return worst.analysis.exit_code
