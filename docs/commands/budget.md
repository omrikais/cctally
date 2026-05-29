# `cctally budget`

Track Claude equivalent-$ spend for the current subscription week against a
weekly budget. `cctally budget` shows spend, pace, projected end-of-week, and
a verdict (`ok` / `warn` / `over`); `cctally budget set <amount>` and
`cctally budget unset` manage the budget. When the budget is set and alerts are
enabled, a desktop alert fires once per crossed threshold as actual spend
passes it (see [Alert behavior](#alert-behavior)).

`budget` is a cctally-original command (not a `ccusage` drop-in), so there is
no `claude budget` / `codex budget` subgroup form — the flat `cctally budget`
is the only surface.

## Subcommands

- `cctally budget` — status report for the current subscription week.
- `cctally budget set <amount>` — set the weekly budget to `<amount>` USD (a positive finite number). Confirms with the resolved budget + alert state.
- `cctally budget unset` — clear the weekly budget (alert thresholds are preserved for the next `set`).

```bash
cctally budget                # status
cctally budget set 300        # set the weekly budget to $300
cctally budget unset          # clear it
cctally budget --json         # machine-readable status
cctally budget --format md    # shareable markdown artifact
```

## Status output

```
Weekly budget: $300.00   (subscription week 2026-05-26 → 2026-06-02)

  Spent so far    $182.40    60.8% of budget
  Remaining       $117.60
  Pace            $36.48/day  ·  4.2 d elapsed
  Daily budget    $42.00/day for the 2.8 d left to stay under
  Projected EOW   $258–$304   →   ⚠ WARN

  Alerts: on · thresholds 90% · 100% · (none crossed yet)
```

- **Spent so far** — live equivalent-$ cost over `[week_start, now]`, with consumption as a percent of the budget.
- **Remaining** — `budget − spent` (negative once you go over).
- **Pace** — current week-average burn ($/day) and days elapsed in the week.
- **Daily budget** — the $/day for the remaining days that keeps you under the budget.
- **Projected EOW** — a low–high projection band (week-average rate vs. trailing-24h rate) with the verdict.

### Verdict

- `ok` (green) — projected end-of-week spend stays comfortably under budget.
- `warn` (amber) — projected spend reaches the lowest alert threshold (default 90%) of the budget but not over.
- `over` (red) — spend is already over budget, or the projection lands over it.

When the week is very early (less than ~15% elapsed) or no spend has landed
yet, the projection is annotated `(LOW CONF — early in week)` and never
escalates to a spurious `over`.

### No budget set

`cctally budget` with no configured budget prints
`No weekly budget set. Set one with: cctally budget set <amount>.` and exits 0
(this is not an error). `--json` returns
`{"schemaVersion":1,"status":"unset","weekly_usd":null}`.

## Data source & scope

- **Spend** is recomputed live from `session_entries` over the effective
  subscription-week window via the same `_sum_cost_for_range(week_start, now,
  mode="auto")` path that `weekly` and `forecast` use — embedded pricing
  edits take effect immediately, no cache invalidation. Worst case (no cache,
  no entries): spend is `$0` and the report renders accordingly.
- **Window** is the reset-aware subscription week anchored on `--resets-at`,
  jitter-normalized, and re-anchored at the effective reset moment when a
  mid-week quota reset occurred. A mid-week reset starts a fresh budget window
  (spend and crossings reset for the new week).
- **Scope is Claude-only for v1.** The budget tracks Claude equivalent-$,
  not Codex/OpenAI dollars — the subscription week is an Anthropic concept and
  folding in another vendor would conflate two billing models under one
  Anthropic-anchored window. Codex inclusion is deferred.

## Config keys

The budget lives under the `budget` block in
`~/.local/share/cctally/config.json`, managed either by `cctally budget
set/unset` or directly via `cctally config`:

- `budget.weekly_usd` — the weekly budget in USD (a number, or `null` for "no budget").
- `budget.alerts_enabled` — whether spend-crossing alerts fire (boolean; default `true`).
- `budget.alert_thresholds` — comma-separated integer percents of the budget that fire an alert when crossed (default `90,100`; an empty list silences alerts while keeping the verdict).

```bash
cctally config set budget.weekly_usd 300
cctally config set budget.alert_thresholds 80,90,100
cctally config set budget.alerts_enabled false
cctally config get budget.alert_thresholds
cctally config unset budget.weekly_usd
```

`budget set`/`unset` write only `budget.weekly_usd`; they preserve
`budget.alerts_enabled` and `budget.alert_thresholds`. They always write the
**default** config (the one the alert path reads) — `--config` is rejected on
the mutating forms (see below).

## Flags

- `--json` — emit machine-readable JSON instead of the terminal report (status), or a small confirmation object (`set`/`unset`).
- `--config PATH` — read status from an alternate config file. **Read-only**, and honored only on the bare status form; `--config` on `set`/`unset` exits 2 (mutations always target the default config).
- `--tz TZ` — display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract.
- `--format {md,html,svg}` — render a shareable artifact instead of the terminal report. See [Shareable reports](share.md).
- `--theme {light,dark}` — color theme for HTML/SVG (no-op for markdown).
- `--no-branding` — strip the "Generated by cctally" footer from `--format` output.
- `--output PATH` — write `--format` output to `PATH` (`-` for stdout); default destination is stdout for `md`, `~/Downloads/cctally-budget-<utcdate>.<ext>` for `html`/`svg`.
- `--copy` — pipe `--format md` to the clipboard (rejected for html/svg).
- `--open` — open the written `--format html`/`svg` file (rejected for md).
- `--reveal-projects` — **inert for budget** (accepted only for share-surface parity; budget has no per-project axis, so it has no effect).

`--format` is a status-only render surface; passing it with `set`/`unset`
exits 2.

## `--json` schema (`schemaVersion: 1`)

Status with a budget set emits the full status plus a config echo and the
window:

```json
{
  "schemaVersion": 1,
  "status": "ok",
  "weekly_usd": 300.0,
  "alerts_enabled": true,
  "alert_thresholds": [90, 100],
  "week_start_at": "2026-05-26T14:00:00Z",
  "week_end_at": "2026-06-02T14:00:00Z",
  "as_of": "2026-05-30T14:00:00Z",
  "spent_usd": 126.0,
  "remaining_usd": 174.0,
  "consumption_pct": 42.0,
  "elapsed_fraction": 0.571,
  "projected_eow_low_usd": 180.0,
  "projected_eow_high_usd": 220.5,
  "daily_pace_usd": 31.5,
  "daily_budget_remaining_usd": 58.0,
  "verdict": "ok",
  "low_confidence": false,
  "crossed_thresholds": []
}
```

Window timestamps are always UTC (`…Z`), ignoring `display.tz`, like every
other cctally `--json`. Other status shapes:

- No budget set → `{"schemaVersion":1,"status":"unset","weekly_usd":null}`.
- Budget set but no usage window resolvable yet → `{"schemaVersion":1,"status":"no_data","weekly_usd":<budget>}`.
- `budget set <amount> --json` → `{"status":"set","weekly_usd":300.0,"alerts_enabled":true,"alert_thresholds":[90,100]}`.
- `budget unset --json` → `{"status":"unset","weekly_usd":null}`.

## Alert behavior

When a budget is set and `budget.alerts_enabled` is true, `record-usage` fires
a desktop alert once per `budget.alert_thresholds` entry as **actual spend**
crosses that percent of the budget. Crossings are **forward-only from the
moment the budget was set** — setting (or raising) a budget does not retro-fire
alerts for spend that already happened. Alert scope is **Claude-only**, matching
the budget's scope. Crossings reset when a new subscription week begins (or a
mid-week quota reset starts a fresh window). The status report's `Alerts:` line
shows the on/off state, the configured thresholds, and which (if any) have been
crossed this week.

## Exit codes

- `0` — normal (status rendered, including the "no budget set" case; `set`/`unset` succeeded).
- `2` — usage error: a non-positive or non-numeric amount on `set`; `--config` on `set`/`unset`; `--format` on `set`/`unset`; an invalid share-flag combination; or a malformed `budget` config block.

## Related

- [`forecast`](forecast.md) — projects current-week usage **%** to the reset (the percent analog of this budget's dollars).
- [`weekly`](weekly.md) — per-subscription-week cost rollup.
- [`alerts`](alerts.md) — alert configuration and the `alerts test` harness.
- [Shareable reports](share.md) — the `--format`/`--theme`/`--output` surface.
