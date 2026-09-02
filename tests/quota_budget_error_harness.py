#!/usr/bin/env python3
"""Measure the forecast budget row's realized error (#661 S2, spec §2.1/§2.3).

Finding F3 was published as a HYPOTHESIS and named the observation its owning
session had to make. That measurement ships here as a read-only script rather
than as prose, because §4's acceptance criterion is a RE-RUN of it against the
repaired code, compared with the committed pre-repair artifact.

METHOD, exactly as §2.1 states it. For each COMPLETED subscription week in the
store, at each UTC day boundary inside that week, call the shipped
`_cctally_forecast._load_forecast_inputs(conn, t, skip_sync=True)` — so the
real `_select_dollars_per_percent` runs and the measurement is of the shipped
selection rather than of a re-implementation. At each such decision point,
compare the predicted remaining spend

    realized_remaining_movement(t) * dollars_per_percent(t)

against the spend actually recorded over the remainder of the week. The signed
relative error is `(predicted - actual) / actual`, and the summary reports its
median absolute value, its p90, its worst case, its signed median, and the
same four broken down by which `dollars_per_percent_source` branch fired.

#670 SCOPING. `_apply_midweek_reset_override` matches `week_reset_events` for
THE WEEK UNDER EVALUATION and restricts the event to one detected by the
historical replay instant. Both axes are recorded in the artifact, because a
run whose scoping is not stated is not reproducible. The shipped loader owns
that causal boundary; this harness no longer monkeypatches a parallel lookup.

WEEK COALESCING. The store spells one physical week many ways. Hour-level
normalisation removes most of that, but not all: two windows can claim the
same week four or five hours apart, or share an end instant with different
starts. Those survivors are coalesced by the rule `WEEK_COALESCING` states,
and the reset events are what keep the rule from over-merging three genuinely
distinct April weeks whose nominal spans overlap. Both the rule and the
per-week window breakdown are recorded in the artifact.

REFUSAL, not degradation. A store that cannot supply the population produces
an artifact carrying the population it found, NO summary, and exit 3. A
summary computed over a population of nothing is the failure this script
exists to make impossible.

Usage:
    python3 tests/quota_budget_error_harness.py \\
        --store <dir> --account <key|merged> --now <iso> [--out <path>]

Exit codes: 0 a summary was produced, 2 invalid invocation, 3 the store could
not supply the population.

Read-only: it opens the store's databases and writes only the artifact.
Maintainer-local measurement tooling — not a `cctally` subcommand.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import statistics
import sys

UTC = dt.timezone.utc
ARTIFACT_SCHEMA_VERSION = 1
REPO = pathlib.Path(__file__).resolve().parents[1]
BIN_DIR = REPO / "bin"

#: How this harness scopes the `week_reset_events` lookup, recorded in the
#: artifact. See the #670 paragraph above. The value names BOTH axes,
#: because a scoping that stated only the detection filter would describe a
#: store-global count as a per-week one.
RESET_SCOPING = "new_week_end_at match AND detected_at_utc<=t"

#: Decision points per completed week: the UTC day boundaries strictly inside
#: the week. Six, matching the population §2.1 measured.
DECISION_POINTS_PER_WEEK = 6

#: How this harness turns stored week spellings into physical weeks, recorded
#: in the artifact. See `_completed_weeks` for why each clause is there.
WEEK_COALESCING = (
    "normalize both boundaries to the nearest hour, then merge two normalized "
    "windows whose half-open spans overlap for more than half of the shorter "
    "span, unless a week_reset_events row records the transition between "
    "their end instants; each physical week takes the boundaries of its "
    "most-populated window and the maximum weekly_percent over every merged "
    "spelling")

#: The overlap a merge requires, as a fraction of the shorter of the two
#: spans. See `_coalesce_windows` for the margins this sits between.
WEEK_OVERLAP_FRACTION = 0.5


# ---------------------------------------------------------------------------
# Artifact schema — one definition, used by the writer and by the test that
# validates the committed baseline against it.
# ---------------------------------------------------------------------------
_POPULATION_KEYS = ("weeks", "decisionPoints", "usablePoints", "span")
_SUMMARY_KEYS = ("medianAbsPct", "p90AbsPct", "worstAbsPct", "signedMedianPct",
                 "byPath")
_BY_PATH_KEYS = ("points", "medianAbsPct", "p90AbsPct", "worstAbsPct",
                 "signedMedianPct", "overPredicted")
_RUN_KEYS = ("store", "account", "now", "resetScoping", "weekCoalescing",
             "prerepair", "prerepairEvidence", "provenance")


def validate_artifact(payload) -> list:
    """Problems with `payload` as an artifact of this harness. Empty is good.

    Structural only: it says nothing about whether the numbers are right, and
    is deliberately tolerant of unknown keys so the schema can grow.
    """
    problems: list = []
    if not isinstance(payload, dict):
        return ["the artifact is not an object"]
    if payload.get("schemaVersion") != ARTIFACT_SCHEMA_VERSION:
        problems.append(
            f"schemaVersion is {payload.get('schemaVersion')!r}, "
            f"expected {ARTIFACT_SCHEMA_VERSION}")
    run = payload.get("run")
    if not isinstance(run, dict):
        problems.append("run is missing or not an object")
    else:
        for key in _RUN_KEYS:
            if key not in run:
                problems.append(f"run.{key} is missing")
        if run.get("resetScoping") != RESET_SCOPING:
            problems.append(
                f"run.resetScoping is {run.get('resetScoping')!r}, "
                f"expected {RESET_SCOPING!r}")
        if run.get("weekCoalescing") != WEEK_COALESCING:
            problems.append(
                f"run.weekCoalescing is {run.get('weekCoalescing')!r}, "
                f"expected {WEEK_COALESCING!r}")
    population = payload.get("population")
    if not isinstance(population, dict):
        problems.append("population is missing or not an object")
    else:
        for key in _POPULATION_KEYS:
            if key not in population:
                problems.append(f"population.{key} is missing")
    for name in ("weeks", "points"):
        if not isinstance(payload.get(name), list):
            problems.append(f"{name} is missing or not a list")
    summary = payload.get("summary")
    if summary is None:
        # A refusal artifact carries no summary at all, and that is valid.
        return problems
    if not isinstance(summary, dict):
        problems.append("summary is not an object")
        return problems
    for key in _SUMMARY_KEYS:
        if key not in summary:
            problems.append(f"summary.{key} is missing")
    by_path = summary.get("byPath")
    if not isinstance(by_path, dict):
        problems.append("summary.byPath is missing or not an object")
    else:
        for branch, block in by_path.items():
            if not isinstance(block, dict):
                problems.append(f"summary.byPath.{branch} is not an object")
                continue
            for key in _BY_PATH_KEYS:
                if key not in block:
                    problems.append(f"summary.byPath.{branch}.{key} missing")
    return problems


# ---------------------------------------------------------------------------
# Loading the shipped code against an arbitrary store
# ---------------------------------------------------------------------------
def _load_cctally(store: pathlib.Path):
    """The shipped module, with its data directory pinned to `store`.

    Through `tests/_script_loader.load_script_module`, which is the estate's
    ONE loader for `bin/cctally`; a hand-rolled compile-and-exec here would
    be a second implementation, and `tests/test_script_loader.py` refuses
    those as a rule rather than as a list. The environment is pinned FIRST,
    because the loader re-derives every path constant from it during the
    load and a later assignment would not be seen.
    """
    tests_dir = str(REPO / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    os.environ["CCTALLY_DATA_DIR"] = str(store)
    os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    os.environ["CCTALLY_DISABLE_UPDATE_CHECK"] = "1"
    os.environ["CCTALLY_DISABLE_RETENTION_SWEEP"] = "1"
    os.environ["CCTALLY_DISABLE_TELEMETRY"] = "1"
    from _script_loader import load_script_module

    return load_script_module()


def _parse_instant(value, label):
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _account_clause(account_key):
    if account_key is None:
        return "", []
    return " AND account_key = ?", [account_key]


#: The tables the shipped selection path reads. The probe below replays their
#: DDL out of the measured store's own `sqlite_master`, so its fixture cannot
#: drift from the production schema.
_PROBE_TABLES = ("weekly_usage_snapshots", "week_reset_events",
                 "weekly_credit_floors")

#: The probe's fixture clock and the current week it measures from.
_PROBE_NOW = dt.datetime(2030, 1, 31, tzinfo=UTC)
_PROBE_CURRENT_WEEK_START = dt.datetime(2030, 1, 29, tzinfo=UTC)


def _probe_conn(conn):
    """An in-memory database carrying the store's own schema for those tables.

    The DDL is read out of `sqlite_master` rather than written here, because a
    hand-transcribed fixture schema would be a second definition that drifts.
    """
    probe = __import__("sqlite3").connect(":memory:")
    placeholders = ",".join("?" for _ in _PROBE_TABLES)
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table'"
        f" AND name IN ({placeholders})", list(_PROBE_TABLES)).fetchall()
    if len(rows) != len(_PROBE_TABLES):
        raise ValueError("the store does not carry the probe's tables")
    for (sql,) in rows:
        probe.execute(sql)
    return probe


def _probe_seed(probe, *, credited):
    """Four prior weeks, the most recent optionally credited mid-week.

    The credited week is the shape spec §4.1 names: a pre-credit reading of 46
    and a post-credit reading of 31, with NO synthetic post-credit baseline
    snapshot. `_floored_week_max` returns 31 there — a fragment of the week's
    real movement — which is the defect §4 repairs. Under the repair the week
    is withheld instead, leaving three eligible candidates where the median
    needs four.
    """
    for index in range(4):
        start = dt.datetime(2029, 12, 25, tzinfo=UTC) + dt.timedelta(
            days=7 * index)
        end = start + dt.timedelta(days=7)
        readings = [(1, 2.0), (3, 20.0), (5, 40.0)]
        if credited and index == 3:
            readings = [(2, 46.0), (5, 31.0)]
            probe.execute(
                "INSERT INTO weekly_credit_floors (week_start_date,"
                " effective_at_utc, observed_pre_credit_pct, applied_at_utc,"
                " account_key) VALUES (?, ?, ?, ?, 'unattributed')",
                (start.date().isoformat(),
                 (start + dt.timedelta(days=4)).isoformat(), 46.0,
                 (start + dt.timedelta(days=4)).isoformat()))
        for offset_days, percent in readings:
            probe.execute(
                "INSERT INTO weekly_usage_snapshots (captured_at_utc,"
                " week_start_date, week_end_date, week_start_at, week_end_at,"
                " weekly_percent, payload_json, source, account_key)"
                " VALUES (?, ?, ?, ?, ?, ?, '{}', 'probe', 'unattributed')",
                ((start + dt.timedelta(days=offset_days)).isoformat(),
                 start.date().isoformat(), end.date().isoformat(),
                 start.isoformat(), end.isoformat(), percent))
    probe.commit()


def _probe_source(module, conn, *, credited):
    """The `dollars_per_percent` source label the shipped selector reaches.

    Cost is stubbed to a constant: the probe is about which BRANCH the
    selection takes, and reading the real cache for four fabricated 2030 weeks
    would make the probe both slow and dependent on data it does not control.
    """
    probe = _probe_conn(conn)
    try:
        _probe_seed(probe, credited=credited)
        original = module._sum_cost_for_range
        module._sum_cost_for_range = lambda *a, **k: 100.0
        try:
            _rate, source = module._select_dollars_per_percent(
                probe, _PROBE_NOW, _PROBE_CURRENT_WEEK_START, 5.0, 50.0,
                skip_sync=True, account_key=None)
        finally:
            module._sum_cost_for_range = original
    finally:
        probe.close()
    return source


def _classify_repair(module, conn):
    """`(prerepair, evidence)` — decided by running the shipped selector.

    `run.prerepair` used to be the constant `True`, so the ONE field that
    distinguishes the committed baseline from Stage B's re-run could not
    distinguish them. Deriving it from the presence of a FUNCTION NAME was no
    better: `bin/cctally` re-exports its siblings one explicit line at a time,
    so a reducer added to `bin/_cctally_forecast.py` is invisible on the
    loaded namespace unless somebody also adds that line, and a post-repair
    run would still have claimed to be the baseline.

    So the flag is decided by BEHAVIOUR. Two cases run against the same
    fabricated four-week population:

    * the control carries no credit, and the trailing median must be reachable
      in both the pre-repair and the repaired code;
    * the treatment credits the most recent week and supplies no post-credit
      baseline, which the pre-repair code prices off `_floored_week_max` and
      the repaired code withholds.

    A control that does not reach the trailing median means the probe itself
    is not exercising the branch, and the answer is `None` with the reason
    stated rather than a guess in either direction.
    """
    try:
        control = _probe_source(module, conn, credited=False)
        treatment = _probe_source(module, conn, credited=True)
    except Exception as exc:                           # noqa: BLE001
        return None, (
            "undecided: the repair probe could not run "
            f"({type(exc).__name__}: {exc})")
    if control != "trailing_4wk_median":
        return None, (
            "undecided: the probe's control population did not reach the "
            f"trailing median at all (source {control!r}), so the treatment "
            "says nothing about the repair")
    if treatment == "trailing_4wk_median":
        return True, (
            "pre-repair: the shipped selector priced a credited week with no "
            "post-credit baseline off _floored_week_max and kept it in the "
            f"trailing median (control {control!r}, treatment {treatment!r})")
    return False, (
        "repaired: the shipped selector withheld a credited week with no "
        "post-credit baseline, leaving too few candidates for the trailing "
        f"median (control {control!r}, treatment {treatment!r})")


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------
def _normalized_windows(module, conn, account_key):
    """The store's `(week_start_at, week_end_at)` spellings, hour-normalized.

    Returns one record per NORMALIZED boundary pair, carrying how many
    snapshots reached it, the highest percentage any of them recorded, and the
    raw spellings it absorbed.

    `_normalize_week_boundary_dt` is the shipped normalisation — the same one
    `_apply_midweek_reset_override` uses to build its lookup key. On the
    measured store it takes 97 raw string pairs down to 33 windows.
    """
    clause, params = _account_clause(account_key)
    rows = conn.execute(
        "SELECT week_start_at, week_end_at, COUNT(*), MAX(weekly_percent)"
        " FROM weekly_usage_snapshots WHERE week_start_at IS NOT NULL"
        " AND week_end_at IS NOT NULL AND weekly_percent IS NOT NULL"
        + clause + " GROUP BY week_start_at, week_end_at", params).fetchall()
    windows: dict = {}
    for start_iso, end_iso, count, highest in rows:
        try:
            start = module._normalize_week_boundary_dt(
                _parse_instant(start_iso, "week_start_at"))
            end = module._normalize_week_boundary_dt(
                _parse_instant(end_iso, "week_end_at"))
        except ValueError:
            continue
        window = windows.get((start, end))
        if window is None:
            window = windows[(start, end)] = {
                "start": start, "end": end, "snapshots": 0,
                "highestPercent": None, "spellings": [],
            }
        window["snapshots"] += int(count)
        if highest is not None:
            window["highestPercent"] = (
                float(highest) if window["highestPercent"] is None
                else max(window["highestPercent"], float(highest)))
        window["spellings"].append((start_iso, end_iso))
    return [windows[key] for key in sorted(windows)]


def _reset_separated_ends(module, conn, account_key):
    """Normalized end-instant pairs that a recorded reset event separates.

    `week_reset_events` is the authoritative record that one metering window
    ended and another began — spec §4.1 says the same thing about the movement
    reducer, and for the same reason: a boundary is a recorded fact, never an
    inference from the shape of the readings.

    Scoped by `account_key` exactly as `_normalized_windows` is. The two are
    the two halves of one coalescing decision, so a reset recorded under a
    different account must not separate windows this account owns. It is
    immaterial on the single-account store this harness measured — the
    account clause is empty for `merged` — and stating the scope is what
    keeps the halves from disagreeing on a store where it is not.
    """
    clause, params = _account_clause(account_key)
    pairs: set = set()
    try:
        rows = conn.execute(
            "SELECT old_week_end_at, new_week_end_at FROM week_reset_events"
            + (" WHERE 1=1" + clause if clause else ""), params
        ).fetchall()
    except Exception:                                  # noqa: BLE001
        return pairs
    for old_iso, new_iso in rows:
        try:
            old = module._normalize_week_boundary_dt(
                _parse_instant(old_iso, "old_week_end_at"))
            new = module._normalize_week_boundary_dt(
                _parse_instant(new_iso, "new_week_end_at"))
        except ValueError:
            continue
        pairs.add((old, new))
        pairs.add((new, old))
    return pairs


def _coalesce_windows(windows, separated):
    """Group `windows` into physical weeks. Returns a list of window lists.

    Two windows are the same physical week when they overlap for more than
    half of the shorter of the two spans AND no `week_reset_events` row
    records the transition between their end instants.

    Hour-level normalisation alone is not enough. On the measured store the
    subscription week of 2026-03-13 appears three times — one window carrying
    1,457 snapshots and two carrying a single stray row each, four and five
    hours off — and 2026-02-26T08:00 and 2026-02-27T07:00 both claim the week
    ending 2026-03-06T07:00. Neither has a reset record, so both are jitter.

    Bare overlap is too weak a test, because a stray window shifted four hours
    LATE also overlaps the following real week by those four hours, which
    chains two real weeks into one component through the stray: the first
    revision of this rule swallowed the whole week of 2026-03-13 that way. The
    fraction separates the two cases with a wide margin on this store — the
    jitter replicas overlap their own week by 97.6% to 100% of the shorter
    span, while a stray overlaps its NEIGHBOUR by 2.4% and the earliest April
    pair by 7.1%.

    The reset clause is what stops the rule from over-merging where the
    fraction cannot. The windows 2026-04-16T19:00 and 2026-04-18T05:00 overlap
    by 80% of the shorter span, and are two genuinely distinct metering weeks:
    reset event 6 records exactly that transition. Spec §4.1 makes the same
    call for the movement reducer — a boundary is a recorded fact, never an
    inference from the shape of the readings.

    Raises when a component ends up holding a pair a reset separates. Merging
    is transitive and separation is not, so a store could in principle chain
    A-B-C where A and C are separated; the harness refuses rather than
    publishing a population it cannot justify.
    """
    parent = list(range(len(windows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(windows)):
        for j in range(i + 1, len(windows)):
            a, b = windows[i], windows[j]
            overlap = (min(a["end"], b["end"])
                       - max(a["start"], b["start"])).total_seconds()
            if overlap <= 0:
                continue
            shorter = min((a["end"] - a["start"]).total_seconds(),
                          (b["end"] - b["start"]).total_seconds())
            if shorter <= 0 or overlap / shorter <= WEEK_OVERLAP_FRACTION:
                continue
            if (a["end"], b["end"]) in separated:
                continue
            parent[find(i)] = find(j)
    groups: dict = {}
    for index, window in enumerate(windows):
        groups.setdefault(find(index), []).append(window)
    components = sorted(groups.values(), key=lambda g: g[0]["start"])
    for component in components:
        for a in component:
            for b in component:
                if (a["end"], b["end"]) in separated:
                    raise ValueError(
                        "coalescing merged two windows a reset event "
                        f"separates: {a['end'].isoformat()} and "
                        f"{b['end'].isoformat()}")
    return components


def _completed_weeks(module, conn, now, account_key):
    """`[(week_start_at, week_end_at, final_percent, detail)]`, oldest first.

    A physical week takes the boundaries of the window carrying the most
    snapshots, because a stray window holding one row must not outvote one
    holding 1,457, and the highest percentage recorded across every window it
    absorbed, because a stray row is a real reading of the same week.
    """
    windows = _normalized_windows(module, conn, account_key)
    components = _coalesce_windows(
        windows, _reset_separated_ends(module, conn, account_key))
    weeks = []
    for component in components:
        dominant = max(component,
                       key=lambda w: (w["snapshots"], -w["start"].timestamp()))
        start, end = dominant["start"], dominant["end"]
        if end >= now:
            continue
        highest = [w["highestPercent"] for w in component
                   if w["highestPercent"] is not None]
        if not highest:
            continue
        detail = {
            "snapshots": sum(w["snapshots"] for w in component),
            "windows": [{"startAt": w["start"].isoformat(),
                         "endAt": w["end"].isoformat(),
                         "snapshots": w["snapshots"],
                         "highestPercent": w["highestPercent"],
                         "spellings": len(w["spellings"])}
                        for w in component],
        }
        weeks.append((start, end, max(highest), detail))
    return weeks


def _decision_points(start, end):
    """The UTC day boundaries strictly inside `[start, end)`."""
    first = (start.replace(hour=0, minute=0, second=0, microsecond=0)
             + dt.timedelta(days=1))
    points = []
    cursor = first
    while cursor < end and len(points) < DECISION_POINTS_PER_WEEK:
        if cursor > start:
            points.append(cursor)
        cursor += dt.timedelta(days=1)
    return points


def _measure(module, conn, now, account_key):
    """`(week_rows, point_rows)` over the store's completed weeks."""
    weeks = _completed_weeks(module, conn, now, account_key)
    week_rows = []
    point_rows = []
    for start, end, final_percent, detail in weeks:
        points = _decision_points(start, end)
        week_rows.append({
            "weekStartAt": start.isoformat(),
            "weekEndAt": end.isoformat(),
            "finalPercent": final_percent,
            "decisionPoints": len(points),
            **detail,
        })
        for at in points:
            row = _measure_point(module, conn, at, end, final_percent,
                                 account_key)
            if row is not None:
                point_rows.append(row)
    return week_rows, point_rows


