# `codex-weekly`

Codex (OpenAI) usage grouped by week (week-start day from `config.json`).

> Not a drop-in for upstream [`ccusage-codex`](../../README.md#acknowledgments)
> — upstream has no `codex weekly` command. This is an addition.

> Canonical form: [`cctally codex weekly`](codex.md) (this flat form remains as an alias).

## Synopsis

```
cctally codex-weekly
    [-s YYYY-MM-DD] [-u YYYY-MM-DD]
    [-b] [-o {asc,desc}]
    [--speed {auto,standard,fast}]
    [--json]
    [-z TZ] [-l LOCALE]
    [--compact] [--color] [--noColor]
    [-O | --offline | --no-offline]
    [-d | --debug] [--debug-samples N]
```

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYY-MM-DD` | Filter from date (inclusive). |
| `-u, --until YYYY-MM-DD` | Filter until date (inclusive). |
| `-b, --breakdown` | Show per-model cost breakdown sub-rows. |
| `-o, --order {asc,desc}` | Sort direction by week (default `asc`). |
| `--speed {auto,standard,fast}` | Codex pricing tier. `auto` (default) reads `service_tier` from `~/.codex/config.toml`; `fast`\|`priority` there selects fast-tier pricing. `fast`/`standard` force the tier. |
| `--json` | Output JSON. |
| `-z, --timezone TZ` | IANA timezone for date bucketing. |
| `-l, --locale LOCALE` | No-op; accepted for drop-in compat. |
| `--compact` | Force compact table layout. |
| `--color` / `--noColor` | No-op; accepted for drop-in compat. |
| `-O, --offline / --no-offline` | No-op; always offline. |
| `-d, --debug` | Emit a stderr "Codex Pricing Debug Report" (totals + top-N highest computed-cost entries). |
| `--debug-samples N` | Cap on top-entry sample rows (default 5; `N=0` suppresses the block). |

## Examples

```bash
cctally codex-weekly
cctally codex-weekly --since 20260301
cctally codex-weekly --breakdown
cctally codex-weekly --json
cctally codex-weekly --order desc
```

## Pricing tier (`--speed`)

`--speed` selects the Codex cost tier, matching `ccusage codex --speed`:

- `auto` (default) — scans `~/.codex/config.toml`; if any `service_tier = "fast"` or `service_tier = "priority"` line is present, fast-tier pricing applies, otherwise standard.
- `fast` — force fast-tier pricing.
- `standard` — force base pricing.

Fast-tier multiplies the per-model cost by a fixed factor: `gpt-5.5` ×2.5, all
other Codex models ×2.0. Detection is a line-scan (a `service_tier` line in any
table counts). `--json` gains no new field — only the `costUSD` figures reflect
the tier.

> `--speed` is a cctally extension on the flat `codex-*` form — the standalone
> `ccusage-codex` binary has no `--speed`. The canonical `cctally codex <cmd>`
> subgroup mirrors `ccusage codex <cmd>`, which does.

## How week boundaries are picked

Unlike Claude `weekly`, there's no Codex equivalent of the status-line
`--resets-at` anchor. Week boundaries here come straight from
`config.json → collector.week_start` (default `monday`). Set it to
match your Codex billing cycle if you have one.

## Notes

Same dedup, token-semantics, and unknown-model behavior as
[`codex-daily`](codex-daily.md#notes--diverges-from-upstream-ccusage-codex-on-duplicate-events).
The `--debug` report shape is shared too — see
[Pricing debug report](codex-daily.md#pricing-debug-report---debug).

## See also

- [`weekly`](weekly.md) — Claude equivalent (uses snapshot anchor instead of config)
- [`codex-daily`](codex-daily.md), [`codex-monthly`](codex-monthly.md), [`codex-session`](codex-session.md)
- [Configuration · week-start resolution](../configuration.md#week-start-resolution-order)
