# `monthly`

Claude usage grouped by calendar month. Drop-in replacement for
`ccusage monthly`, offline.

> Canonical form: [`cctally claude monthly`](claude.md) (this flat form remains as an alias).

## Synopsis

```
cctally monthly
    [-s YYYYMMDD] [-u YYYYMMDD]
    [-b] [-o {asc,desc}]
    [-m {auto,calculate,display}]
    [--json]
```

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYYMMDD` | Filter from date (inclusive). |
| `-u, --until YYYYMMDD` | Filter until date (inclusive). |
| `-b, --breakdown` | Show per-model cost breakdown sub-rows. |
| `-o, --order {asc,desc}` | Sort direction by month (default `asc`). |
| `-m, --mode {auto,calculate,display}` | Cost source (drop-in for `ccusage monthly --mode`). `auto` (default) uses the recorded `costUSD` from JSONL when present, else computes from embedded pricing — this is the pre-Session-C behavior. `calculate` always computes from embedded pricing, ignoring any recorded `costUSD`. `display` shows the recorded `costUSD` only, rendering `$0.00` when a row has none (ccusage-faithful). Most modern Claude Code JSONL omits `costUSD`, so under `display` near-everything reports `$0`. |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | Output JSON matching `ccusage monthly` format. |

## Examples

```bash
cctally monthly --since 20260101
cctally monthly --since 20260101 --until 20260331
cctally monthly --since 20260101 --breakdown
cctally monthly --since 20260101 --json
cctally monthly --order desc
```

## Notes

- Calendar months — UTC-bucketed. If you live in a non-UTC zone and want
  local-month bucketing, post-process the JSON.
- Cost recomputes on every read (same semantics as [`daily`](daily.md)).

## See also

- [`daily`](daily.md), [`weekly`](weekly.md) — finer buckets
- [`codex-monthly`](codex-monthly.md) — Codex equivalent


## Shareable output

`cctally monthly` accepts `--format {md,html,svg}` and related flags for shareable artifacts. See [share.md](share.md) for the full flag reference.
