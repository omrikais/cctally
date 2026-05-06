# `five-hour-breakdown`

Per-percent cumulative + marginal cost milestones inside a single
API-anchored 5-hour block. Mirror of `percent-breakdown` for the 5h axis.

## Synopsis

```
cctally five-hour-breakdown
    [--block-start ISO8601 | --ago N]
    [--json] [--no-color] [--tz TZ]
```

## Purpose

When `record-usage` sees a 5h-window snapshot crossing an integer percent
threshold, it writes a row to `five_hour_milestones`. This command renders
those rows for one block so you can see exactly when each percent was
reached and what it cost incrementally — the same lens as
`percent-breakdown`, but scoped to a 5h window instead of a week.

## Block selector

| Flag | Behavior |
| --- | --- |
| _(none — default)_ | Current/most-recent API-anchored block (active or last-closed). |
| `--block-start <iso>` | Explicit. Accepts ISO 8601 with offset (`2026-04-30T19:30:00Z`, `2026-04-30T22:30:00+03:00`) or naive (`2026-04-30T19:30`). **Naive selectors are interpreted as UTC.** Date-only inputs (`2026-04-30`) are rejected with exit 2. |
| `--ago N` | Relative; `0` = current, `1` = previous, …. Mutually exclusive with `--block-start`. |

`--tz` affects display only; it does NOT shift selector parsing.

The selector resolves through `_canonical_5h_window_key`, so any
timestamp within the same 10-minute floor as the actual block matches
correctly.

## Options

| Flag | Description |
| --- | --- |
| `--block-start ISO8601` | Explicit selector (see above). |
| `--ago N` | Relative selector (see above). |
| `--json` | Emit camelCase JSON (`schemaVersion: 1`). |
| `--no-color` | Disable ANSI color (currently a no-op — table is plain text). |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). Note: `--block-start` selector parsing is **not** display-tz-shifted (see callout in "Block selector" above). |

## Header line

```
Block: 2026-04-30 19:30 UTC (active, 4h 06m elapsed) · 5h%: 66.7% · 7d% 62.5→66.7 (Δ +4.2pp)
```

- Closed blocks: `(closed, 5h 00m, ended HH:MM)`.
- Crossed-reset blocks: append ` ⚡ crossed weekly reset` and Δ renders as `—`.

## Default columns

| # | Threshold | Cumulative Cost | Marginal Cost | 7d at crossing |

- **#** — 1-indexed row number.
- **Threshold** — `percent_threshold` formatted `1%`, `2%`, ….
- **Cumulative Cost** — `block_cost_usd`.
- **Marginal Cost** — `marginal_cost_usd`; `n/a` for the first crossing.
- **7d at crossing** — `seven_day_pct_at_crossing` formatted as integer percent; `—` if null.

## Empty case

If the selected block recorded zero milestones (block stayed under 1%),
the header line prints followed by:

```
No milestones recorded — block did not cross 1%.
```

Exit 0.

## Examples

```bash
cctally five-hour-breakdown
cctally five-hour-breakdown --block-start 2026-04-30T19:30
cctally five-hour-breakdown --ago 1
cctally five-hour-breakdown --json
```

## JSON shape

```jsonc
{
  "schemaVersion": 1,
  "block": {
    "blockStartAt":             "2026-04-30T19:30:00Z",
    "fiveHourWindowKey":        1777595400,
    "fiveHourResetsAt":         "2026-05-01T00:30:00Z",
    "lastObservedAtUtc":        "2026-04-30T23:36:11Z",
    "status":                   "active",
    "finalFiveHourPercent":     66.7,
    "totalCost":                81.45,
    "dollarsPerPercent":        1.22,
    "inputTokens":              12345,
    "outputTokens":             67890,
    "cacheCreationTokens":      11111,
    "cacheReadTokens":          22222,
    "sevenDayPctAtBlockStart":  62.5,
    "sevenDayPctAtBlockEnd":    66.7,
    "sevenDayPctDeltaPp":       4.2,
    "crossedSevenDayReset":     false
  },
  "milestones": [
    {
      "percentThreshold":       1,
      "capturedAt":             "2026-04-30T19:42:11Z",
      "blockCostUSD":           5.67,
      "marginalCostUSD":        null,
      "sevenDayPctAtCrossing":  4.0
    }
  ]
}
```

## Notes

- Forward-only / write-once. A block opened on a pre-schema DB shows
  milestones from the percent floor at first observation onward only —
  earlier crossings are not synthesized (matches `percent-breakdown`).
- Selector mismatch (`--block-start` doesn't resolve to any block) prints
  `cctally five-hour-breakdown: no block matches '…' (closest: <ts>)`
  and exits 2.

## See also

- [`five-hour-blocks`](five-hour-blocks.md) — list view of all blocks
- [`percent-breakdown`](percent-breakdown.md) — same lens, weekly axis
- [`record-usage`](record-usage.md) — writes the milestones rendered here
