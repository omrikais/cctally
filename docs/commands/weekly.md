# `weekly`

Claude usage grouped by **subscription week** (anchored to `--resets-at`),
with `Used %` and `$/1%` columns daily/monthly don't have.

> **Cost coverage:** Claude dollar and token totals are
> [transcript-derived lower bounds](../claude-cost-coverage.md), not exact
> `/usage` billing totals.

> Canonical form: [`cctally claude weekly`](claude.md) (this flat form remains as an alias).

## Synopsis

```
cctally weekly
    [-s YYYYMMDD] [-u YYYYMMDD]
    [-b] [-o {asc,desc}]
    [-m {auto,calculate,display}]
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
| `-m, --mode {auto,calculate,display}` | Cost source (drop-in for `ccusage weekly --mode`). `auto` (default) uses the recorded `costUSD` from JSONL when present, else computes from embedded pricing — this is the pre-Session-C behavior. `calculate` always computes from embedded pricing, ignoring any recorded `costUSD`. `display` shows the recorded `costUSD` only, rendering `$0.00` when a row has none (ccusage-faithful). Most modern Claude Code JSONL omits `costUSD`, so under `display` near-everything reports `$0`. The pre-snapshot extrapolation tail carries no entries, so its weeks contribute `$0` regardless of mode. |
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

## A credited week is TWO rows

An Anthropic quota reset never moves a week's boundaries, but it does end one billing cycle and begin another inside that week. A week that was credited in place therefore renders as **two rows** — the segment before the credit and the segment after it — each with its own interval, its own cost and its own `Used %`. This is the same pair [`report`](report.md) has rendered since v1.7.2, and the two commands now agree on it.

The two rows share one `week` value, because `week` is the billing-cycle join key into `weekly_usage_snapshots.week_start_date`, which the credit does not move. What distinguishes them is `weekStartAt`, so **a row's identity in `--json` is the pair `(week, weekStartAt)`, not `week` alone**:

```json
{ "week": "2026-06-05", "displayWeek": "2026-06-05",
  "weekStartAt": "2026-06-05T15:00:00Z", "weekEndAt": "2026-06-10T09:00:00+00:00",
  "usedPct": 71.0, "totalCost": 6.0 },
{ "week": "2026-06-05", "displayWeek": "2026-06-10",
  "weekStartAt": "2026-06-10T09:00:00+00:00", "weekEndAt": "2026-06-12T15:00:00Z",
  "usedPct": 12.0, "totalCost": 3.0 }
```

`displayWeek` differs between the two rows: it is each segment's own user-facing start date, and it is what the terminal table's `Week` column renders.

**Scripting warning.** `{r["week"]: r for r in payload["weekly"]}` silently drops the pre-credit row and under-reports that week's spend by the whole pre-credit segment. Key on `(r["week"], r["weekStartAt"])`, or on `weekStartAt` alone.

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
