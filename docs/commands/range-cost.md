# `range-cost`

Compute USD cost for an arbitrary absolute time range from Claude, Codex, or
both providers' local session data.

## Synopsis

```
cctally range-cost
    -s START [-e END]
    [-m {auto,calculate,display}]
    [-p PROJECT] [-b]
    [--source {claude,codex,all}] [--speed {auto,standard,fast}]
    [--json] [--total-only]
```

## Purpose

Ad-hoc cost question: "what did I spend between X and Y?". Useful for an
absolute window that does not align to a calendar day, week, or month.

## Options

| Flag | Description |
| --- | --- |
| `-s, --start START` | Start timestamp (ISO 8601, with timezone offset). **Required.** |
| `-e, --end END` | End timestamp (ISO 8601, default: now). |
| `-m, --mode {auto,calculate,display}` | Claude cost calculation mode. Default `auto`; non-default values are rejected for direct Codex requests. |
| `-p, --project PROJECT` | Filter to a source-native project. |
| `-b, --breakdown` | Show per-model usage and cost breakdown. |
| `--json` | Machine-readable JSON. |
| `--total-only` | Print numeric USD total only — useful for shell math. |
| `--source {claude,codex,all}` | Analytics provider; default `claude`. `cctally claude range-cost` and `cctally codex range-cost` are fixed-source forms and do not expose this flag. |
| `--speed {auto,standard,fast}` | Codex pricing tier; default `auto`. Applies to Codex/all and a non-default value is rejected for Claude-only. |

## Examples

```bash
cctally range-cost -s "2026-04-10T10:00:00+03:00"
cctally range-cost \
    -s "2026-04-10T10:00:00Z" -e "2026-04-12T10:00:00Z" --breakdown
cctally range-cost -s "2026-04-10T10:00:00Z" --json
cctally range-cost -s "2026-04-10T10:00:00Z" --total-only
cctally codex range-cost -s "2026-07-14T00:00:00Z" -e "2026-07-15T00:00:00Z" --breakdown
cctally range-cost --source all -s "2026-07-14T00:00:00Z" -e "2026-07-15T00:00:00Z" --total-only
```

## Notes

- Unlike `daily` / `weekly` / `monthly`, this command takes ISO 8601
  timestamps with timezone offset (or `Z`), **not** date-only strings.
  This is intentional — a range query needs sub-day precision.
- Reads through `cache.db` like every other JSONL-derived command.
- `--total-only` emits a single bare number suitable for `$(...)`
  capture; `--json` emits the full structured payload.
- Codex always calculates embedded, speed-aware pricing. Its `inputTokens`
  include cached input, `outputTokens` include reasoning output, and its JSON
  names `cachedInputTokens` / `nonCachedInputTokens`; it never invents Claude
  cache-create/read fields. `--mode` is rejected when non-default for a
  Codex-only request, and remains a Claude-leg option for `--source all`.
- The range is inclusive at both endpoints for every provider. In `--source
  all`, `--total-only` is allowed and prints the one compatible physical USD
  sum. Normal all-source output keeps Claude then Codex sections separate.
- A direct Codex `--project` filter requires qualified metadata. An unavailable
  join emits the source/status JSON envelope, writes no human report, and exits
  3. An all-source request still renders its Claude block and an unavailable
  Codex block, also with exit 3.

## See also

- [`daily`](daily.md) / [`weekly`](weekly.md) — calendar-aligned views
- [`sync-week`](sync-week.md) — persists weekly cost snapshots
