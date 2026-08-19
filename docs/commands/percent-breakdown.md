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
- `report --detail` calls into the same renderer for the *current* week.
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

## See also

- [`record-usage`](record-usage.md) — writes the milestones rendered here
- [`report`](report.md) — `--detail` includes this view inline
- [`cctally codex percent-breakdown`](codex-percent-breakdown.md) — the Codex
  native seven-day equivalent with the same terminal design
