# `cctally budget`

Track spend against a budget, **per vendor** (Claude and/or Codex) over a
configurable **calendar period**. `cctally budget` shows spend, pace, projected
end-of-period, and a verdict (`ok` / `warn` / `over`); `cctally budget set
<amount>` and `cctally budget unset` manage the budget. When a budget is set and
alerts are enabled, a desktop alert fires once per crossed threshold as actual
spend passes it (see [Alert behavior](#alert-behavior)).

The Claude budget defaults to the **subscription week** (the original v1
behavior) but can run over a **calendar week** or **calendar month** instead. A
separate **Codex (OpenAI) budget** tracks Codex's *actual API dollars* over a
calendar week or month — Codex has no Anthropic subscription week, so a Codex
budget is calendar-period only. The two budgets are independent: configure
either, both, or neither. There is **no combined cross-vendor cap** (Claude is
equivalent-$, Codex is actual-$ — they are not summed).

`budget` is a cctally-original command (not a `ccusage` drop-in), so there is
no `claude budget` / `codex budget` subgroup form — the flat `cctally budget`
is the only surface.

## Subcommands

- `cctally budget` — status report (Claude block, then the Codex block when configured, then per-project).
- `cctally budget set <amount>` — set the **Claude** budget to `<amount>` USD (a positive finite number). Confirms with the resolved budget + period + alert state.
- `cctally budget set <amount> --vendor codex` — set the **Codex** budget. Codex requires a calendar period (`--period calendar-week` or `calendar-month`; default `calendar-month`).
- `cctally budget set <amount> --period {calendar-week,calendar-month,subscription-week}` — set the Claude budget's period. Omitting `--period` preserves the stored period (or the per-vendor default on first create).
- `cctally budget unset` — clear the **Claude** budget amount (period + alert thresholds are preserved for the next `set`).
- `cctally budget unset --vendor codex` — remove the **Codex** budget entirely.

```bash
cctally budget                              # status
cctally budget set 300                      # Claude weekly budget = $300 (subscription week)
cctally budget set 300 --period calendar-month   # Claude budget over the calendar month
cctally budget set 200 --vendor codex            # Codex budget = $200/calendar-month (actual API $)
cctally budget set 50 --vendor codex --period calendar-week
cctally budget unset                        # clear the Claude amount
cctally budget unset --vendor codex         # remove the Codex budget
cctally budget --json                       # machine-readable status
cctally budget --format md                  # shareable markdown artifact
```

## Status output

The legacy single-vendor / subscription-week status is byte-stable:

```
Weekly budget: $300.00   (subscription week 2026-05-26 → 2026-06-02)

  Spent so far    $182.40    60.8% of budget
  Remaining       $117.60
  Pace            $36.48/day  ·  4.2 d elapsed
  Daily budget    $42.00/day for the 2.8 d left to stay under
  Projected EOW   $258–$304   →   ⚠ WARN

  Alerts: on · thresholds 90% · 100% · (none crossed yet)
```

With a calendar period and/or a Codex budget, the report renders a labeled
block per configured vendor — Claude first, then Codex — and the header carries
a cost-basis parenthetical (`— equivalent-$` for Claude, `— actual API $` for
Codex):

```
Claude budget: $300.00   (calendar month 2026-06)   — equivalent-$

  Spent so far    $182.40    60.8% of budget
  …

Codex budget: $200.00   (calendar month 2026-06)   — actual API $

  Spent so far    $96.00    48.0% of budget
  …
  Alerts: off
```

- **Spent so far** — live cost over `[period_start, now]`, with consumption as a
  percent of the budget. Claude is **equivalent-$** (recomputed from Claude
  `session_entries`); Codex is **actual API $** (recomputed from Codex
  `codex_session_entries`).
- **Remaining** — `budget − spent` (negative once you go over).
- **Pace** — current period-average burn ($/day) and days elapsed in the period.
- **Daily budget** — the $/day for the remaining days that keeps you under the budget.
- **Projected EOW** — a low–high projection band (period-average rate vs. trailing-24h rate) with the verdict. (Projected-pace *alerts* fire on the period-average leg of this band — for any Claude period and for Codex budgets; see [Alert behavior](#alert-behavior).)

The header period label is the *display-tz civil* window: `(subscription week
… → …)`, `(calendar week 2026-06-01 → 06-08)`, or `(calendar month 2026-06)`.

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

- **Claude spend** is recomputed live from `session_entries` over the effective
  window via the same `_sum_cost_for_range(period_start, now, mode="auto")` path
  that `weekly` and `forecast` use — embedded pricing edits take effect
  immediately, no cache invalidation. Worst case (no cache, no entries): spend is
  `$0` and the report renders accordingly.
- **Codex spend** is recomputed live from `codex_session_entries` over the
  resolved calendar window via `_sum_codex_cost_for_range(start, now)` (the same
  `get_codex_entries()` + `CODEX_MODEL_PRICING` primitives `codex-daily` /
  `codex-weekly` use), at the effective `auto` speed. This is the **actual API
  dollars** Codex charges — *not* an Anthropic-equivalent figure. Empty Codex
  cache → `$0`. The Codex spend reconciles to the `codex-*` reports within
  `1e-9` USD over the same window (`bin/cctally-reconcile-test`).
- **Window** depends on the period:
  - **subscription-week** (Claude only) — the reset-aware subscription week
    anchored on `--resets-at`, jitter-normalized, and re-anchored at the
    effective reset moment when a mid-week quota reset occurred. A mid-week reset
    starts a fresh budget window (spend and crossings reset for the new week).
  - **calendar-week / calendar-month** — a civil week (anchored on the
    configured week-start day) or civil month resolved in the **display
    timezone** (`display.tz`), DST-correct (a spring-forward week is a true 167h
    span, fall-back 169h). Rolling into the next period yields a fresh window;
    spend and crossings reset.
- **Per-vendor, no combined cap.** Claude (equivalent-$) and Codex (actual-$)
  are tracked independently. A single cross-vendor cap is intentionally **not**
  built — summing equivalent-$ and actual-$ would be semantically muddy. A Codex
  budget never requires a Claude budget (or vice versa).
- **Calendar / Codex budgets need no usage snapshots.** Unlike the v1
  subscription-week budget, the calendar and Codex paths do not depend on
  `weekly_usage_snapshots` — a fresh machine with a configured Codex budget
  renders `$0` / `0%` rather than "no usage data yet this week".

## Config keys

The budget lives under the `budget` block in
`~/.local/share/cctally/config.json`, managed either by `cctally budget
set/unset` or directly via `cctally config`:

- `budget.weekly_usd` — the **Claude** budget in USD (a number, or `null` for "no budget"). The name is a back-compat misnomer: it applies whatever period `budget.period` selects, not strictly a week.
- `budget.period` — the Claude budget period: `subscription-week` (default), `calendar-week`, or `calendar-month`.
- `budget.alerts_enabled` — whether Claude spend-crossing alerts fire (boolean; default `true`).
- `budget.alert_thresholds` — comma-separated integer percents of the budget that fire an alert when crossed (default `90,100`; an empty list silences alerts while keeping the verdict).
- `budget.projected_enabled` — opt-in projected-pace alerts (default `false`; any Claude period — requires `budget.alerts_enabled` to fire; see [Alert behavior](#alert-behavior)).
- `budget.codex` — the **Codex** budget, a nested JSON object (or `null` for "no Codex budget"). See [Codex budget](#codex-budget).
- `budget.projects` / `budget.project_alerts_enabled` — per-project budgets + their opt-in alert toggle. See [Per-project budgets](#per-project-budgets).

```bash
cctally config set budget.weekly_usd 300
cctally config set budget.period calendar-month
cctally config set budget.alert_thresholds 80,90,100
cctally config set budget.alerts_enabled false
cctally config get budget.alert_thresholds
cctally config unset budget.weekly_usd
```

`budget set`/`unset` (Claude) write only `budget.weekly_usd` (and
`budget.period` when `--period` is given); they preserve `budget.period`,
`budget.alerts_enabled`, and `budget.alert_thresholds`. `budget set/unset
--vendor codex` write/remove the `budget.codex` block. All mutations target the
**default** config (the one the alert path reads) — `--config` is rejected on
the mutating forms (see below).

## Flags

- `--vendor {claude,codex}` — which vendor budget `set`/`unset` operate on (default `claude`). Codex budgets are calendar-period only.
- `--period {subscription-week,sub-week,calendar-week,week,calendar-month,month}` — the budget period for `set`. Short spellings normalize (`sub-week`→`subscription-week`, `week`→`calendar-week`, `month`→`calendar-month`). Omitting it preserves the stored period, else the per-vendor default (`claude`=subscription-week, `codex`=calendar-month). `--vendor codex --period subscription-week` (or `sub-week`) exits 2 with a clear message — Codex has no subscription week.
- `--json` — emit machine-readable JSON instead of the terminal report (status), or a small confirmation object (`set`/`unset`).
- `--config PATH` — read status from an alternate config file. **Read-only**, and honored only on the bare status form; `--config` on `set`/`unset` exits 2 (mutations always target the default config).
- `--tz TZ` — display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract.
- `--format {md,html,svg}` — render a shareable artifact instead of the terminal report. See [Shareable reports](share.md).
- `--theme {light,dark}` — color theme for HTML/SVG (no-op for markdown).
- `--no-branding` — strip the "Generated by cctally" footer from `--format` output.
- `--output PATH` — write `--format` output to `PATH` (`-` for stdout); default destination is stdout for `md`, `~/Downloads/cctally-budget-<utcdate>.<ext>` for `html`/`svg`.
- `--copy` — pipe `--format md` to the clipboard (rejected for html/svg).
- `--open` — open the written `--format html`/`svg` file (rejected for md).
- `--reveal-projects` — show real project basenames in the per-project share section instead of the default anonymized `Project A/B/…` labels (see [Per-project budgets](#per-project-budgets)). Inert when no projects are configured.

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
  "period": "subscription-week",
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

`period` (always present) is the Claude budget period. When a Codex budget is
configured, an additive `codex` sibling object carries the parallel fields,
amount under `amount_usd` and window under `period_start_at` / `period_end_at`:

```json
"codex": {
  "amount_usd": 200.0,
  "period": "calendar-month",
  "alerts_enabled": false,
  "alert_thresholds": [90, 100],
  "period_start_at": "2026-06-01T00:00:00Z",
  "period_end_at": "2026-07-01T00:00:00Z",
  "as_of": "2026-06-04T19:52:26Z",
  "spent_usd": 96.0,
  "remaining_usd": 104.0,
  "consumption_pct": 48.0,
  "elapsed_fraction": 0.128,
  "projected_eow_low_usd": 0.0,
  "projected_eow_high_usd": 0.0,
  "daily_pace_usd": 0.0,
  "daily_budget_remaining_usd": 7.64,
  "verdict": "ok",
  "low_confidence": true,
  "crossed_thresholds": []
}
```

Both `period` and the `codex` block are **additive** — no `schemaVersion` bump.
The `codex` key is present **only** when a Codex budget is configured. Window
timestamps are always UTC (`…Z`), ignoring `display.tz`, like every other
cctally `--json`. (The unified `budget_milestones` table — Codex rows tagged
`vendor='codex'` — internally stores `period_start_at` as the `+00:00` offset
form; the `--json` view normalizes to `…Z`.) Other status shapes:

- No Claude budget set → `{"schemaVersion":1,"status":"unset","weekly_usd":null}` (plus a `codex` block if one is configured).
- Budget set but no usage window resolvable yet → `{"schemaVersion":1,"status":"no_data","weekly_usd":<budget>}`.
- `budget set <amount> --json` → `{"status":"set","weekly_usd":300.0,"period":"subscription-week","alerts_enabled":true,"alert_thresholds":[90,100]}`.
- `budget set <amount> --vendor codex --json` → `{"status":"set","vendor":"codex","amount_usd":200.0,"period":"calendar-month","alerts_enabled":false,"alert_thresholds":[90,100]}`.
- `budget unset --json` → `{"status":"unset","weekly_usd":null}`; `budget unset --vendor codex --json` → `{"status":"unset","vendor":"codex"}`.

## Alert behavior

When the Claude budget is set and `budget.alerts_enabled` is true, `record-usage`
fires a desktop alert once per `budget.alert_thresholds` entry as **actual
equivalent-$ spend** crosses that percent of the budget. Crossings are
**forward-only from the moment the budget was set** — setting (or raising) a
budget does not retro-fire alerts for spend that already happened. Crossings
reset when a new period begins (a new subscription week, or rolling into the next
calendar week/month, or a mid-week quota reset). The status report's `Alerts:`
line shows the on/off state, the configured thresholds, and which (if any) have
been crossed this period.

The Claude `budget` alert axis is **period-generalized**: the milestone window
key (internally `week_start_at`, a back-compat misnomer) holds the resolved
period-start instant for a calendar period. The fired alert carries a `period`
discriminator so the [dashboard](#dashboard-surfacing) labels "Month" / "Calendar
week" / "Week" correctly.

**Projected-pace alerts** (`budget.projected_enabled`) fire over **any** Claude
period — `subscription-week`, `calendar-week`, or `calendar-month`. When the
projected end-of-period spend (the week-/period-average projection) crosses a
threshold, the `projected` axis fires once per `(period_start_at, threshold)`,
re-arming each period. Like the actual-spend `budget` axis, projected-pace
requires `budget.alerts_enabled` to be on — `budget.projected_enabled` alone is
inert. Codex budgets fire projected-pace too; see [Codex budget](#codex-budget).

### Codex budget

A Codex budget tracks Codex's **actual API dollars** over a calendar week or
month. Set the **amount** (and period) via `budget set --vendor codex` (CLI-only)
or directly (the `alerts_enabled` / `projected_enabled` toggles are also writable
from the [dashboard](#dashboard-surfacing)):

```bash
cctally config set budget.codex '{"amount_usd": 200, "period": "calendar-month", "alerts_enabled": true}'
cctally config get budget.codex          # round-trips as JSON
cctally config unset budget.codex
```

The `budget.codex` block (a nested JSON object, validated like
`budget.projects`):

- `amount_usd` — required; a positive finite number.
- `period` — `calendar-week` or `calendar-month` (default `calendar-month`). **Not** `subscription-week` — Codex has no Anthropic week.
- `alerts_enabled` — boolean, **default `false`** (opt-in, like the projected/project axes).
- `alert_thresholds` — integer percents in `[1, 100]`, strictly increasing (default `90,100`).
- `projected_enabled` — boolean, default `false` (opt-in Codex projected-pace alerts). Requires `alerts_enabled` to also be on to fire (mirrors the Claude projected toggle); a `codex_budget_usd` projected crossing fires once per `(period_start_at, threshold)`, re-arming each period, from `record-usage` and opportunistically on `cctally budget`.

#### The `codex_budget` alert axis

When `budget.codex.alerts_enabled` is on, a desktop alert fires once per
`(period_start_at, threshold)` as Codex **actual spend** crosses that percent of
the Codex budget — the same forward-only / fire-once / set-then-dispatch contract
as the global budget axis. Setting a Codex budget mid-period when already over
records the crossed thresholds as already-alerted **without** an instant popup
(reconcile-on-set); only later crossings fire. Rolling into the next period
re-arms (a fresh `period_start_at`).

The alert is a sixth axis (`codex_budget`) alongside `weekly` / `five_hour` /
`budget` / `projected` / `project_budget`:

- **Notification text** labels the vendor + civil period:
  *"Codex - $210.00 of $200.00 (105% of budget)"* with a "(this month)" /
  "(this week)" subtitle.
- **Firing trigger** — because Codex usage never flows through Claude's
  `record-usage`, the `codex_budget` axis fires from **two** places: every Claude
  `record-usage` hook-tick (which gains a Codex-budget check), and
  **opportunistically** whenever you run `cctally budget` (computing the Codex
  section already resolves spend + crossings). Forward-only/fire-once means the
  double-trigger never double-fires. **Documented limitation:** a pure-Codex user
  who never runs Claude Code gets no push until their next `cctally` invocation.
- **Dashboard** — see [Dashboard surfacing](#dashboard-surfacing).
- **Test it** — `cctally alerts test --axis codex-budget` dispatches a synthetic
  example end-to-end (prints `notifier: <resolved>`).

#### Dashboard surfacing

Fired Codex alerts appear in the dashboard "Recent alerts" panel/modal (and as a
toast) with a distinct **"CODEX"** chip (vs the global "BUDGET" / per-project
"PROJECT" chips) and a **period-aware** label ("Month of …" / "Calendar week of
…", not "Week"). The same period-aware label fix applies to calendar-period
**Claude** budget alerts. A Codex projected-pace crossing reuses the
**"PROJECTED"** chip with a vendor-tagged context line — *"projected \$230 of
\$200 · Codex"*.

The dashboard **Settings** overlay (key `s`) exposes two Codex toggles —
**"Codex budget alerts"** (`budget.codex.alerts_enabled`) and **"Codex
projected-pace alerts"** (`budget.codex.projected_enabled`). They write through a
**nested partial-merge** — flipping either toggle updates only that sub-leaf and
never clobbers the Codex `amount_usd` / `period` / `alert_thresholds` (those stay
**CLI-only**). When **no Codex budget is configured** both toggles render
**disabled** with the hint *"Set a Codex budget via the CLI first: `cctally
budget set 200 --vendor codex`"* — set an amount on the CLI, and the toggles
become active on the next dashboard tick. Note the same dependency as the CLI:
**Codex `projected_enabled` requires `alerts_enabled`** to actually fire (the two
toggles are independent in the UI; the dependency is enforced server-side, not in
the overlay).

## Per-project budgets

Beyond the single global weekly budget, you can set a separate weekly
equivalent-$ budget **per project** (keyed by canonical git-root). Per-project
budgets are independent of the global budget — you can configure projects with
no global `budget.weekly_usd` set at all.

### Managing per-project budgets

`budget set`/`unset` take an optional `--project` flag:

```bash
cd ~/repos/foo
cctally budget set 25 --project          # budget the cwd's git-root at $25/week
cctally budget set 25 --project /abs/path # budget an explicit git-root path
cctally budget unset --project            # clear the cwd's git-root budget
cctally budget unset --project /abs/path  # clear an explicit path
```

- Bare `--project` resolves the **current working directory** to its canonical
  git-root. Outside a git repo it exits 2.
- `--project <path>` resolves the given path to its git-root (so you can budget
  any repo without `cd`-ing into it).
- Same-basename repos stay distinct: the identity is the full git-root path
  (`ProjectKey.bucket_path`), not the basename, so `~/a/foo` and `~/b/foo` are
  two independent budgets that render with disambiguated `Project` labels.

> **Argument order:** put the amount **before** `--project`
> (`cctally budget set 25 --project`). The flag-first form
> (`cctally budget set --project 25`) binds `25` to `--project` and is
> rejected with a hint pointing at the correct ordering.

### Per-project display section

`cctally budget` (bare) prints a compact per-project table **below** the global
status, one row per configured project, sorted by `Used %` descending:

```
Per-project budgets

  Project      Budget    Spent     Used %   Verdict
  foo          $25.00    $26.00     104%    ⚠ OVER
  bar          $50.00    $12.30      25%    ok
```

- Only configured projects appear; an empty `budget.projects` omits the section.
- The section renders even when the global `budget.weekly_usd` is unset (and
  even on the "no budget set" / "no usage data yet" paths — it degrades to a
  brief note when no usage window has resolved yet).
- A configured project whose repo was deleted/moved (or that never matched any
  session entry this week) shows a `$0 / 0% / ok` row — never an error.
- The displayed verdict is **projection-based** (same `ok`/`warn`/`over` ladder
  + `LOW CONF` cue as the global status); the per-project **push alerts** below
  fire on actual-spend crossings — the same split the global budget uses.

`--json` carries an additive `projects[]` array (no `schemaVersion` bump),
present even when the global budget is unset:

```json
"projects": [
  {"project": "foo", "project_key": "/Users/me/repos/foo", "budget_usd": 25.0,
   "spent_usd": 26.0, "consumption_pct": 104.0, "verdict": "over",
   "low_confidence": false}
]
```

`--json` emits real git-root paths (like `project --json`). Share output
(`--format`) routes per-project names through the anonymization chokepoint:
`--reveal-projects` shows real basenames, the default anonymizes to
`Project A/B/…`.

### Per-project config keys

- `budget.projects` — a flat map `{ "<canonical-git-root>": <usd> }` (default
  `{}`). Values are positive finite numbers. Primary management is
  `budget set/unset --project`; for direct config edits it round-trips as JSON
  (`config get budget.projects` emits JSON; `config set budget.projects '<json-object>'`
  JSON-parses + validates).
- `budget.project_alerts_enabled` — boolean, **default `false`** (opt-in). Gates
  per-project push **alerts** only; the display section always renders
  configured projects regardless. Follows the `projected`-axis precedent
  (opt-in, default OFF) rather than the global budget's "alerts on when set".

```bash
cctally config get budget.projects                 # emits JSON
cctally config set budget.projects '{"/abs/repo": 40}'
cctally config set budget.project_alerts_enabled true
cctally config unset budget.projects
```

Per-project alerts reuse the global `budget.alert_thresholds` and the shared
3-tier severity — there is no separate per-project threshold config in v1.

### The `project_budget` alert axis

When `budget.project_alerts_enabled` is on, `record-usage` fires a desktop
alert once per `(project, threshold)` as a project's **actual spend** crosses
that percent of its own budget — the same forward-only / fire-once / set-then-
dispatch contract as the global budget axis, scaled to a project dimension.
Setting a project budget mid-week when already over records the crossed
thresholds as already-alerted **without** an instant popup (forward-only-from-
write reconcile); only later crossings fire.

The alert is a fifth axis (`project_budget`) alongside `weekly` / `five_hour` /
`budget` / `projected`:

- **Notification text** carries the project basename:
  *"Project foo - $26.00 of $25.00 (104% of budget)"*.
- **Dashboard** — fired project alerts appear in the existing "Recent alerts"
  panel/modal with a distinct **"PROJECT"** chip (vs the global "BUDGET" chip)
  and the project basename + `$spent of $budget` context. The Settings overlay
  (`s`) has a **"Per-project budget alerts"** on/off toggle that persists
  `budget.project_alerts_enabled`. (Editing per-project budget *amounts* stays
  CLI-only — the dashboard only views fired alerts and toggles the axis.)
- **Test it** — `cctally alerts test --axis project-budget` (CLI) or the
  dashboard Settings "Send test alert" picker (axis "Project budget") dispatch a
  synthetic example ($26 of $25) without needing a real `budget.projects` entry.

## Exit codes

- `0` — normal (status rendered, including the "no budget set" case; `set`/`unset` succeeded).
- `2` — usage error: a non-positive or non-numeric amount on `set`; `--config` on `set`/`unset`; `--format` on `set`/`unset`; `--vendor codex --period subscription-week` (Codex has no subscription week); an invalid share-flag combination; a malformed `budget` config block (including a malformed `budget.codex` block, or a Codex period of `subscription-week`); or `--project` (bare) outside a git repo.

## Related

- [`forecast`](forecast.md) — projects current-week usage **%** to the reset (the percent analog of this budget's dollars).
- [`weekly`](weekly.md) — per-subscription-week cost rollup.
- [`alerts`](alerts.md) — alert configuration and the `alerts test` harness.
- [Shareable reports](share.md) — the `--format`/`--theme`/`--output` surface.
