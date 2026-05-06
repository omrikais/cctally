# `blocks`

Claude usage grouped by 5-hour session block. Drop-in replacement for
`ccusage blocks`, offline.

## Synopsis

```
cctally blocks
    [-s YYYYMMDD] [-u YYYYMMDD]
    [-b] [--json]
```

## Purpose

5-hour blocks are how Anthropic meters short-window usage; this view
makes them visible without round-tripping to `ccusage`'s network calls.

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYYMMDD` | Filter from date (inclusive). |
| `-u, --until YYYYMMDD` | Filter until date (inclusive). |
| `-b, --breakdown` | Show per-model cost breakdown. |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). Note: `--since`/`--until` keep upstream-parity host-local parsing (documented divergence). |
| `--json` | Output JSON matching `ccusage blocks` format. |

## Examples

```bash
cctally blocks --since 20260414
cctally blocks --since 20260410 --until 20260416
cctally blocks --since 20260414 --breakdown
cctally blocks --since 20260414 --json
```

## Notes

- Each 5-hour window is anchored to Anthropic's real
  `rate_limits.5h.resets_at` value (recorded by `record-usage` into
  `weekly_usage_snapshots`) when a snapshot exists for that window.
- When no recorded snapshot covers a window, the start time falls back
  to the heuristic `floor(first_cc_entry_in_window) + 5h`. Heuristic
  rows are flagged with a `~` prefix on the start time, and a footer
  legend (`~ = approximate start...`) appears at the bottom of the
  table when any heuristic rows are present. The bucket may drift
  from the real Anthropic boundary by up to ~5h on heuristic rows
  (e.g., a window initiated via claude.ai web or the API while
  `record-usage` wasn't running).
- `--breakdown` adds child rows per model under each parent block.
- `--json` output is a superset of upstream `ccusage blocks --json`:
  non-gap blocks include an additive `"anchor": "recorded" | "heuristic"`
  field indicating which path produced the start time. Gap blocks omit
  the field. All other keys match upstream.

## See also

- [`daily`](daily.md) — same data, calendar-day buckets
- [`five-hour-blocks`](five-hour-blocks.md) — API-anchored analytics view (no heuristic rows)
- [`record-usage`](record-usage.md) — supplies the anchor that defines block boundaries
