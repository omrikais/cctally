# project

Roll Claude or Codex usage up by project. Claude has per-project `Used %`
against a subscription-week ceiling; Codex has calendar-week cost and token
rollups without a fabricated `Used %`. In `--source all`, provider sections
stay separate and `--weeks` resolves one absolute calendar range.

> **Claude cost coverage:** Claude dollar and token totals are
> [transcript-derived lower bounds](../claude-cost-coverage.md), not exact
> `/usage` billing totals. Codex accounting is unaffected.

## Quick examples

    # Current Claude subscription week, sorted by cost desc
    cctally project

    # Last 2 Claude subscription weeks
    cctally project --weeks 2

    # Drill into one project with per-model breakdown
    cctally project --project ccusage --breakdown

    # Machine-readable output
    cctally project --json

    # Fixed Codex route; --weeks means calendar weeks
    cctally codex project --weeks 2 --breakdown

    # Keep provider rows separate while adding only physical cost/token totals
    cctally project --source all --since 2026-07-14 --until 2026-07-20

    # Or via the wrapper (after `install.sh`)
    cctally-project --weeks 2

## Flags

| Flag | Default | Description |
|---|---|---|
| `--since DATE`, `--until DATE` | — | Inclusive range (YYYY-MM-DD). |
| `--weeks N` | — | Last N weeks ending now: Claude subscription weeks, Codex calendar weeks; `all` resolves one absolute calendar range. Mutually exclusive with `--since`/`--until`. |
| *(no range flags)* | source-native current week | Claude subscription week or Codex calendar week; `all` resolves one absolute calendar range. |
| `--project PATTERN` | — | Claude: case-insensitive substring filter on display key. Fixed Codex: exact opaque `projectKey` or exact collision-safe display label. Repeatable (OR). |
| `--model PATTERN` | — | Same but for model names. Repeatable. |
| `--breakdown` | off | Parent project row + one child row per model used. |
| `--order asc\|desc` | `desc` | |
| `--sort cost\|used\|name\|last-seen` | `cost` | Ties break on display key ascending. `used` is Claude-only and is rejected by the fixed Codex route and `--source all`. |
| `--group git-root\|full-path` | `git-root` | `full-path` skips the walker and buckets by `realpath(project_path)` (symlink-aliased spellings collapse into one row; the displayed label is whichever spelling was seen first). |
| `--tz TZ` | config | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | off | Emit structured JSON. |
| `--no-color` | off | Disable ANSI color. |
| `--source {claude,codex,all}` | `claude` | Analytics provider. The default and explicit `claude` preserve the established Claude report. `all` renders Claude then Codex as separate sections. |
| `--speed {auto,standard,fast}` | `auto` | Codex pricing tier for the Codex or all-source leg. A non-`auto` value is rejected for Claude-only requests. |

## Provider-aware routing

The flat command defaults to `--source claude`; `cctally project` and
`cctally project --source claude` therefore retain the existing
subscription-week semantics and bytes. The fixed subgroup forms are equivalent
but do not expose `--source`:

```bash
cctally claude project ...  # fixed Claude
cctally codex project ...   # fixed Codex
```

Codex project rows use an opaque `projectKey` and a privacy-safe
`displayLabel`; neither a home/root path nor a source-root fingerprint is a
user-facing identity. `--project` accepts either an exact opaque key or an
exact display label. A label that matches more than one qualified Codex project
is rejected with exit 2; no match is an ordinary empty result. In an all-source
request the same filter is applied independently to each provider, and equal
labels never merge.

For Codex, `--weeks N` means configured **calendar** weeks. For `--source all`,
that calendar interval is resolved once and used for both provider sections.

The dashboard's All Projects panel adopts the same one-absolute-range rule this
command enforces: both providers are folded over one shared interval, and the
panel names its resolved dates rather than describing the ranking as a week. It
publishes **no quota-share percentage** under All — quota attribution divides by
a subscription week's total and means nothing over an absolute range — so the
percentage there is a share of the ranked cost and the panel legend says so.
Codex cannot calculate Claude-style `--sort used`, so that value is rejected
for Codex and all-source requests. Other project filters, grouping, ordering,
breakdown, timezone, and share flags apply inside each source section.

## Claude range resolution

Without `--since`/`--until`, the Claude range is `weeksInRange` **real
subscription intervals** ending with the one that contains now — the same
intervals `_compute_subscription_weeks` derives from your retained reset
anchors, and the same ones the dashboard Projects panel buckets into.
`rangeStart` is therefore always an interval start, never a date part-way
through one. On a multi-account merged read the intervals are the merged
fragments the reset anchors of every account produce, and a fragment can be
shorter than a week — so `--weeks N` spans N fragments, which together cover
less calendar time than N weeks.