def _measure_point(module, conn, at, week_end, final_percent, account_key):
    try:
        inputs = module._load_forecast_inputs(
            conn, at, skip_sync=True, account_key=account_key)
    except Exception as exc:                       # noqa: BLE001
        return {"at": at.isoformat(), "usable": False,
                "cause": f"load-failed: {type(exc).__name__}"}
    if inputs is None:
        return {"at": at.isoformat(), "usable": False,
                "cause": "no-current-week-snapshot"}
    if inputs.dollars_per_percent is None:
        return {"at": at.isoformat(), "usable": False,
                "cause": inputs.dollars_per_percent_source}

    movement = final_percent - inputs.p_now
    try:
        actual = module._sum_cost_for_range(
            at, week_end, mode="auto", skip_sync=True,
            account_key=account_key)
    except Exception as exc:                       # noqa: BLE001
        return {"at": at.isoformat(), "usable": False,
                "cause": f"cost-failed: {type(exc).__name__}"}
    predicted = movement * inputs.dollars_per_percent
    if actual is None or actual <= 0:
        return {"at": at.isoformat(), "usable": False,
                "cause": "no-remaining-spend"}
    return {
        "at": at.isoformat(),
        "usable": True,
        "path": inputs.dollars_per_percent_source,
        "pNow": inputs.p_now,
        "realizedRemainingMovementPct": movement,
        "dollarsPerPercent": inputs.dollars_per_percent,
        "predictedRemainingUsd": predicted,
        "actualRemainingUsd": actual,
        "signedRelativeErrorPct": (predicted - actual) / actual * 100.0,
    }


