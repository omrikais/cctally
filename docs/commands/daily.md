# `daily`

Claude usage grouped by date. Drop-in replacement for `ccusage daily`,
offline.

> Canonical form: [`cctally claude daily`](claude.md) (this flat form remains as an alias).

## Synopsis

```
cctally daily
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
| `-o, --order {asc,desc}` | Sort direction by date (default `asc`). |
| `-m, --mode {auto,calculate,display}` | Cost source (drop-in for `ccusage daily --mode`). `auto` (default) uses the recorded `costUSD` from JSONL when present, else computes from embedded pricing — this is the pre-Session-C behavior. `calculate` always computes from embedded pricing, ignoring any recorded `costUSD`. `display` shows the recorded `costUSD` only, rendering `$0.00` when a row has none (ccusage-faithful). Most modern Claude Code JSONL omits `costUSD`, so under `display` near-everything reports `$0`. |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | Output JSON matching `ccusage daily` format. |

## Examples

```bash
cctally daily --since 20260414
cctally daily --since 20260410 --until 20260416
cctally daily --since 20260414 --breakdown
cctally daily --since 20260414 --json
cctally daily --order desc
```

## Notes

- Cost is recomputed on every read from `CLAUDE_MODEL_PRICING` against
  `cache.db`'s `session_entries` rows. Pricing-dict updates take effect
  immediately with no `cache-sync` needed.
- JSON output matches upstream `ccusage daily --json` shape for scripting
  parity.
- Date arguments accept `YYYYMMDD` (no separator).

## See also

- [`monthly`](monthly.md), [`weekly`](weekly.md) — coarser buckets, same data
- [`blocks`](blocks.md) — finer buckets (5-hour windows)
- [`session`](session.md) — group by `sessionId` instead of by date


## Shareable output

`cctally daily` accepts `--format {md,html,svg}` and related flags for shareable artifacts. See [share.md](share.md) for the full flag reference.
