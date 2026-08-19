# `cctally forecast`

Project current-week usage to the reset boundary. Produces three forms of
output from the same computation:

- Default: a box-framed terminal report with a progress bar, forecast
  range, and daily $ / % budgets at configurable ceilings.
- `--json`: the same data as a machine-readable payload.
- `--status-line`: a compact one-line segment sized for embedding in
  `~/.claude/statusline-command.sh`.

> **Cost coverage:** Claude dollar and token totals are
> [transcript-derived lower bounds](../claude-cost-coverage.md), not exact
> `/usage` billing totals.

## When it helps

Mid-week, glance at the status line; if the projection range creeps
toward 90%+, you're pacing above your sustainable rate and can look at
the full report (`cctally forecast`) to see how much
$ or % of quota per day keeps you under 100%.

## Output states

- **Safe** (`high < 90`): cyan projection range.
- **Approaching** (`90 <= high < 100`): yellow projection with "approaching 100%" note.
- **Projected cap** (`high >= 100`): red projection + `⚠ may cap`; status
  line appends the to-90% $/day budget; terminal report shows projected
  `cap_at` timestamp in the footer.
- **Already capped** (`p_now >= 100`): red `⚠ CAPPED` banner; budget
  section omitted.
- **Low confidence**: shown when any of four triggers fires. Terminal shows the forecast range dimmed with a `LOW CONF` label followed by the reasons in plain words; status line reduces to `tracking…`. The four triggers, with the reason code each one emits in `--json` and the wording the terminal renders for it:

  | Trigger | `--json` code | Terminal wording |
  |---|---|---|
  | Fewer than 24 hours have elapsed since the week started | `elapsed_hours<24` | less than 24 hours into the week |
  | The account has used under 2% of its weekly quota so far | `percent<2` | under 2% of quota used so far |
  | Fewer than 3 usage snapshots have been recorded this week | `snapshots<3` | fewer than 3 usage snapshots |
  | No usage snapshot is at least 24 hours old, so no rate can be measured over a full day | `no_sample_ge_24h` | no snapshot at least 24 hours old |

  More than one trigger can fire at once, and every one that fired is
  named. The `--json` codes are the stable surface and are unchanged; the
  terminal wording is a render step over them. An unrecognised code — one a
  newer binary emits and this render does not know — is printed verbatim
  rather than dropped.

## Data source

- Usage %: latest `weekly_usage_snapshots.weekly_percent` for the current
  subscription week (ground truth for the cap).
- $ spent: live sum of `session_entries` over `[week_start_at, now)` priced
  via `CLAUDE_MODEL_PRICING` (mirrors `weekly`).
- $/1% conversion: current-week rate when `p_now >= 10`; otherwise the
  4-week trailing median; otherwise the current-week rate however sparse.
  The source used is reported in the footer (`rate source: ...`).

## Flags

- `--json` — emit JSON instead of the terminal report.
- `--status-line` — emit the one-liner (mutually exclusive with `--json`).
- `--targets A,B,...` — comma-separated ceilings for the budget table
  (default `100,90`). Integer percentages in (0, 200].
- `--tz TZ` — display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant).
- `--explain` — append a footer with rate values, snapshot count, and rate source.
- `--no-sync` — skip `sync_cache()`; recommended for status-line use since
  `record-usage` already ingests in the background.
- `--color {auto,always,never}` — color control. Also honors `NO_COLOR`.

## Status-line integration

Add to `~/.claude/statusline-command.sh`, parallel to the existing
`record-usage &` snippet:

```bash
forecast_seg=$(cctally forecast --status-line --no-sync 2>/dev/null)
```

Then include `$forecast_seg` wherever your prompt composition renders
segments. When there's no data yet, `$forecast_seg` is empty and the
segment is silent.

## Examples

```bash
cctally forecast
cctally forecast --json | jq '.forecast'
cctally forecast --status-line --no-sync
cctally forecast --targets 100,95,85
```


## Shareable output

`cctally forecast` accepts `--format {md,html,svg}` and related flags for shareable artifacts. See [share.md](share.md) for the full flag reference.

## Exit codes

- `0` — success.
- `2` — usage/validation error (e.g. `--json` together with `--status-line`; an unparseable `--targets` value). Changed from `1` in the #279 contract cleanup so `forecast` matches the rest of the cctally-native family. See `docs/cli-contract.md`.
