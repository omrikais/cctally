# `cache-report`

Cache-hit %, cost-saved-vs-baseline, write-premium tracking, and anomaly
triggers across days or sessions. The most feature-rich command in the
suite.

## Synopsis

```
cctally cache-report
    [--days DAYS] [--since SINCE] [--until UNTIL]
    [--by-session]
    [--offline] [--project PROJECT] [--json]
    [--anomaly-threshold-pp PP] [--anomaly-window-days N] [--no-anomaly]
    [--sort {date,net,cache,recent,cost,anomaly}]
```

## Purpose

Surface caching behavior so regressions (e.g. a Claude Code update that
silently disables prompt caching for a model) become visible in days
rather than dollars.

## What it shows

- **Cache %** = `cache_read_tokens / (input + cache_create + cache_read)`
- **$ Saved** — counterfactual: what cost would have been with no caching, minus actual
- **$ Wasted** — premium paid for cache writes that didn't yield enough reads
- **Net $** — `Saved – Wasted`; negative means the caching is costing you
- **Anomaly glyph (⚠)** — fires when `Net $ < 0` or `Cache % drops ≥ 15pp` vs. the trailing 14-day median
- Per-model breakdown rows under each parent row (always shown)

## Options

| Flag | Description |
| --- | --- |
| `--days N` | Recent days to include (default `7`). |
| `--since` / `--until` | ISO 8601 window bounds. Override `--days` when set. |
| `--by-session` | Group by Claude `sessionId` (resume-merged) instead of by date. Adds SessionId / Last Activity / Project columns. |
| `--offline` | No-op (pricing always embedded). |
| `--project PROJECT` | Filter to a specific project. |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | Machine-readable JSON. |
| `--anomaly-threshold-pp PP` | Cache% drop threshold for the `cache_drop` trigger. Default `15`. |
| `--anomaly-window-days N` | Trailing window (days) for baseline median. Default `14`. |
| `--no-anomaly` | Disable both `cache_drop` and `net_negative` triggers. |
| `--sort` | Override sort order. Defaults: `date` in daily mode, `net` in `--by-session`. Options: `date`, `net`, `cache`, `recent`, `cost`, `anomaly`. |

## Examples

```bash
cctally cache-report
cctally cache-report --days 14
cctally cache-report --since 2026-04-10 --until 2026-04-18
cctally cache-report --by-session --days 14
cctally cache-report --by-session --sort cache
cctally cache-report --json
```

## Gotchas

- **Anomaly baseline silent-skips when samples are thin.** The
  `cache_drop` trigger needs ≥5 daily rows or ≥10 session rows in the
  trailing `--anomaly-window-days` window. With fewer samples, the trigger
  is silently skipped (no warning) — looks like a missed regression but
  is the correct behavior to avoid first-two-weeks false positives.
  Widen `--days` or inspect `--days N --json | jq '.days[].anomaly.reasons'`.
- `--since` / `--until` accept either pure-date (`2026-04-10`) or
  full-ISO (`2026-04-10T10:00:00Z`). Mixed-format same-day windows
  collapse to empty (e.g. `--since 20260418 --until 2026-04-18`) — fix:
  use the same format on both ends.
- `--by-session` collapses Claude `--resume` chains into one row using
  the `session_files.session_id` mapping.
- Per-model child rows are always rendered. There is no flag to suppress
  them.

## See also

- [`cache-sync`](cache-sync.md) — prime the cache this command queries
- [Architecture · cache.db](../architecture.md#the-session-entry-cache-cachedb)
