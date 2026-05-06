# `report`

Trend table of dollars per 1% weekly usage. The headline command of this
project.

## Synopsis

```
cctally report
    [--weeks N] [--sync-current]
    [--week-start-name {monday,…,sunday}]
    [--mode {auto,calculate,display}]
    [--offline] [--project PROJECT]
    [--json] [--detail]
```

## Purpose

For each recent subscription week, join the latest usage % snapshot with
the latest cost snapshot, divide cost by percent, and render a trend.
This is the metric that surfaces quota-rule changes (or your own usage
shifts) early.

## Options

| Flag | Description |
| --- | --- |
| `--weeks N` | How many recent week windows to include (default `8`). |
| `--sync-current` | Run `sync-week` first, then report. |
| `--week-start-name` | Week-start day used if report falls back to date-only logic. |
| `--mode {auto,calculate,display}` | Mode passed to `sync-week` when `--sync-current` is set. |
| `--offline` | Forwarded to `sync-week` when `--sync-current` is set. |
| `--project PROJECT` | Forwarded to `sync-week` when `--sync-current` is set. |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | Machine-readable JSON output. |
| `--detail` | Include per-percent cost milestones for the current week. |

## Examples

```bash
cctally report
cctally report --sync-current
cctally report --weeks 12 --json
cctally report --sync-current --detail
```

The `cctally-dollar-per-percent` wrapper exists for the muscle-memory
shortcut — it's exactly `report --sync-current "$@"`.

## Notes

- Joining uses `WeekRef`: prefers exact `week_start_at` (ISO timestamp),
  falls back to `week_start_date` (date-only) for older snapshots that
  predate the hour-accurate column.
- `$/1%` is meaningful only when both usage % and cost are non-zero. Weeks
  with one but not the other render as `—` for the joined column.
- `--detail` adds the `percent-breakdown` view for the *current* week
  only; for an older week, call `percent-breakdown --week-start <date>`
  directly.

## See also

- [`sync-week`](sync-week.md) — produces the cost half of the join
- [`record-usage`](record-usage.md) — produces the usage half of the join
- [`percent-breakdown`](percent-breakdown.md) — same milestones `--detail` shows
- [`cctally-dollar-per-percent`](../../bin/cctally-dollar-per-percent) — wrapper
