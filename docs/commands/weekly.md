# `weekly`

Claude usage grouped by **subscription week** (anchored to `--resets-at`),
with `Used %` and `$/1%` columns daily/monthly don't have.

## Synopsis

```
cctally weekly
    [-s YYYYMMDD] [-u YYYYMMDD]
    [-b] [-o {asc,desc}]
    [--json]
```

## Purpose

The "what did each subscription week cost me" view. Unlike calendar-week
groupings, the boundaries here align with Anthropic's actual quota window
so `Used %` and `$/1%` are directly meaningful.

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYYMMDD` | Filter from date (inclusive). |
| `-u, --until YYYYMMDD` | Filter until date (inclusive). |
| `-b, --breakdown` | Show per-model cost breakdown sub-rows. |
| `-o, --order {asc,desc}` | Sort direction by week (default `asc`). |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | Output JSON. |

## Examples

```bash
cctally weekly
cctally weekly --since 20260101
cctally weekly --breakdown
cctally weekly --json
cctally weekly --order desc
```

## How week boundaries are picked

1. For weeks where `weekly_usage_snapshots` has rows, that week's exact
   `week_start_at` is used.
2. For weeks **before** the earliest snapshot, boundaries are
   extrapolated by 7-day multiples back from the earliest known anchor.
3. If no snapshots exist at all, falls back to `config.json`
   `collector.week_start` (default `monday`).

## Gotchas

- **`weekly` ignores `weekly_cost_snapshots` for cost.** Cost is always
  recomputed from `cache.db` so pricing-dict edits take effect on the
  next read. If you want "cost as it was when the snapshot was taken,"
  use [`report`](report.md) instead.
- **Phantom weeks.** When fallback logic synthesizes a week boundary
  for an interval that has no `weekly_usage_snapshots` and no
  `session_entries` rows, you'll see a row with zero usage and zero
  cost. This is by design (so your trend doesn't have date gaps) but
  can look like a bug.

## See also

- [`daily`](daily.md), [`monthly`](monthly.md) — calendar-aligned buckets
- [`report`](report.md) — same `$/1%` metric, snapshot-based instead of recomputed
- [Architecture · week boundaries](../architecture.md#week-boundaries)


## Shareable output

`cctally weekly` accepts `--format {md,html,svg}` and related flags for shareable artifacts. See [share.md](share.md) for the full flag reference.
