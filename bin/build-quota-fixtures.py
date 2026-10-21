#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally quota` (#661 S1).

Writes one `.local/share/cctally/{stats,cache}.db` per scenario under
`tests/fixtures/quota/<scenario>/`, plus — for the one scenario that needs it
— a seeded `quota-calibrations.json` standing in for a durable prior.

Every scenario is measured at one pinned instant, `AS_OF`, so a golden is a
recording of an answer rather than of a clock.

The generator holds the RATIO of weighted units to meter movement near a
chosen units-per-point while letting the daily VOLUME vary between three and
eight meter points. That distinction is load-bearing rather than cosmetic:
the eligibility fence bounds absolute daily volume and the detector's fence
bounds the ratio, so a fixture holding volume nearly constant makes every
lower-rate watch day fall below the eligibility fence and the verdict comes
back blocked rather than reported. Two more facts the fixtures encode, both
established by measurement:

* a series with no entry past its last complete day leaves that day
  `no-local-history`, which on a short series blows the incomplete-history
  budget and on a rate-change series removes the third watch day the detector
  needs, so every scenario writes one trailing entry;
* readings opening at 0 or 1 carry a meter delta short of the day's step,
  because spec section 4 gives those two readings half-width intervals, so
  every week opens at 2.

Run: `bin/build-quota-fixtures.py` (idempotent — overwrites existing DBs).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_stats_db,
    fixture_timestamp_utc,
    seed_account,
    seed_weekly_usage_snapshot,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/quota"
UTC = dt.timezone.utc

#: The pinned clock every scenario is measured at. Committed in each
#: scenario's `input.env` as well, because the harness passes it through
#: `CCTALLY_AS_OF`.
AS_OF = dt.datetime(2026, 8, 28, 6, 0, tzinfo=UTC)

#: The supported-composition floor the glue enforces. Every scenario's series
#: starts after it, so no scenario is accidentally an unvalidated era.
ERA_FLOOR = dt.date(2026, 7, 25)

#: Meter points per day, cycled. Wide enough that the eligibility fence sits
#: well below a lower-rate day's volume.
STEPS = (5, 3, 7, 4, 8, 6, 5)

#: Multiplicative noise on the RATIO, cycled. Small enough that three watch
#: days fit inside the 15% detection-width gate at the t(2) quantile of 4.303.
RATIO_JITTER = (1.0, 1.01, 0.99, 1.005, 0.995, 1.015, 0.985)

BUDGET = 2_000_000.0
WATCH_BUDGET = 1_400_000.0