def _percentile(values, fraction):
    """The `fraction` quantile by nearest rank; None on an empty sequence."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1,
                max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _block(rows):
    errors = [r["signedRelativeErrorPct"] for r in rows]
    absolute = [abs(e) for e in errors]
    return {
        "points": len(rows),
        "medianAbsPct": statistics.median(absolute) if absolute else None,
        "p90AbsPct": _percentile(absolute, 0.9),
        "worstAbsPct": max(absolute) if absolute else None,
        "signedMedianPct": statistics.median(errors) if errors else None,
        "overPredicted": sum(1 for e in errors if e > 0),
    }


def _summarize(point_rows):
    usable = [r for r in point_rows if r.get("usable")]
    if not usable:
        return None
    overall = _block(usable)
    by_path: dict = {}
    for row in usable:
        by_path.setdefault(row["path"], []).append(row)
    return {
        "medianAbsPct": overall["medianAbsPct"],
        "p90AbsPct": overall["p90AbsPct"],
        "worstAbsPct": overall["worstAbsPct"],
        "signedMedianPct": overall["signedMedianPct"],
        "byPath": {name: _block(rows) for name, rows in sorted(
            by_path.items())},
    }


def _artifact(*, store, account, now, week_rows, point_rows, summary,
              refusal=None, provenance="", prerepair=None,
              prerepair_evidence=""):
    usable = [r for r in point_rows if r.get("usable")]
    span = None
    if week_rows:
        span = {"from": week_rows[0]["weekStartAt"],
                "to": week_rows[-1]["weekEndAt"]}
    payload = {
        "schemaVersion": ARTIFACT_SCHEMA_VERSION,
        "run": {
            "store": str(store),
            "account": account,
            "now": now.isoformat(),
            "resetScoping": RESET_SCOPING,
            "weekCoalescing": WEEK_COALESCING,
            "prerepair": prerepair,
            "prerepairEvidence": prerepair_evidence or (
                "undecided: the run did not reach the repair probe"),
            "provenance": provenance or (
                "measured by tests/quota_budget_error_harness.py against the "
                "named store"),
        },
        "population": {
            "weeks": len(week_rows),
            "decisionPoints": len(point_rows),
            "usablePoints": len(usable),
            "span": span,
        },
        "weeks": week_rows,
        "points": point_rows,
    }
    if refusal:
        payload["refusal"] = refusal
    if summary is not None:
        payload["summary"] = summary
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True,
                        help="The data directory holding stats.db + cache.db.")
    parser.add_argument("--account", default="merged",
                        help="An account key, or the literal `merged`.")
    parser.add_argument("--now", required=True,
                        help="The clock the replay measures against (ISO).")
    parser.add_argument("--out", default=None,
                        help="Write the artifact here as well as to stdout.")
    args = parser.parse_args(argv)

    try:
        now = _parse_instant(args.now, "--now")
    except ValueError:
        parser.error(f"--now is not an ISO instant: {args.now!r}")
        return 2
    store = pathlib.Path(args.store).expanduser()
    account_key = None if args.account == "merged" else args.account

    refusal = None
    week_rows: list = []
    point_rows: list = []
    summary = None
    # Undecided until the probe runs. A store that cannot be opened at all
    # never reaches it, and a refusal artifact makes no claim about the repair
    # in either direction.
    prerepair = None
    prerepair_evidence = ""
    if not store.is_dir():
        refusal = f"the store directory does not exist: {store}"
    else:
        try:
            module = _load_cctally(store)
            conn = module.open_db()
            try:
                prerepair, prerepair_evidence = _classify_repair(module, conn)
                week_rows, point_rows = _measure(
                    module, conn, now, account_key)
            finally:
                conn.close()
        except Exception as exc:                   # noqa: BLE001
            refusal = f"the store could not be read: {type(exc).__name__}: {exc}"
        else:
            summary = _summarize(point_rows)
            if summary is None:
                refusal = (
                    "the store supplied no usable decision point; a summary "
                    "over an empty population would be a fabricated answer")

    payload = _artifact(store=store, account=args.account, now=now,
                        week_rows=week_rows, point_rows=point_rows,
                        summary=summary, refusal=refusal,
                        prerepair=prerepair,
                        prerepair_evidence=prerepair_evidence)
    text = json.dumps(payload, indent=2) + "\n"
    if args.out:
        out = pathlib.Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0 if summary is not None else 3


if __name__ == "__main__":
    raise SystemExit(main())
