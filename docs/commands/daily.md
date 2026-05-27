# `daily`

Claude usage grouped by date. Drop-in replacement for `ccusage daily`,
offline.

> Canonical form: [`cctally claude daily`](claude.md) (this flat form remains as an alias).

## Synopsis

```
cctally daily
    [-s YYYYMMDD] [-u YYYYMMDD]
    [-b] [-o {asc,desc}]
    [-m {auto,calculate,display}]
    [-i] [-p PATTERN ...] [--project-aliases PAIRS]
    [--json]
```

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYYMMDD` | Filter from date (inclusive). |
| `-u, --until YYYYMMDD` | Filter until date (inclusive). |
| `-b, --breakdown` | Show per-model cost breakdown sub-rows. |
| `-o, --order {asc,desc}` | Sort direction by date (default `asc`). |
| `-m, --mode {auto,calculate,display}` | Cost source (drop-in for `ccusage daily --mode`). `auto` (default) uses the recorded `costUSD` from JSONL when present, else computes from embedded pricing — this is the pre-Session-C behavior. `calculate` always computes from embedded pricing, ignoring any recorded `costUSD`. `display` shows the recorded `costUSD` only, rendering `$0.00` when a row has none (ccusage-faithful). Most modern Claude Code JSONL omits `costUSD`, so under `display` near-everything reports `$0`. |
| `-i, --instances` | Group the report by **project** (git-root). Renders a section layout — a cyan `Project: <label>` header per project, that project's daily rows beneath, then one global `Total`. JSON becomes `{projects: {...}, totals}`. Drop-in for `ccusage daily --instances`. |
| `-p, --project PATTERN` | Filter to projects matching `PATTERN` — a **case-insensitive substring** of the project label *or* its underlying path. **Repeatable** (`-p a -p b`), with **OR** semantics. Without `-i`, the matches are merged into the normal date-aggregated output (no sections). |
| `--project-aliases PAIRS` | Comma-separated `key=Label` pairs overriding project display labels (e.g. `lib=Library,cctally-dev=Tracker`). The `key` is matched against a project's label (git-root basename), git-root path, or bucket path. **Display-only** — the `--json` `projects` keys are never aliased. |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | Output JSON matching `ccusage daily` format. Under `-i` the shape is `{projects: {...}, totals}` (see [Project axis](#project-axis---instances----project)). |

## Examples

```bash
cctally daily --since 20260414
cctally daily --since 20260410 --until 20260416
cctally daily --since 20260414 --breakdown
cctally daily --since 20260414 --json
cctally daily --order desc
cctally daily --instances                       # group by project (git-root)
cctally daily --instances --json                # {projects: {...}, totals}
cctally daily --instances --breakdown           # per-model rows inside each section
cctally daily -p tally -p web                   # filter (substring, repeatable, OR)
cctally daily --instances --project-aliases cctally-dev=Tracker
```

## Project axis (`--instances` / `--project`)

`-i/--instances`, `-p/--project`, and `--project-aliases` add a **project
dimension** to `daily` (only — `monthly`/`weekly` do not carry them, matching
upstream ccusage). They also work under the canonical subgroup form
`cctally claude daily`.

### `-i/--instances` — section layout

Each project (resolved to its **git-root**) becomes a section: a cyan
`Project: <label>` header, that project's daily rows, with one global `Total`
across all projects at the bottom. Sections are ordered by **cost descending**
(ties by label ascending). `--breakdown` still nests per-model sub-rows under
each date *within* a section; `--order desc` reverses the dates within each
section (the section order stays cost-descending).

```
┌─────────────────────────┬───┬ … ┬────────────┐
│ Date                    │ … │   │   Cost (USD)│
├─────────────────────────┼───┼ … ┼────────────┤
│ Project: app (personal) │   │   │            │
│ 2026-05-20              │ … │   │     $18.00  │
│ Project: app (work)     │   │   │            │
│ 2026-05-20              │ … │   │      $3.00  │
│ 2026-05-21              │ … │   │      $2.10  │
│ Project: (unknown)      │   │   │            │
│ 2026-05-20              │ … │   │      $0.60  │
│ Project: lib            │   │   │            │
│ 2026-05-20              │ … │   │      $0.40  │
│ Total                   │ … │   │     $24.10  │
└─────────────────────────┴───┴ … ┴────────────┘
```

Under `--json`, `-i` emits the dual shape:

```json
{
  "projects": {
    "app (personal)": [ { "date": "2026-05-20", … "totalCost": 18.0, … } ],
    "app (work)":     [ { "date": "2026-05-20", … }, { "date": "2026-05-21", … } ],
    "(unknown)":      [ … ],
    "lib":            [ … ]
  },
  "totals": { "inputTokens": …, "totalCost": 24.1, "totalTokens": … }
}
```

Each per-row dict is byte-identical in field set/order to a default `{daily}`
row; `totals` is the same field set summed across all projects. JSON keys
preserve the cost-descending section order.

### `-p/--project` — filter

`-p` filters to projects whose label or underlying path contains `PATTERN`
(case-insensitive substring), and is repeatable with OR semantics. With `-i`
it scopes the sections; **without** `-i` it merges all matching projects back
into the normal date-aggregated `{daily}` output (no sections):

```bash
cctally daily -p lib            # only the lib project, plain daily table
cctally daily -p app --json     # both app roots, combined into date buckets
```

### Divergences from upstream ccusage

cctally's project model is git-root-native rather than upstream's raw encoded
`~/.claude/projects/` dir name, so several behaviors are deliberate supersets:

- **Git-root grouping with basename-collision disambiguation.** Projects are
  grouped by their git-root (via the same resolver `cctally project` uses), not
  the raw encoded dir. Two distinct git-roots that share a basename stay
  separate sections / JSON keys, disambiguated by their parent segment:
  `app (work)` and `app (personal)`. If two roots share *both* basename and
  parent (`/a/x/app` + `/b/x/app` → both `app (x)`), the JSON key of the second
  gets a `(#2)` counter suffix so keys never collide / silently overwrite.
- **`-p` is substring + repeatable** (OR), where upstream's `-p` is a single
  exact-string match. A single `-p foo` behaves exactly like ccusage.
- **`-p`-alone has no per-row `project` field.** Upstream's exact single-`-p`
  adds `"project": <name>` to each `{daily}` row; cctally's `-p` can match
  multiple projects, so a single per-row identity is ill-defined — `-p`-alone
  JSON stays plain `{daily, totals}`. Per-project identity lives in the
  `{projects}` map under `-i`.
- **`-i` always emits `{projects}`.** Null-`project_path` entries collect under
  an `(unknown)` bucket, so cctally never falls back to upstream's `{daily}`
  shape when no row has a project.
- **`--project-aliases` matches the cctally label / path and is table-only.**
  The alias `key` is matched against a project's label (git-root basename),
  git-root, or bucket path — not upstream's `parseProjectName`-cleaned encoded
  dir. The `--json` `projects` keys are never aliased.
- **Under `--format` (shareable output): `-i` is a no-op, but `-p` is
  honored.** Project-section grouping has no share equivalent (the daily share
  artifact is a headline cost trend, like `--breakdown` is also ignored), so
  `-i` is dropped. But `-p` is a data-scope filter and survives into the
  artifact: `cctally daily -p lib --format svg` produces a lib-only artifact.

## Notes

- Cost is recomputed on every read from `CLAUDE_MODEL_PRICING` against
  `cache.db`'s `session_entries` rows. Pricing-dict updates take effect
  immediately with no `cache-sync` needed.
- JSON output matches upstream `ccusage daily --json` shape for scripting
  parity.
- Date arguments accept `YYYYMMDD` (no separator).

## See also

- [`monthly`](monthly.md), [`weekly`](weekly.md) — coarser buckets, same data
- [`blocks`](blocks.md) — finer buckets (5-hour windows)
- [`session`](session.md) — group by `sessionId` instead of by date


## Shareable output

`cctally daily` accepts `--format {md,html,svg}` and related flags for shareable artifacts. See [share.md](share.md) for the full flag reference.
