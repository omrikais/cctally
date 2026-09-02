#!/usr/bin/env python3
"""Seed a trustworthy quota calibration into a COPIED fixture HOME.

Used only by `bin/cctally-reconcile-test`'s PROJECTED1-b leg (#661 S2 spec
section 3.3). The regime's composition centres are computed from the
fixture's OWN current-week population, so the apply adapter's support test
passes by construction rather than by a hand-guessed centre that would drift
the moment the fixture builder changed a model name.

PUBLIC. `.mirror-allowlist:97` matches it through the `bin/_lib-*` glob, and
that is correct rather than an oversight: the public `bin/cctally-reconcile-test`
shells out to this script, so a mirror that published the harness without it
would publish a test that cannot run. A `!` negation here would recreate
exactly the failure `.mirror-allowlist:199-202` already documents for the
underscore-named modules.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
from importlib.machinery import SourceFileLoader
import importlib.util

BIN_DIR = os.environ.get("PROJECTED_BIN_DIR") or str(
    pathlib.Path(__file__).resolve().parent)
sys.path.insert(0, BIN_DIR)
loader = SourceFileLoader("cctally", os.path.join(BIN_DIR, "cctally"))
spec = importlib.util.spec_from_loader("cctally", loader)
c = importlib.util.module_from_spec(spec)
sys.modules["cctally"] = c
loader.exec_module(c)

import _cctally_core  # noqa: E402
import _lib_quota_model as qm  # noqa: E402

UTC = dt.timezone.utc


def main() -> int:
    conn = c.open_db()
    try:
        now = c._command_as_of()
        fetched = c._fetch_current_week_snapshots(conn, now)
        if fetched is None:
            print("no current-week snapshot in the fixture", file=sys.stderr)
            return 1
        week_start_at, week_end_at, samples = fetched
        week_start_at, samples = c._apply_midweek_reset_override(
            conn, week_start_at, week_end_at, samples, now_utc=now)
    finally:
        conn.close()

    horizon = min(now, week_end_at)
    forecast = c._load_sibling("_cctally_forecast")
    entries = forecast._week_entry_records(
        week_start_at, horizon, account_key=None)
    aggregated = qm.aggregate_composition(entries)
    if aggregated is None:
        print("the fixture's current week has no weighted population",
              file=sys.stderr)
        return 1
    family_shares, class_shares = aggregated

    units = sum(u for u in (qm.weighted_units(e) for e in entries)
                if u is not None)
    if units <= 0:
        print("the fixture's current week weighs zero units", file=sys.stderr)
        return 1
    # Pin units-per-point so the modelled consumption is a plausible
    # mid-week figure rather than an accident of the fixture's token counts.
    units_per_point = units / 30.0

    payload = {
        "schemaVersion": 1,
        "accounts": {"*": {"regimes": [{
            "effectiveFrom": (
                week_start_at - dt.timedelta(days=30)).isoformat(),
            "effectiveUntil": None,
            "fingerprint": qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
            "algorithmRevision": qm.QUOTA_MODEL_ALGORITHM_REVISION,
            "unitsPerPoint": units_per_point,
            "interval": {"lo": units_per_point * 0.95,
                         "hi": units_per_point * 1.05},
            "support": {"days": 26, "segments": 4},
            "status": "ok",
            "asOf": week_start_at.isoformat(),
            "qualifications": [],
            "familyShares": dict(family_shares),
            "classShares": dict(class_shares),
            "familyRadius": 0.02,
            "classRadius": 0.05,
        }]}},
    }
    path = _cctally_core.APP_DIR / "quota-calibrations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"seeded {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
