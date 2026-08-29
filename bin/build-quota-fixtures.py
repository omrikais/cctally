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
              path="/p/a.jsonl"):
        # Direct SQL rather than `seed_session_entry`, which carries no
        # `cache_create_1h_tokens` parameter. That column is the one the
        # kernel withholds a day over when it is NULL beside a positive
        # cache-write total, so a fixture must be able to write it.
        self.cache.execute(
            "INSERT INTO session_entries (source_path, line_offset,"
            " timestamp_utc, model, input_tokens, output_tokens,"
            " cache_create_tokens, cache_read_tokens, cache_create_1h_tokens,"
            " account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (path, self.offset, fixture_timestamp_utc(at), model, int(fresh),
             int(output), int(cache_create), int(cache_read), cache_1h,
             account_key))
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


SCENARIOS = {
    "01-steady-no-change": scenario_steady,
    "02-rate-change-confirmed": scenario_rate_change,
    "03-credit-only": scenario_credit_only,
    "04-sparse-day-in-run": scenario_sparse_in_run,
    "05-unsupported-mix": scenario_unsupported_mix,
    "06-precedence-all-three": scenario_precedence,
    "07-recorded-prior-diverges": scenario_recorded_prior,
    "08-two-accounts": scenario_two_accounts,
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