class Store:
    """The two databases of one scenario, plus a monotonic entry counter.

    Both stores are created through the SHARED fixture builders, which
    produce the post-migration production schema. A hand-written schema is
    not an option here: `cctally quota` opens both stores through the guarded
    openers, so a fixture at an unrecognised schema head sends the migration
    dispatcher down its fresh-install path and the command then measures a
    store the builder did not seed.
    """

    def __init__(self, home: Path):
        share = home / ".local" / "share" / "cctally"
        share.mkdir(parents=True, exist_ok=True)
        for name in ("stats.db", "cache.db", "conversations.db"):
            for suffix in ("", "-wal", "-shm"):
                (share / f"{name}{suffix}").unlink(missing_ok=True)
        (share / "quota-calibrations.json").unlink(missing_ok=True)
        create_stats_db(share / "stats.db")
        create_cache_db(share / "cache.db")
        self.home = home
        self.share = share
        self.stats = sqlite3.connect(share / "stats.db")
        self.cache = sqlite3.connect(share / "cache.db")
        self.offset = 0

    def snapshot(self, at, week_anchor, percent, *, source="statusline",
                 account_key="unattributed"):
        seed_weekly_usage_snapshot(
            self.stats, captured_at_utc=at.isoformat(),
            week_start_date=week_anchor.date().isoformat(),
            week_end_date=(week_anchor + dt.timedelta(days=7)).date()
            .isoformat(),
            week_start_at=week_anchor.isoformat(),
            week_end_at=(week_anchor + dt.timedelta(days=7)).isoformat(),
            weekly_percent=float(percent), source=source,
            account_key=account_key)

    def entry(self, at, *, model="claude-opus-5", fresh=0, output=0,
              cache_create=0, cache_1h=0, cache_read=0, account_key=None,
              speed=None, path="/p/a.jsonl"):
        # Direct SQL rather than `seed_session_entry`, which carries no
        # `cache_create_1h_tokens` parameter. That column is the one the
        # kernel withholds a day over when it is NULL beside a positive
        # cache-write total, so a fixture must be able to write it.
        self.cache.execute(
            "INSERT INTO session_entries (source_path, line_offset,"
            " timestamp_utc, model, input_tokens, output_tokens,"
            " cache_create_tokens, cache_read_tokens, cache_create_1h_tokens,"
            " account_key, speed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (path, self.offset, fixture_timestamp_utc(at), model, int(fresh),
             int(output), int(cache_create), int(cache_read), cache_1h,
             account_key, speed))
        self.offset += 1

    def account(self, key, email, label=None):
        seed_account(self.stats, account_key=key, provider="claude",
                     natural_id=key, email=email, label=label,
                     first_seen_utc="2026-07-25T00:00:00Z",
                     last_seen_utc="2026-08-27T00:00:00Z")

    def close(self):
        self.stats.commit()
        self.cache.commit()
        self.stats.close()
        self.cache.close()


def _week_anchor(first_day: dt.date) -> dt.datetime:
    return dt.datetime.combine(first_day - dt.timedelta(days=1), dt.time(8),
                               tzinfo=UTC)


