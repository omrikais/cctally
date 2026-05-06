# `range-cost`

Compute USD cost for an arbitrary time range from Claude session JSONLs.

## Synopsis

```
cctally range-cost
    -s START [-e END]
    [-m {auto,calculate,display}]
    [-p PROJECT] [-b]
    [--json] [--total-only]
```

## Purpose

Ad-hoc cost question: "what did I spend between X and Y?". Useful for
custom windows that don't align to a calendar day, week, or month.

## Options

| Flag | Description |
| --- | --- |
| `-s, --start START` | Start timestamp (ISO 8601, with timezone offset). **Required.** |
| `-e, --end END` | End timestamp (ISO 8601, default: now). |
| `-m, --mode {auto,calculate,display}` | Cost calculation mode. Default `auto`. |
| `-p, --project PROJECT` | Filter to a specific project (cwd). |
| `-b, --breakdown` | Show per-model usage and cost breakdown. |
| `--json` | Machine-readable JSON. |
| `--total-only` | Print numeric USD total only — useful for shell math. |

## Examples

```bash
cctally range-cost -s "2026-04-10T10:00:00+03:00"
cctally range-cost \
    -s "2026-04-10T10:00:00Z" -e "2026-04-12T10:00:00Z" --breakdown
cctally range-cost -s "2026-04-10T10:00:00Z" --json
cctally range-cost -s "2026-04-10T10:00:00Z" --total-only
```

## Notes

- Unlike `daily` / `weekly` / `monthly`, this command takes ISO 8601
  timestamps with timezone offset (or `Z`), **not** date-only strings.
  This is intentional — a range query needs sub-day precision.
- Reads through `cache.db` like every other JSONL-derived command.
- `--total-only` emits a single bare number suitable for `$(...)`
  capture; `--json` emits the full structured payload.

## See also

- [`daily`](daily.md) / [`weekly`](weekly.md) — calendar-aligned views
- [`sync-week`](sync-week.md) — persists weekly cost snapshots
