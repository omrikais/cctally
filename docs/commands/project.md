# project

Roll Claude or Codex usage up by project. Claude has per-project `Used %`
against a subscription-week ceiling; Codex has calendar-week cost and token
rollups without a fabricated `Used %`. In `--source all`, provider sections
stay separate and `--weeks` resolves one absolute calendar range.

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
Codex cannot calculate Claude-style `--sort used`, so that value is rejected
for Codex and all-source requests. Other project filters, grouping, ordering,
breakdown, timezone, and share flags apply inside each source section.

## Claude `Used %`

For a single subscription week *W* and project *P*:

    attributed_pct[P, W] = (cost[P, W] / total_cost[W]) * weekly_percent[W]

Where `total_cost[W]` is the sum across **all** entries in the week (not
affected by `--project` / `--model` filters — the denominator stays
invariant). Over a multi-week range, per-week attributions sum: three
weeks of 20% → `60.0% (3wk)`. The `(Nwk)` suffix makes this explicit.
`—` in the `Used %` column means the week had no
`weekly_usage_snapshots` row (usually: very fresh install). `$/1%` is
`cost / attributed_pct`.

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
    ... (11 columns: Project, Sessions, First Seen, Last Seen,
         Input, Cache Create, Cache Read, Output, Cost (USD),
         Used %, $/1%)

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
