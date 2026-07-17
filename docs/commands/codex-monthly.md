# `codex-monthly`

Codex (OpenAI) usage grouped by calendar month. Drop-in replacement for
upstream [`ccusage-codex`](../../README.md#acknowledgments) `monthly`,
offline.

> Canonical form: [`cctally codex monthly`](codex.md) (this flat form remains as an alias).

## Config and sharing

`codex-monthly` retains its established calendar-month accounting semantics.
`--config PATH` reads an alternate config without writing it, and the standard
share flags (`--format`, `--theme`, `--no-branding`, `--output`, `--copy`, and
`--open`) create a visibly Codex-labelled artifact. The no-`--format` terminal,
JSON, and empty bytes remain the compatibility surface. Review every artifact
before sharing; details are in [share.md](share.md).

## Synopsis

```
cctally codex-monthly
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
| `-s, --since YYYY-MM-DD` | Filter from date (inclusive; `YYYY-MM-DD` or `YYYYMMDD`). |
| `-u, --until YYYY-MM-DD` | Filter until date (inclusive). |
| `-b, --breakdown` | Show per-model cost breakdown sub-rows. |
| `-o, --order {asc,desc}` | Sort direction by month (default `asc`). |
| `--speed {auto,standard,fast}` | Codex pricing tier. `auto` (default) reads `service_tier` from `~/.codex/config.toml`; `fast`\|`priority` there selects fast-tier pricing. `fast`/`standard` force the tier. |
| `--json` | Output JSON matching `ccusage-codex monthly` format. |
| `-z, --timezone TZ` | IANA timezone for date bucketing. |
| `-l, --locale LOCALE` | No-op; accepted for drop-in compat. |
| `--compact` | Force compact table layout. |
| `--color` / `--noColor` | No-op; accepted for drop-in compat. |
| `-O, --offline / --no-offline` | No-op; always offline. |
| `-d, --debug` | Emit a stderr "Codex Pricing Debug Report" (totals + top-N highest computed-cost entries). |
| `--debug-samples N` | Cap on top-entry sample rows (default 5; `N=0` suppresses the block). |

## Examples

```bash
cctally codex-monthly --since 20260101
cctally codex-monthly --breakdown
cctally codex-monthly --json
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

## Notes

Same dedup, token-semantics, and unknown-model behavior as
[`codex-daily`](codex-daily.md#notes--diverges-from-upstream-ccusage-codex-on-duplicate-events) —
read that page for the details. The `--debug` report shape is also
shared — see
[Pricing debug report](codex-daily.md#pricing-debug-report---debug).

## See also

- [`codex-daily`](codex-daily.md), [`codex-weekly`](codex-weekly.md), [`codex-session`](codex-session.md)