def seed_series(store: Store, *, days: int, budget: float = BUDGET,
                watch_budget: float | None = None, watch_days: int = 0,
                model: str = "claude-opus-5", watch_model: str | None = None,
                sparse_last_day: bool = False, absent_days: tuple = (),
                account_key=None, first: dt.date | None = None) -> dt.date:
    """Seed `days` complete days ending the day before `AS_OF`.

    Returns the last seeded date. `absent_days` names indexes whose meter
    moves with no token entry at all, which is `no-local-history` rather than
    sparse — a different absence with a different remedy.
    """
    last = AS_OF.date() - dt.timedelta(days=1)
    first = first or (last - dt.timedelta(days=days - 1))
    assert first >= ERA_FLOOR, "a scenario must start inside the supported era"
    anchor = _week_anchor(first)
    running: dict = {}
    for index in range(days):
        date = first + dt.timedelta(days=index)
        week = anchor + dt.timedelta(
            days=7 * ((date - anchor.date()).days // 7))
        scale = budget
        row_model = model
        if watch_days and index >= days - watch_days:
            scale = watch_budget if watch_budget is not None else budget
            row_model = watch_model or model
        step = STEPS[index % len(STEPS)]
        if sparse_last_day and index == days - 1:
            step = 2
        opening = running.get(week, 2)
        running[week] = opening + step
        for hour, pct in ((9, opening), (21, opening + step)):
            store.snapshot(dt.datetime.combine(date, dt.time(hour),
                                               tzinfo=UTC),
                           week, pct, account_key=account_key or
                           "unattributed")
        if index in absent_days:
            continue
        units = step * scale * RATIO_JITTER[index % len(RATIO_JITTER)]
        store.entry(dt.datetime.combine(date, dt.time(12), tzinfo=UTC),
                    model=row_model, fresh=int(units),
                    account_key=account_key,
                    path=f"/p/{account_key or 'a'}.jsonl")
    store.entry(dt.datetime.combine(AS_OF.date(), dt.time(0, 30), tzinfo=UTC),
                model=model, fresh=1000, account_key=account_key,
                path=f"/p/{account_key or 'a'}-tail.jsonl")
    return first + dt.timedelta(days=days - 1)


# --------------------------------------------------------------------------
# Scenarios. Each takes the scenario's fake HOME and seeds it.
# --------------------------------------------------------------------------
def scenario_steady(home: Path) -> None:
    """The success path: a trustworthy fit and no rate change."""
    store = Store(home)
    seed_series(store, days=30)
    store.close()


def scenario_rate_change(home: Path) -> None:
    """A confirmed change: exit 1 with both regimes published."""
    store = Store(home)
    seed_series(store, days=29, watch_budget=WATCH_BUDGET, watch_days=3)
    store.close()


def scenario_credit_only(home: Path) -> None:
    """A credit alone. The transition day is excluded and no rate change is
    manufactured, which is the whole point of reading the authoritative
    credit tables rather than inferring a fork from a meter decrease."""
    store = Store(home)
    last = seed_series(store, days=30)
    credit_at = dt.datetime.combine(last - dt.timedelta(days=8),
                                    dt.time(15), tzinfo=UTC)
    store.stats.execute(
        "INSERT INTO week_reset_events (detected_at_utc, old_week_end_at,"
        " new_week_end_at, effective_reset_at_utc, observed_pre_credit_pct,"
        " account_key) VALUES (?,?,?,?,?,?)",
        (credit_at.isoformat(), credit_at.isoformat(),
         (credit_at + dt.timedelta(days=7)).isoformat(),
         credit_at.isoformat(), 30.0, "unattributed"))
    store.close()


def scenario_sparse_in_run(home: Path) -> None:
    """Sparse local history inside the decisive run preempts the verdict.

    The blocking reason is its own closed vocabulary, so the command names
    what happened instead of borrowing `local-history-incomplete` and telling
    a user whose days are fully ingested that their history is missing.
    """
    store = Store(home)
    seed_series(store, days=30, watch_budget=WATCH_BUDGET, watch_days=4,
                sparse_last_day=True)
    store.close()


def scenario_unsupported_mix(home: Path) -> None:
    """An unrecognized family in the recent days.

    A rate change is neither confirmed nor denied here, and the renderer must
    say so: the evidence needed to decide is missing, which is not the same
    claim as "no rate change".
    """
    store = Store(home)
    seed_series(store, days=29, watch_budget=WATCH_BUDGET, watch_days=3,
                watch_model="some-unreleased-model-9")
    store.close()


def scenario_precedence(home: Path) -> None:
    """Incomplete local history outranks an unsupported mix and a thin fit.

    Spec section 9's fourth case: all three conditions at once, with the
    precedence order visible in the reported status.
    """
    store = Store(home)
    seed_series(store, days=12, watch_days=2,
                watch_model="some-unreleased-model-9",
                absent_days=(8, 9, 10))
    store.close()


def scenario_recorded_prior(home: Path) -> None:
    """A durable prior whose fingerprint and value diverge from a fresh fit.

    This is the epic's "a plan whose fitted budget differs from a recorded
    prior" criterion, read explicitly: this design has no plan-tier concept
    and does not invent one. What it has is a recorded calibration that
    disagrees with what the store now supports, which is the situation a plan
    change produces and the one the command must handle.
    """
    store = Store(home)
    seed_series(store, days=30)
    store.close()
    (home / ".local" / "share" / "cctally" / "quota-calibrations.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "accounts": {"*": {"regimes": [{
                "effectiveFrom": "2026-07-25T00:00:00+00:00",
                "effectiveUntil": None,
                "fingerprint": "a-fingerprint-from-earlier-constants",
                "algorithmRevision": 1,
                "unitsPerPoint": 1_612_643.0,
                "interval": {"lo": 1_550_000.0, "hi": 1_675_000.0},
                "support": {"days": 21, "segments": 3},
                "status": "ok",
                "asOf": "2026-07-24T00:00:00+00:00",
                "qualifications": [],
            }]}},
        }, indent=2) + "\n")


