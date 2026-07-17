# `report`

Trend table of dollars per 1% weekly usage. The headline command of this
project.

## Provider-aware report routing

`cctally report` and `cctally report --source claude` preserve the existing
Claude subscription-week trend. The flat command also accepts `--source
{claude,codex,all}` (default `claude`); `cctally claude report` and `cctally
codex report` are fixed-source forms and do not accept `--source`.

Codex report is quota-window-native, not a calendar or Claude subscription
week report. It emits one source/root/logical-limit series, using each selected
native reset block's `[reset - window, min(as-of, reset))` accounting interval.
The `--weeks N` value selects the newest N S2 reset blocks within every logical
limit series, including the current block. Cost per percent is null/unavailable
when the selected percent or quota/cost evidence is insufficient; it is never
fabricated as zero.

`--source all` keeps Claude subscription-week rows and every Codex logical-limit
series in separate source sections. It has **no** `combined` trend, average,
USD, token total, used percent, reset, or dollars-per-percent field: one Codex
accounting interval can overlap multiple logical limits, so adding per-limit
series would double count physical usage. `--speed` applies to Codex/all;
`--week-start-name`, `--mode`, `--offline`, and `--project` are Claude-only
options (they affect only the Claude leg of `all`); `--detail` remains
source-native in both sections.

The fixed `cctally codex report` parser accepts only its native surface: it
does not expose the Claude-only `--week-start-name`, `--mode`, `--offline`, or
`--project` flags. Its `--sync-current` syncs Codex accounting and reconciles
native quota state; it does not invoke `sync-week`.

Direct Codex JSON uses `schemaVersion, source, status, data, warnings`; the
quota-series section can be `ok`, `empty`, or `unavailable`. An all-source JSON
envelope still orders `sources` as Claude then Codex but deliberately omits
`combined`.

As of v1.12.0, historical `weekly_cost_snapshots` rows are recomputed
under the corrected dedup logic (per stats migration
`008_recompute_weekly_cost_snapshots_dedup_fix`). `report` and `weekly`
agree on historical cost for rows with `mode='auto' AND project IS NULL`.
Rows with `mode='display'` (user-supplied costs) and per-project scoped
snapshots are left untouched. Very old rows without `range_start_iso` /
`range_end_iso` populated are skipped — their pre-fix value persists; if
you need them corrected, delete the row and re-run `sync-week`.

## Synopsis

```
cctally report
    [--weeks N] [--sync-current]
    [--week-start-name {monday,…,sunday}]
    [--mode {auto,calculate,display}]
    [--offline] [--project PROJECT]
    [--source {claude,codex,all}] [--speed {auto,standard,fast}]
    [--json] [--detail]

cctally codex report
    [--weeks N] [--sync-current]
    [--speed {auto,standard,fast}]
    [--json] [--detail]
```

## Purpose

For Claude, each recent subscription week joins the latest usage % snapshot with
the latest cost snapshot, divide cost by percent, and render a trend.
This is the metric that surfaces quota-rule changes (or your own usage
shifts) early.

## Options

| Flag | Description |
| --- | --- |
| `--weeks N` | Claude: recent subscription weeks; Codex: recent native quota windows (default `8`). |
| `--sync-current` | Claude: run `sync-week` first. Codex: sync Codex accounting and reconcile native quota state first. |
| `--week-start-name` | Claude-only: week-start day used if report falls back to date-only logic. Not accepted by fixed Codex. |
| `--mode {auto,calculate,display}` | Claude-only mode passed to `sync-week`; not accepted by fixed Codex. |
| `--offline` | Claude-only forwarding to `sync-week`; not accepted by fixed Codex. |
| `--project PROJECT` | Claude-only forwarding to `sync-week`; not accepted by fixed Codex. |
| `--source {claude,codex,all}` | Analytics provider; default `claude`. The `cctally claude report` and `cctally codex report` subgroup forms fix the source and omit this flag. |
| `--speed {auto,standard,fast}` | Codex pricing tier; default `auto`. Applies to the Codex leg of a Codex or all-source request; non-`auto` is rejected for Claude-only. |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | Machine-readable JSON output. |
| `--detail` | Include per-percent cost milestones for the current week. |

## Examples

```bash
cctally report
cctally report --sync-current
cctally report --weeks 12 --json
cctally report --sync-current --detail
cctally codex report --weeks 4 --detail
cctally report --source all --weeks 4 --json
```

The `cctally-dollar-per-percent` wrapper exists for the muscle-memory
shortcut — it's exactly `report --sync-current "$@"`.

## Notes

- Joining uses `WeekRef`: prefers exact `week_start_at` (ISO timestamp),
  falls back to `week_start_date` (date-only) for older snapshots that
  predate the hour-accurate column.
- `$/1%` is meaningful only when both usage % and cost are non-zero. Weeks
  with one but not the other render as `—` for the joined column.
- For Claude, `--detail` adds the `percent-breakdown` view for the *current*
  week only; for an older week, call `percent-breakdown --week-start <date>`
  directly. Codex `--detail` emits native quota-window attribution detail.

## See also

- [`sync-week`](sync-week.md) — produces the Claude cost half of the join
- [`record-usage`](record-usage.md) — produces the usage half of the join
- [`percent-breakdown`](percent-breakdown.md) — same milestones `--detail` shows
- [`cctally-dollar-per-percent`](../../bin/cctally-dollar-per-percent) — wrapper


## Shareable output

`cctally report` accepts `--format {md,html,svg}` and related flags for shareable artifacts. See [share.md](share.md) for the full flag reference.
