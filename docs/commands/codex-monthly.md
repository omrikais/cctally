# `codex-monthly`

Codex (OpenAI) usage grouped by calendar month. Drop-in replacement for
upstream [`ccusage-codex`](../../README.md#acknowledgments) `monthly`,
offline.

## Synopsis

```
cctally codex-monthly
    [-s YYYY-MM-DD] [-u YYYY-MM-DD]
    [-b] [-o {asc,desc}]
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

## Notes

Same dedup, token-semantics, and unknown-model behavior as
[`codex-daily`](codex-daily.md#notes--diverges-from-upstream-ccusage-codex-on-duplicate-events) —
read that page for the details. The `--debug` report shape is also
shared — see
[Pricing debug report](codex-daily.md#pricing-debug-report---debug).

## See also

- [`codex-daily`](codex-daily.md), [`codex-weekly`](codex-weekly.md), [`codex-session`](codex-session.md)