def scenario_two_accounts(home: Path) -> None:
    """Two real accounts at materially different rates.

    Fitting one budget across both would land between them and describe
    neither meter, so each is analysed independently and reported separately,
    and the #341 R8 decoration appears only because there is more than one
    REAL account.
    """
    store = Store(home)
    store.account("acct-alpha", "alpha@example.com")
    store.account("acct-beta", "beta@example.com")
    seed_series(store, days=30, budget=2_000_000.0, account_key="acct-alpha")
    seed_series(store, days=30, budget=3_200_000.0, account_key="acct-beta")
    store.close()


def scenario_offset_credit_and_fast(home: Path) -> None:
    """A mixed-offset credit boundary plus a usage-credit fast request."""
    store = Store(home)
    seed_series(store, days=30)
    for effective in ("2026-07-25T02:00:00+03:00",
                      "2026-07-25T03:00:00+03:00"):
        store.stats.execute(
            "INSERT INTO weekly_credit_floors (week_start_date,"
            " effective_at_utc, observed_pre_credit_pct, applied_at_utc,"
            " account_key) VALUES (?,?,?,?,?)",
            ("2026-07-25", effective, 30.0, effective, "unattributed"))
    store.entry(dt.datetime(2026, 8, 20, 12, tzinfo=UTC), fresh=50_000_000,
                speed="fast", path="/p/fast.jsonl")
    store.close()


def scenario_malformed_retained_instant(home: Path) -> None:
    """A malformed retained timestamp is withheld, never a traceback."""
    store = Store(home)
    seed_series(store, days=30)
    store.stats.execute(
        "UPDATE weekly_usage_snapshots SET captured_at_utc = ? WHERE id = ("
        "SELECT MIN(id) FROM weekly_usage_snapshots)",
        ("2026-13-45T00:00:00Z",))
    store.close()


# --------------------------------------------------------------------------
# #978 — the Opus 5.5 coefficient era. Both scenarios carry their OWN clock,
# committed in their `input.env`, because the ten scenarios above stay pinned
# to the Opus 5 era at `AS_OF`.
# --------------------------------------------------------------------------
#: The `opus-5-5` token-class weights, as a literal copy. Deliberately NOT
#: imported from the kernel: the scenario must be able to catch a kernel that
#: weighs any aggregation path with the wrong era, which a fixture computing
#: its units with the kernel's own table could never do.
NEW_ERA_WEIGHTS = {"fresh": 1.0, "output": 0.64, "cache_1h": 1.0,
                   "cache_read": 0.0148}
#: The `opus-5` weights, likewise a literal copy, for scenario 12's pre-era
#: days.
OPUS5_WEIGHTS = {"fresh": 1.0, "output": 4.73, "cache_1h": 1.03,
                 "cache_read": 0.0031}

#: Scenario 11's known budget, in `opus-5-5` units per general-weekly point.
#: Each day's meter moves an integer number of points and its tokens weigh
#: `points * KNOWN_BUDGET * RATIO_JITTER` under NEW_ERA_WEIGHTS, so the
#: readings are the cumulative units divided by KNOWN_BUDGET up to that
#: jitter. EXPECTED: the golden's `calibration.fitted.value` equals
#: KNOWN_BUDGET within 1% under the ceiling rule (the jitter's mean is 1.0).
#: A kernel that weighs these days with the Opus 5 set, or applies a family
#: multiplier, lands far outside that tolerance.
KNOWN_BUDGET = 1_000_000.0

#: Three 5.5-family entry shapes with differently composed class mixes, the
#: hand-worked entries of the kernel's RED test 2b. Fields: model, fresh,
#: output, cache_create (total writes), cache_1h, cache_read.
NEW_ERA_SHAPES = (
    ("claude-opus-5-5", 1000, 200, 0, 0, 50_000),        # 1868 units
    ("claude-sonnet-5-5", 4000, 1000, 2000, 500, 0),     # 6640 units
    ("claude-haiku-5-5", 0, 3000, 0, 0, 100_000),        # 3400 units
)
#: How many of each shape a day carries, cycled, so no two consecutive days
#: share a family or class composition.
NEW_ERA_MIXES = ((1, 1, 1), (2, 0, 1), (0, 1, 2), (1, 2, 0), (3, 1, 0),
                 (0, 2, 1), (1, 0, 3))