This is a correction as of #620. `--weeks N` previously resolved its start by
stepping back `7 * (N - 1)` days from the current interval's start, which is
short of the truth by the accumulated shortfall of every drifted week inside
the range — Anthropic's reset day moves, and a drifted cycle produces a
genuinely short week. The gap was not empty: cost inside it was folded into
`totals.costUsd`, the previous week's whole quota percentage was summed into
`totals.usedPercent`, and the extra interval could carry no snapshot and
therefore report `Used % unavailable for 1 week` about a week you never asked
for. `rangeStart`, `weeksInRange`, `totals.costUsd` and `totals.usedPercent`
can all move as a result, and the new values describe the window the command
says it is describing.

An explicit `--since` / `--until` range is unaffected: those instants are the
ones you asked for and are used verbatim.

**A credited week counts as one interval per billing cycle.** An Anthropic quota reset leaves a week's boundaries where they are but ends one billing cycle and begins another inside it, so a week credited `n` times is `n + 1` subscription intervals — two for the ordinary single credit. `--weeks N` counts intervals, which means a credited week consumes `n + 1` of the N slots and the range reaches less far back in calendar time than N times seven days. That is deliberate: a segment IS a billing cycle, so counting segments is counting cycles, and it is what [`report`](report.md) and [`weekly`](weekly.md) already render for the same week. `weeksInRange` and `rangeStart` both reflect the segment count.

Every segment resolves its own percentage inside its own half-open interval, so `project` reports the same per-cycle figures as [`weekly`](weekly.md) on the same store. A cycle with no observation of its own is reported as missing rather than given a neighbour's reading, which means a week credited `n` times can report up to `n + 1` independently missing observations. `totals.usedPercent` sums every cycle, because a billing cycle is the unit that owns a 100% quota and this surface already sums one cycle per week across weeks; a credited week therefore contributes one percentage per cycle.

## Claude `Used %`

`Used %` is a project's **modelled weekly quota** wherever the fitted
calibration `cctally quota` writes can be applied to the week, and the older
cost share otherwise. The two are computed differently and the payload says
which one you are reading.

**Modelled quota.** For a single subscription week *W* and project *P*:

    attributed_pct[P, W] = weighted_units[P, W] / units_per_point[W]

`weighted_units` is the same per-request weighting `cctally quota` fits, run
on each entry BEFORE it is aggregated into a project bucket — the aggregate
drops the one-hour cache-write split the weighting needs. It needs no meter
reading and no cost denominator at all.

A week is modelled only when every entry in it falls inside one S1 metering
regime and the week's whole account population passes that regime's
composition support test. A week that fails either falls back for the **whole
week**: partial-week mixtures are not produced. Support is tested over the
account-week population, never per project.

**The cost share** is the fallback, and is what every install without a fitted
calibration reports:

    attributed_pct[P, W] = (cost[P, W] / total_cost[W]) * weekly_percent[W]

Where `total_cost[W]` is the sum across **all** entries in the week (not
affected by `--project` / `--model` filters — the denominator stays
invariant). This proxy over-credits a cache-heavy project and under-credits an
output-heavy Opus one, because cache reads are far cheaper per token than
output under the model's weights. That is the reason the modelled basis
exists.

Over a multi-week range, per-week attributions sum: three weeks of 20% →
`60.0% (3cy)`. The `(Ncy)` suffix makes this explicit, and it counts billing cycles rather than calendar weeks, because a credited week contributes more than one. `—` in the `Used %`
column means the week had no `weekly_usage_snapshots` row (usually: very fresh
install), or that modelled quota was withheld. `$/1%` is
`cost / attributed_pct`.

**`--account` is required on a decorated multi-account install.** A run
without `--account` is merged, and no valid merged calibration exists, so
modelled quota is withheld with the cause `account-not-resolved` rather than
falling back to the cost share — falling back would keep publishing a cost
share under a column that says quota. At a single real account nothing
decorates and the merged path is the account path, so this affects only
genuinely multi-account installs.

## `--json` schema 2

`project --json` carries `schemaVersion: 2`. `attributedUsedPercent` and
`costPerPercent` keep their spellings and change their meaning, from the cost
share to modelled quota, which the CLI contract classifies as breaking on its
own.

The additive keys:

- `attribution.basis` — `modelled`, `cost-share` or `withheld` for the run as
  a whole. The run reports the weakest basis any contributing week reached.
- `attribution.cause` — the typed reason modelled quota was not reached:
  `account-not-resolved`, `calibration-absent`, `regime-boundary`,
  `unsupported-composition` or `no-local-history`. `null` when the basis is
  `modelled`.
