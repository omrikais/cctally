# `percent-breakdown`

Per-percent cumulative and marginal cost milestones for a single week.

> **Cost coverage:** Stored Claude milestone costs are
> [transcript-derived lower bounds](../claude-cost-coverage.md), not exact
> `/usage` billing totals.

## Synopsis

```
cctally percent-breakdown
    [--week-start YYYY-MM-DD]
    [--week-start-name {monday,…,sunday}]
    [--json]
```

## Purpose

When `record-usage` sees a snapshot crossing an integer percent threshold,
it writes a row to `percent_milestones`. This command renders those rows
for a chosen week so you can see exactly when each percent was reached and
what it cost incrementally.

## Options

| Flag | Description |
| --- | --- |
| `--week-start YYYY-MM-DD` | Week start date. Defaults to the current week. |
| `--week-start-name` | Week-start day used when no explicit date or usage data is available. |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | Machine-readable JSON output. |

## Examples

```bash
cctally percent-breakdown
cctally percent-breakdown --week-start 2026-03-20
cctally percent-breakdown --json
```

## Notes

- Output includes the 5-hour percent at the moment of each crossing
  (added by A1 — the `five_hour_percent_at_crossing` column on
  `percent_milestones`). Useful for correlating big cost jumps with
  short-window usage spikes.
- Only milestones recorded by `record-usage` show up — if your status
  line wasn't running for part of the week, you'll see gaps, and they
  cannot be retroactively reconstructed.
- `report --detail` renders the same milestones for the *current* week in its
  own table, with a narrower column set and no 5-hour column. Both tables
  disclose an observation gap the same way.
- **After an in-place weekly credit, this view has a gap it cannot fill
  (issue #213).** A partial credit lowers the reported weekly percentage
  without re-anchoring the week, and `percent_milestones` is forward-only: no
  new row is recorded until usage climbs back above the pre-credit peak. So
  every percent between the post-credit level and that peak is missing here,
  and so is the spend attributed to it. The rows that are present are correct;
  the ones that are absent were never recorded, and no command reconstructs
  them. Anything built on these rows — the alert next step that offers
  `percent-breakdown`, `report --detail`, and the dashboard's per-percent
  view — inherits the same gap. `cctally record-credit` documents the credit
  model itself.

## A back-filled run says so

When observation stops for long enough that the next reading crosses several
integer percents at once, one snapshot records all of them. The whole
accumulated cost lands on the first threshold of the run and every threshold
after it has no separable marginal cost of its own — those crossings were
never observed apart from each other.

The run is named once above the table, giving its threshold range, the instant
of the single observation that recorded it, and how long it had been since the
previous crossing:

```
Observation gap: 19%-35% were all recorded from one observation at 2026-09-04 04:03 UTC, 17.4 hours after the previous crossing. Their marginal costs are not separable (observation_gap).
```

Inside the table those rows print `observation_gap` in the `Marginal Cost`
column instead of a bare `n/a`, which read as "this crossing cost nothing
measurable". The run's first row keeps its real marginal cost, and a week with
several runs gets one line per run.

A bare `n/a` still appears, and still means what it always did: a threshold
whose marginal cost is unknown because no earlier milestone exists to subtract
from. That is the ordinary shape of the first crossing of a week or of a
post-credit cycle.

In `--json`, `marginalCostUSD` stays a number or `null` exactly as before, and
a classified row gains a sibling `marginalCostWithheldCause` whose value is
`observation_gap`. The key is present only on a classified row, `schemaVersion`
is unchanged, and a consumer that ignores unknown keys needs no change. The
JSON does not expose the snapshot identity, so a consumer reconstructs a run
from consecutive rows that share `capturedAt` and carry the cause.

## See also

- [`record-usage`](record-usage.md) — writes the milestones rendered here
- [`report`](report.md) — `--detail` includes this view inline
- [`cctally codex percent-breakdown`](codex-percent-breakdown.md) — the Codex
  native seven-day equivalent with the same terminal design