def _shape_units(shape, weights) -> float:
    _model, fresh, output, cache_create, cache_1h, cache_read = shape
    # A five-minute write weighs as fresh input; the one-hour write has its
    # own coefficient. This is the kernel's decomposition, restated.
    return (weights["fresh"] * (fresh + (cache_create - cache_1h))
            + weights["output"] * output
            + weights["cache_1h"] * cache_1h
            + weights["cache_read"] * cache_read)


def _seed_new_era_day(store, date, target, mix, *, hour=12):
    """Entries whose NEW_ERA_WEIGHTS units total exactly `target`.

    The mix is scaled by a whole multiplier, so every token count stays an
    integer, and the remainder is topped up as fresh input on the Opus 5.5
    entry, whose weight is exactly 1.0.
    """
    base = sum(count * _shape_units(shape, NEW_ERA_WEIGHTS)
               for count, shape in zip(mix, NEW_ERA_SHAPES))
    scale = int(target // base)
    remainder = round(target - scale * base)
    for offset, (count, shape) in enumerate(zip(mix, NEW_ERA_SHAPES)):
        model, fresh, output, cache_create, cache_1h, cache_read = shape
        n = count * scale
        top_up = remainder if offset == 0 else 0
        if n == 0 and top_up == 0:
            continue
        store.entry(dt.datetime.combine(date, dt.time(hour, offset * 10),
                                        tzinfo=UTC),
                    model=model, fresh=fresh * n + top_up, output=output * n,
                    cache_create=cache_create * n, cache_1h=cache_1h * n,
                    cache_read=cache_read * n, path=f"/p/{model}.jsonl")


def scenario_opus55_steady_mixed_families(home: Path) -> None:
    """A steady Opus 5.5 era history over the three 5.5 families.

    Non-zero output, cache reads and both write durations, in a composition
    that changes every day, so the new coefficients and the new token-class
    floor are visible in the golden and a wrong weight cannot cancel through
    the fitted scale. Clock 2026-10-20T06:00Z; 21 complete days from
    2026-09-29. Expected: `ok`, `no-rate-change`, exit 0.
    """
    store = Store(home)
    first = dt.date(2026, 9, 29)
    days = 21
    anchor = _week_anchor(first)
    running: dict = {}
    for index in range(days):
        date = first + dt.timedelta(days=index)
        week = anchor + dt.timedelta(
            days=7 * ((date - anchor.date()).days // 7))
        step = STEPS[index % len(STEPS)]
        opening = running.get(week, 2)
        running[week] = opening + step
        for hour, pct in ((9, opening), (21, opening + step)):
            store.snapshot(dt.datetime.combine(date, dt.time(hour),
                                               tzinfo=UTC), week, pct)
        _seed_new_era_day(
            store, date,
            step * KNOWN_BUDGET * RATIO_JITTER[index % len(RATIO_JITTER)],
            NEW_ERA_MIXES[index % len(NEW_ERA_MIXES)])
    store.entry(dt.datetime.combine(first + dt.timedelta(days=days),
                                    dt.time(0, 30), tzinfo=UTC),
                model="claude-opus-5-5", fresh=1000, path="/p/tail.jsonl")
    store.close()


#: Scenario 12's one unchanged `claude-sonnet-5` mix, with non-fresh classes.
BOUNDARY_MIX = {"fresh": 1000, "output": 500, "cache_read": 20_000}


def scenario_era_boundary_no_change(home: Path) -> None:
    """A history spanning 2026-09-22 with no metering-rate change.

    Clock 2026-09-28T06:00Z; complete days 2026-08-25..09-27. The token mix
    never changes, and each day's meter moves by its units under ITS OWN
    era's weights at one constant budget, so the provider's rate is
    constant. Measured in Opus-5-weighted units per point, the post-boundary
    days sit about 2.1x higher: a step a cross-era fit would read as a rate
    change. The current subscription week is anchored 2026-09-21T12:00Z and
    so crosses the boundary; six complete post-boundary days (09-22..27)
    support the in-era fit.

    Expected: `analysisStart` 2026-09-22; a verdict other than
    `rate-change-detected`; the current week's consumption, headroom and
    projection withheld as `unvalidated-coefficient-era` while its observed
    reading is published.
    """
    store = Store(home)
    first = dt.date(2026, 8, 25)
    last = dt.date(2026, 9, 27)
    boundary = dt.date(2026, 9, 22)
    anchor = dt.datetime(2026, 9, 21, 12, tzinfo=UTC)
    running: dict = {}
    days = (last - first).days + 1
    for index in range(days):
        date = first + dt.timedelta(days=index)
        week = anchor + dt.timedelta(
            days=7 * ((date - anchor.date()).days // 7))
        step = STEPS[index % len(STEPS)]
        opening = running.get(week, 2)
        running[week] = opening + step
        # Readings after the 12:00 anchor, so no day is split across weeks.
        for hour, pct in ((13, opening), (23, opening + step)):
            store.snapshot(dt.datetime.combine(date, dt.time(hour),
                                               tzinfo=UTC), week, pct)
        weights = NEW_ERA_WEIGHTS if date >= boundary else OPUS5_WEIGHTS
        per_mix = sum(weights[c] * n for c, n in BOUNDARY_MIX.items())
        factor = (step * BUDGET * RATIO_JITTER[index % len(RATIO_JITTER)]
                  / per_mix)
        store.entry(dt.datetime.combine(date, dt.time(18), tzinfo=UTC),
                    model="claude-sonnet-5",
                    fresh=round(BOUNDARY_MIX["fresh"] * factor),
                    output=round(BOUNDARY_MIX["output"] * factor),
                    cache_read=round(BOUNDARY_MIX["cache_read"] * factor),
                    path="/p/boundary.jsonl")
    store.entry(dt.datetime(2026, 9, 28, 0, 30, tzinfo=UTC),
                model="claude-sonnet-5", fresh=1000, path="/p/tail.jsonl")
    store.close()


SCENARIOS = {
    "01-steady-no-change": scenario_steady,
    "02-rate-change-confirmed": scenario_rate_change,
    "03-credit-only": scenario_credit_only,
    "04-sparse-day-in-run": scenario_sparse_in_run,
    "05-unsupported-mix": scenario_unsupported_mix,
    "06-precedence-all-three": scenario_precedence,
    "07-recorded-prior-diverges": scenario_recorded_prior,
    "08-two-accounts": scenario_two_accounts,
    "09-offset-credit-and-fast": scenario_offset_credit_and_fast,
    "10-malformed-retained-instant": scenario_malformed_retained_instant,
    "11-opus55-steady-mixed-families": scenario_opus55_steady_mixed_families,
    "12-era-boundary-no-change": scenario_era_boundary_no_change,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Override the output directory. Defaults to the in-tree "
             "tests/fixtures/quota/. bin/cctally-quota-test passes a per-run "
             "scratch dir so the in-tree fixtures stay byte-stable.")
    parser.add_argument("--scenario", action="append", default=None,
                        help="Build only the named scenario (repeatable).")
    args = parser.parse_args(argv)
    out_root = args.out if args.out is not None else FIXTURES_DIR
    for name in (args.scenario or list(SCENARIOS)):
        home = out_root / name
        home.mkdir(parents=True, exist_ok=True)
        SCENARIOS[name](home)
        print(f"built: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
