# `sync-week`

Compute the USD cost for a subscription week and store it in
`weekly_cost_snapshots`.

## Synopsis

```
cctally sync-week
    [--week-start YYYY-MM-DD] [--week-end YYYY-MM-DD]
    [--week-start-name {monday,…,sunday}]
    [--mode {auto,calculate,display}]
    [--offline] [--project PROJECT]
    [--json] [--quiet]
```

## Purpose

Walk Claude session JSONLs in the chosen week window, compute total USD
cost from `CLAUDE_MODEL_PRICING`, and persist a snapshot. `report` reads
these snapshots to compute `$/1%`.

## Week selection priority

1. Explicit `--week-start` / `--week-end` (date based)
2. Latest `weekly_usage_snapshots.week_start_at` / `week_end_at`
   (hour-accurate)
3. Current week from configured week-start rule (`--week-start-name` →
   `config.json` → `monday`)

## Options

| Flag | Description |
| --- | --- |
| `--week-start YYYY-MM-DD` | Explicit week start date. If `--week-end` is omitted, uses start + 6 days. |
| `--week-end YYYY-MM-DD` | Explicit inclusive week end (for custom windows). |
| `--week-start-name` | Week-start day used when explicit/custom boundaries are unavailable. |
| `--mode {auto,calculate,display}` | Cost calculation mode (default `auto`). |
| `--offline` | No-op; pricing data is always embedded. |
| `--project PROJECT` | Filter cost calc to a single Claude project (cwd). |
| `--json` | Machine-readable JSON output. |
| `--quiet` | Suppress human-readable output (no effect with `--json`). |

## Examples

```bash
cctally sync-week
cctally sync-week --week-start 2026-02-05 --week-end 2026-02-12
cctally sync-week --mode calculate --offline --json
```

## Notes

- `sync-week` is **idempotent per invocation** — calling it twice in a row
  writes two snapshot rows for the same week. `report` reads the latest
  per-week row.
- `--mode display` reuses the value computed at last `--mode calculate`
  rather than recomputing from JSONLs. Use it as a low-cost refresh.
- Cost is only stored here; if you change `CLAUDE_MODEL_PRICING`, re-run
  `sync-week` to pick up the new rates in `weekly_cost_snapshots`. (Other
  commands like `daily` / `weekly` recompute on every read and don't need
  this.)

## See also

- [`report`](report.md) — joins this with usage to compute `$/1%`
- [`weekly`](weekly.md) — read-side weekly view that recomputes cost on the fly
- [`cctally-sync-week`](../../bin/cctally-sync-week) — bash wrapper, identical args
