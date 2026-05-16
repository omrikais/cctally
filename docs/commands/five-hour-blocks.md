# `five-hour-blocks`

List API-anchored 5-hour blocks with rollup totals plus 7d-drift columns.

## Synopsis

```
cctally five-hour-blocks
    [-s YYYYMMDD] [-u YYYYMMDD]
    [--breakdown {model,project}]
    [--json] [--no-color] [--tz TZ]
```

## Purpose

Analytics view on the `five_hour_blocks` table populated by `record-usage`
each tick. Distinct from `cctally blocks` (the upstream-parity drop-in that
also shows heuristic-anchored windows): `five-hour-blocks` is API-anchored
only, and adds 7d-drift columns plus a `⚡` reset-crossing marker.

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYYMMDD` | Filter from date (inclusive). |
| `-u, --until YYYYMMDD` | Filter until date (inclusive). |
| `--breakdown {model,project}` | Add per-model OR per-project rollup-child rows under each parent block (single value, not multi). |
| `--json` | Emit camelCase JSON (`schemaVersion: 1`). |
| `--no-color` | Disable ANSI color (currently a no-op — table is plain text). |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |

## Default columns

7-column table:

| Block Start | Status | 5h % | Cost | $/1% | 7d % range | Δ7d |

- **Block Start** — `block_start_at` formatted `YYYY-MM-DD HH:MM <zone>`. Crossed-reset rows render with a leading `⚡ ` prefix on this cell (mirroring the `~` heuristic-anchor convention in `cctally blocks`).
- **Status** — `ACTIVE` (open block) or `closed`.
- **5h %** — `final_five_hour_percent`; live for the active row.
- **Cost** — `total_cost_usd` (recomputed every tick from `session_entries`).
- **$/1%** — `cost / 5h%`. Renders `—` when 5h% < 0.5 (matches `report` $/1% clamp).
- **7d % range** — `seven_day_pct_at_block_start → seven_day_pct_at_block_end`, e.g. `62.5→66.7`. Right side is the latest live 7d% for the active row.
- **Δ7d** — signed pp delta (`end − start`); `—` when `crossed_seven_day_reset=1` or when `seven_day_pct_at_block_start` is null.

## Default time window

No implicit date filter; capped at **last 50 rows** descending by `block_start_at` when neither `--since` nor `--until` is given. Either filter lifts the cap and returns all rows in the date range.

## Footer

```
<N> blocks · cost: $<sum>[ · ⚡ = block crossed weekly reset]
```

The `⚡` legend only appears when at least one row crossed.

## Examples

```bash
cctally five-hour-blocks
cctally five-hour-blocks --since 20260420
cctally five-hour-blocks --breakdown model
cctally five-hour-blocks --breakdown project --json
```

## JSON shape

```jsonc
{
  "schemaVersion": 1,
  "window": { "since": "...", "until": "...", "limit": 50, "order": "desc",
              "count": 12, "truncated": false },
  "blocks": [
    {
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
      "crossedSevenDayReset":     false,
      "modelBreakdowns":   [ /* present iff --breakdown=model */ ],
      "projectBreakdowns": [ /* present iff --breakdown=project */ ]
    }
  ]
}
```

`modelBreakdowns` / `projectBreakdowns` are mutually exclusive: each is
present only under the matching `--breakdown` value, omitted otherwise.
The sentinel `"projectPath": "(unknown)"` covers entries with NULL
`session_files.project_path`.

## Credit annotations

When Anthropic issues an in-place 5h credit (utilization drops while
`rate_limits.5h.resets_at` stays unchanged), affected block rows render
with an inline `⚡ credited −Xpp @ HH:MM` chip beside the block-start
time. Multiple credits in the same block concatenate as `⚡ −Xpp, −Ypp`
(up to ~30 distinct 10-minute slots per block; same-slot collisions
absorb to the first observation by the underlying
`UNIQUE(five_hour_window_key, effective_reset_at_utc)` schema
constraint). The chip carries through `--format md` / `html` / `svg`
share output in the same `block_start` cell.

JSON envelopes carry the credit details per block. Each credit row
exposes the 10-min-floored effective moment plus the pre- and post-credit
percent values so downstream consumers can render the chip without
re-querying snapshots:

```jsonc
{
  "blockStartAt": "2026-05-16T19:30:00Z",
  "credits": [
    {
      "effectiveResetAtUtc": "2026-05-16T21:00:00Z",
      "priorPercent":         28.0,
      "postPercent":          8.0,
      "deltaPp":              -20.0
    }
  ]
}
```

The `credits` field is omitted (or empty `[]`) on uncredited blocks.

## Notes

- API-anchored only. Heuristic windows (no recorded `rate_limits.5h.resets_at`
  snapshot) never produce a `five_hour_blocks` row; they remain visible
  via `cctally blocks` with a `~` prefix.
- `total_cost_usd` and the four token columns are recomputed every tick
  from `session_entries`; pricing edits to `CLAUDE_MODEL_PRICING` take
  effect on the next `record-usage` tick with no invalidation.
- `--tz` affects display only. `--since` / `--until` accept `YYYYMMDD` and parse as the corresponding UTC date (this command's selectors are not display-tz-shifted; see [Display timezone](config.md#how-displaytz-interacts-with-subcommands)).

## See also

- [`blocks`](blocks.md) — upstream-parity drop-in including heuristic windows
- [`five-hour-breakdown`](five-hour-breakdown.md) — per-percent milestones inside one block
- [`record-usage`](record-usage.md) — populates `five_hour_blocks` each tick


## Shareable output

`cctally five-hour-blocks` accepts `--format {md,html,svg}` and related flags for shareable artifacts. See [share.md](share.md) for the full flag reference.
