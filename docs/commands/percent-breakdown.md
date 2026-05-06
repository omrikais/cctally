# `percent-breakdown`

Per-percent cumulative and marginal cost milestones for a single week.

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

## See also

- [`record-usage`](record-usage.md) — writes the milestones rendered here
- [`report`](report.md) — `--detail` includes this view inline