- `attribution.totals` — four distinct quantities, which are not the same
  number and are never conflated: `modelledWeekPoints` (every local entry in
  the window's MODELLED weeks, weighted and converted), `visibleRowPoints`
  (the subset the rendered rows carry), `filteredOrUnmodelledPoints` (exactly
  the difference between the two), and `observedMinusModelledPoints` (the
  meter's reading minus the first). A week that fell back to the cost share
  contributes to none of the three point figures, because its points are not
  comparable to a modelled quantity; a run holding one withholds the residual
  outright. The residual is stated only when the account, the window and the
  population align — a single resolved account, a range that covers every
  subscription week it touches whole, and no fallback or filter splitting the
  population — and `residualCause` names the cause otherwise. That cause is
  one of three, and they are separate conditions rather than three names for
  one: `population-misaligned` when the account, the window or a filter split
  the population; `observed-absent` when at least one modelled week carries no
  meter snapshot, so there is no observed side to subtract from; and
  `no-modelled-weeks` when the window modelled no subscription week at all.
  The last two are not misalignments — the population is whole and one operand
  simply is not there — and reporting them as misalignment told a user their
  window or filters were at fault when neither was. **The residual is not an
  estimate of off-machine usage**, and it has been measured with both signs,
  so no consumer may assume a direction.
- `projects[].attributionBasis` — the same three-member vocabulary per row. A
  project spanning weeks that resolved differently reports `cost-share`.

A v1 consumer that read `attributedUsedPercent` as a cost share must read
`attribution.basis` to know which measure it holds. On an install with no
fitted calibration the basis is `cost-share` and the value is unchanged.

The terminal table states the same four quantities in a footer under it, so a
terminal user gets the reconciliation a `--json` consumer already had. The
footer prints the residual with the sign it was measured with and says
outright that the difference is not an identification or an estimate of usage
from another machine; it never states a direction.

"Whole subscription weeks" means the requested range covers every
subscription week it touches from that week's start to its end. A range like
`--since 2026-06-03 --until 2026-06-05` slices one week, so the meter reading
covers seven days while the modelled population covers three, and the
residual is withheld. The OPEN week is covered whole by a range running to
now, because the meter reading and the local entries both stop there.

## Claude `Cost Share`

`Cost Share` is each project's percentage of the total cost of the projects the table lists, and the table states that total and that project count in a line under it, because a terminal has no hover for the disclosure the dashboard makes in a tooltip.

Its denominator is not the one `Used %` uses, and the two columns therefore answer different questions. `Cost Share` is a share of the listed spend and always sums to 100% across the parent rows. `Used %` is a share of the account's weekly quota, so it sums to the account's quota consumption instead. A `--project` or `--model` filter narrows the set of listed projects, and `Cost Share` is measured over that narrowed set, which is exactly what the line under the table names; `Used %` keeps the invariant all-entries denominator described above.

Model rows under `--breakdown` leave `Cost Share` blank, for the same reason they leave `Used %` and `$/1%` blank: a model's share of its own project is a different denominator from the one the column names. `Cost Share` is a terminal column only; `--json` consumers compute the same figure from `projects[].costUsd`.

## Claude grouping

`git-root` (default) walks from each entry's `cwd` upward looking for a
`.git` (file or dir) and buckets by that path. Non-git directories
(`~/tmp/scratch`, `~`) fall back with a dimmed `(no-git)` suffix.
Entries whose `project_path` hasn't been captured yet (lazy-backfill
race) bucket as `(unknown)` and produce a stderr warning pointing to
`cache-sync`.

When two distinct git-roots share a basename (e.g. `~/repos/foo` and
`~/forks/foo`), the display label is disambiguated by appending the
parent segment: `foo (repos)` vs `foo (forks)`.

## Output

Claude terminal output is a ccusage-style ANSI table matching the shape of
`session` / `daily`:

    Claude Token Usage Report - Projects (2026-04-13 — 2026-04-19)
    ... (12 columns: Project, Sessions, First Seen, Last Seen,
         Input, Cache Create, Cache Read, Output, Cost (USD),
         Cost Share, Used %, $/1%)

`--json` emits a payload with `rangeStart`, `rangeEnd`, `weeksInRange`,
`groupMode`, `totals.{costUsd,usedPercent,weeklyAttributionAvailable}`,
`projects[]`, and `warnings[]`.

Codex output uses source-native project identities, cost, and token fields; it
does not invent Claude `Used %` or `$/1%` values.

For a direct Codex request, the outer envelope is
`schemaVersion, source, status, data, warnings`; `status` is `ok`, `empty`,
`partial`, or `unavailable`. A qualified-metadata failure is an explicit
`unavailable` Codex envelope and exit 3. In `--source all`, `sources[]` always
contains Claude then Codex; an unavailable Codex project block remains visible
beside an available Claude block. The all-source wrapper may add only the
compatible physical `costUsd` and `totalTokens` summary—never a blended project
percentage or quota.

## Exit codes

- `0` — success.
- `2` — invalid flag combination or bad input (e.g. `--weeks` together with
  `--since`/`--until`; `--until < --since`; an unparseable `--since`/`--until`).
  Changed from `1` in the #279 contract cleanup: `project` is a cctally-native
  command and now exits `2` on usage/validation errors like the rest of the
  native family (`diff`, `budget`, `forecast`, …). See `docs/cli-contract.md`.

## See also

- `session` — rollup by Claude sessionId
- `daily` / `weekly` / `monthly` — rollup by time bucket
- `report` — weekly $/1% trend with retrospective snapshot cost
- `forecast` — will I cap this week?


## Shareable output

`cctally project` accepts `--format {md,html,svg}` and related flags for shareable artifacts. See [share.md](share.md) for the full flag reference.
