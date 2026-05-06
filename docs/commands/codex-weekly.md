# `codex-weekly`

Codex (OpenAI) usage grouped by week (week-start day from `config.json`).

> Not a drop-in for upstream [`ccusage-codex`](../../README.md#acknowledgments)
> — upstream has no `codex weekly` command. This is an addition.

## Synopsis

```
cctally codex-weekly
    [-s YYYY-MM-DD] [-u YYYY-MM-DD]
    [-b] [-o {asc,desc}]
    [--json]
    [-z TZ] [-l LOCALE]
    [--compact] [--color] [--noColor]
    [-O | --offline | --no-offline]
```

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYY-MM-DD` | Filter from date (inclusive). |
| `-u, --until YYYY-MM-DD` | Filter until date (inclusive). |
| `-b, --breakdown` | Show per-model cost breakdown sub-rows. |
| `-o, --order {asc,desc}` | Sort direction by week (default `asc`). |
| `--json` | Output JSON. |
| `-z, --timezone TZ` | IANA timezone for date bucketing. |
| `-l, --locale LOCALE` | No-op; accepted for drop-in compat. |
| `--compact` | Force compact table layout. |
| `--color` / `--noColor` | No-op; accepted for drop-in compat. |
| `-O, --offline / --no-offline` | No-op; always offline. |

## Examples

```bash
cctally codex-weekly
cctally codex-weekly --since 20260301
cctally codex-weekly --breakdown
cctally codex-weekly --json
cctally codex-weekly --order desc
```

## How week boundaries are picked

Unlike Claude `weekly`, there's no Codex equivalent of the status-line
`--resets-at` anchor. Week boundaries here come straight from
`config.json → collector.week_start` (default `monday`). Set it to
match your Codex billing cycle if you have one.

## Notes

Same dedup, token-semantics, and unknown-model behavior as
[`codex-daily`](codex-daily.md#notes--diverges-from-upstream-ccusage-codex-on-duplicate-events).

## See also

- [`weekly`](weekly.md) — Claude equivalent (uses snapshot anchor instead of config)
- [`codex-daily`](codex-daily.md), [`codex-monthly`](codex-monthly.md), [`codex-session`](codex-session.md)
- [Configuration · week-start resolution](../configuration.md#week-start-resolution-order)
