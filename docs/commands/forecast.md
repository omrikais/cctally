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


## `--json` (`schemaVersion: 2`)

The payload bumped from `schemaVersion: 1` in #661 S2. Both meters display a **ceiling**, so a shown reading of `k` means true consumption fell in `[k-1, k)`. Every rate, projection and budget is now measured from the unbiased midpoint of that interval rather than from the displayed number, which reads about half a point lower. The displayed reading is unchanged and still appears as `current.weekly_percent`.

Three existing keys changed both their type and their meaning, which `docs/cli-contract.md` classifies as breaking on either count:

| Key | v1 | v2 |
|---|---|---|
| `forecast.final_percent_low` | always a number, raw-derived | number or `null`, corrected-derived |
| `forecast.final_percent_high` | always a number, raw-derived | number or `null`, corrected-derived |
| `forecast.week_avg_projection_pct` | always a number, raw-derived | number or `null`, corrected-derived |

They are `null` when the projection is withheld, and `forecast.projection_basis` plus `forecast.projection_code` say why:

- **Right-censored.** A displayed 100 denotes `[99, +infinity)` and has no point estimate, so no projection, no time-to-cap, no daily budget and no week-average rate is derived from it. `forecast.right_censored` is `true`, `projection_basis` is `"withheld"` and `projection_code` is `"right-censored"`.
- **Unavailable.** The corrected point, the elapsed span or the remaining span is missing, so there is no pace to project along. `projection_basis` is `"withheld"` and `projection_code` is `"unavailable"`.

**A displayed zero is not one of those states.** Right-censoring applies at 100 and only at 100, because that reading alone denotes an interval unbounded above. Every other reading, zero included, denotes a bounded interval whose midpoint is the corrected point, so a displayed 0 projects from 0.25 exactly as a displayed 40 projects from 39.5, and its week-average rate is published on the same basis.

`rates.dollars_per_percent` is a separate decision and is still withheld at a displayed zero. Its gate is about having a signal to divide by, not about the reading's interval, and the two must not be read as one.

The additive keys, which would not on their own have required the bump:

- `current.weekly_percent_corrected` — the corrected midpoint the arithmetic ran on; `null` when right-censored.
- `current.weekly_percent_interval` — the `[lo, hi]` the displayed reading denotes; `hi` is `null` at the cap.
- `forecast.right_censored` — which state the nulls above mean.
- `forecast.projection_basis` — `"calibrated"`, `"corrected-meter"` or `"withheld"`.
- `forecast.projection_code` — the typed cause of a withheld projection, a member of the quota model's closed evidence vocabulary.
- `forecast.calibration_code` — why the calibrated basis was **not** reached, `null` when it was. Distinct from `projection_code`: falling back to the corrected meter is not a withholding.

`rates.week_average_pct_per_hour` is `null` at a right-censored reading for the same reason the projections are.

### The qualified trailing-median rate (#661 S2 §4.2)

`rates.dollars_per_percent_source` names which rule produced the rate, and two of its values qualify a rate that IS published rather than explaining one that is not:

- `trailing_4wk_median_drifted` — one or more prior weeks were dropped from the median because their model mix sits outside the calibration's support, so the rate is measured over a reduced population.
- `trailing_4wk_median_unverified` — the comparability test could not read the entry store at all, so the prior weeks in the median were never checked against the current metering rate.

Both accompany a NON-null `dollars_per_percent`. A consumer reading the rate without reading this field gets a number with no statement of what it was measured over.

Candidate weeks never cross a metering-rate boundary, and that restriction holds even when the population read fails: the boundary test is a date comparison against the fitted regime's own interval and needs no store access, so an unreadable entry cache makes the POPULATION test inert and leaves the boundary test running. With fewer than four surviving candidates the sparse or current-week fallback runs instead.

### The dollar rate at a censored reading

`rates.dollars_per_percent` is published at a right-censored reading, and it is divided by the **raw displayed reading** rather than by a corrected point. That is not an oversight. A censored reading has no corrected point at all — it denotes `[99, +infinity)` — so there is nothing else to divide by, and the alternative would be to withhold a rate that is a real, useful lower-bound estimate of dollars per meter point. On the `already-capped` fixture that publishes `0.407767` beside null projections, which is the intended pair: the RATE is available and the PROJECTIONS are not, because the projections need an upper bound on consumption and the rate does not.

## Shareable output

`cctally forecast` accepts `--format {md,html,svg}` and related flags for shareable artifacts. See [share.md](share.md) for the full flag reference. At a right-censored reading the artifact's "Projected end-of-week %" row states `withheld — meter at cap` and the projection ray is omitted from the chart. A ceiling distance further out than 30 days renders as `>30d` rather than as its literal figure: a very small rate produces a very distant date, and that is a presentation bound rather than a withholding — the rate underneath is still published.

## Exit codes

- `0` — success.
- `2` — usage/validation error (e.g. `--json` together with `--status-line`; an unparseable `--targets` value). Changed from `1` in the #279 contract cleanup so `forecast` matches the rest of the cctally-native family. See `docs/cli-contract.md`.
