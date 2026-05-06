# project

Roll Claude usage up by project (git-root resolved), with per-project
`Used %` attribution against the subscription's weekly ceiling.
Answers the mid-week question *"which project is eating my budget?"*

## Quick examples

    # Current subscription week, sorted by cost desc
    cctally project

    # Last 2 subscription weeks
    cctally project --weeks 2

    # Drill into one project with per-model breakdown
    cctally project --project ccusage --breakdown

    # Machine-readable output
    cctally project --json

    # Or via the wrapper (after `install.sh`)
    cctally-project --weeks 2

## Flags

| Flag | Default | Description |
|---|---|---|
| `--since DATE`, `--until DATE` | — | Inclusive range (YYYY-MM-DD). |
| `--weeks N` | — | Last N subscription weeks ending at the current week. Mutually exclusive with `--since`/`--until`. |
| *(no range flags)* | current subscription week | Zero-arg default. |
| `--project PATTERN` | — | Case-insensitive substring filter on project display key. Repeatable (OR). |
| `--model PATTERN` | — | Same but for model names. Repeatable. |
| `--breakdown` | off | Parent project row + one child row per model used. |
| `--order asc\|desc` | `desc` | |
| `--sort cost\|used\|name\|last-seen` | `cost` | Ties break on display key ascending. |
| `--group git-root\|full-path` | `git-root` | `full-path` skips the walker and buckets by `realpath(project_path)` (symlink-aliased spellings collapse into one row; the displayed label is whichever spelling was seen first). |
| `--tz TZ` | config | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | off | Emit structured JSON. |
| `--no-color` | off | Disable ANSI color. |

## What `Used %` means

For a single subscription week *W* and project *P*:

    attributed_pct[P, W] = (cost[P, W] / total_cost[W]) * weekly_percent[W]

Where `total_cost[W]` is the sum across **all** entries in the week (not
affected by `--project` / `--model` filters — the denominator stays
invariant). Over a multi-week range, per-week attributions sum: three
weeks of 20% → `60.0% (3wk)`. The `(Nwk)` suffix makes this explicit.
`—` in the `Used %` column means the week had no
`weekly_usage_snapshots` row (usually: very fresh install). `$/1%` is
`cost / attributed_pct`.

## Grouping

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

Terminal output is a ccusage-style ANSI table matching the shape of
`session` / `daily`:

    Claude Token Usage Report - Projects (2026-04-13 — 2026-04-19)
    ... (11 columns: Project, Sessions, First Seen, Last Seen,
         Input, Cache Create, Cache Read, Output, Cost (USD),
         Used %, $/1%)

`--json` emits a payload with `rangeStart`, `rangeEnd`, `weeksInRange`,
`groupMode`, `totals.{costUsd,usedPercent,weeklyAttributionAvailable}`,
`projects[]`, and `warnings[]`.

## Exit codes

- `0` — success.
- `1` — invalid flag combination (e.g. `--weeks` together with
  `--since`/`--until`; `--until < --since`).

## See also

- `session` — rollup by Claude sessionId
- `daily` / `weekly` / `monthly` — rollup by time bucket
- `report` — weekly $/1% trend with retrospective snapshot cost
- `forecast` — will I cap this week?
