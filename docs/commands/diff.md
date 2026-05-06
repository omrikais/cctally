# diff

Compare Claude usage between two windows in a single bordered-table
render. Answers *"what changed between this week and last week — and
which models, projects, and cache patterns drove the change?"*

## Quick examples

    # This subscription week vs last
    cctally diff --a this-week --b last-week

    # This calendar month vs last
    cctally diff --a this-month --b last-month

    # Rolling 7d vs the previous 7d (lengths match by construction)
    cctally diff --a last-7d --b prev-7d

    # Compare windows of different length (per-day normalization)
    cctally diff --a last-7d --b prev-14d --allow-mismatch

    # Drill into a single section (skip the others)
    cctally diff --a this-week --b last-week --only cache

    # Show every row including those filtered as noise
    cctally diff --a this-week --b last-week --all

    # Machine-readable output
    cctally diff --a this-week --b last-week --json | jq

## Window grammar

The `--a` and `--b` tokens share the same grammar:

| Form | Meaning |
|---|---|
| `this-week` | Current subscription week — start at the most-recent reset anchor, end at now. |
| `last-week` | Previous full subscription week. |
| `Nw-ago` | Subscription week N resets back from the current (`1w-ago` ≡ `last-week`). |
| `this-month` | Current calendar month in the user's local TZ. |
| `last-month` | Previous full calendar month. |
| `Nm-ago` | Calendar month N months back. |
| `last-Nd` | Trailing N days ending at now (`[now − Nd, now)`). |
| `prev-Nd` | The N-day window immediately before `last-Nd` (`[now − 2Nd, now − Nd)`). |
| `YYYY-MM-DD..YYYY-MM-DD` | Explicit inclusive date range, interpreted in local TZ. |

Subscription-week tokens fall back to the canonical 7-day-multiple
offset when no snapshot exists at the target week (same logic `weekly`
uses). If even the earliest anchor is missing, the command exits with
`diff: no subscription-week anchor available …` and exit code 1.

## Output sections

Four sections render by default; trim with `--only` (comma-separated):

1. **Overall** — total cost, tokens (input/output/cache-read/cache-write),
   Used %, Cache %. Exactly one row.
2. **Models** — same metrics per model. Asymmetric rows surface for
   models that appear in only one of the two windows.
3. **Projects** — same metrics per git-root project key (reuses the same
   `_resolve_project_root` walker `project` does).
4. **Cache** — cache-active-entries scope: per-cache-vendor row plus an
   `cache:overall` row. See "Caveats" below for what `cost_usd` means
   here.

Two opt-in sections are reserved but **deferred** in v1:

- `--with trend` — best/worst `$/1%` day per window.
- `--with time` — per-day-of-week / per-hour patterns.

Passing either today errors out with `diff: --with {trend,time} is
deferred to a follow-up release` and exit code 1.

## Asymmetric rows

When a model or project appears in only one of the two windows, it
still gets a row, tagged in the `label` column:

- `(new)` — present only in window B (`status: "new"`, `a` is `null`,
  `delta.X` carries the full B value, `delta.X_pct` is `null`).
- `(dropped)` — present only in window A (`status: "dropped"`, `b` is
  `null`, `delta.X` carries the negated A value, `delta.X_pct` is
  `null`).

The asymmetric-null shape is part of the JSON contract; consumers MUST
tolerate either side being `null`.

## Noise filter

By default, changed rows below `|Δ$| < $0.10 AND |Δ%| < 1.0` are hidden
to keep the table readable. Hidden-count footers show how many rows
were filtered:

    (3 rows hidden; |Δ$| < $0.10 AND |Δ%| < 1.0. Pass --all to show, or --min-delta to override.)

Bypass with `--all`, or override the thresholds explicitly:

    cctally diff --a this-week --b last-week --min-delta 0.50
    cctally diff --a this-week --b last-week --min-delta-pct 5

`(new)` and `(dropped)` rows are NEVER suppressed — they always appear.

## Mismatched windows

By default, comparing windows of different length is refused:

    diff: window A is 7.0 days, window B is 14.0 days; pass --allow-mismatch to compare anyway with per-day normalization

Pass `--allow-mismatch` to enable per-day normalization. Every absolute
cost and Δ$ value is divided by `length_days` before display; Δ% stays
a ratio (always meaningful); `Used %` is NOT normalized. The output
banners "(per-day normalized)" and the JSON envelope sets
`normalization: "per-day"`.

## Used % modes

`Used %` is mode-aware per window — picked from the window's
boundary alignment:

| Condition | Mode | Meaning |
|---|---|---|
| Window is one full subscription week | `exact` | Real Used % from the snapshot. |
| Window covers ≥ 2 full subscription weeks aligned to boundaries | `avg` | Average across those weeks. |
| Otherwise (e.g. ragged 7d, partial week, calendar month spanning resets) | `n/a` | `—` rendered, JSON `used_pct: null`. |

The column header in the rendered table reflects the mode (`Used %`,
`Used % (avg)`, or `Used % (—)`). The JSON envelope carries the mode
explicitly per window in `windows.{a,b}.used_pct_mode`.

## Flags

| Flag | Default | Description |
|---|---|---|
| `--a TOKEN` | required | Window A token (see grammar above). |
| `--b TOKEN` | required | Window B token. |
| `--allow-mismatch` | off | Permit mismatched window lengths; deltas normalized per-day. |
| `--only LIST` | all sections | Comma-separated subset of `overall,models,projects,cache`. |
| `--with LIST` | none | Opt-in sections (`trend,time`). Deferred — currently errors. |
| `--all` | off | Show all rows (bypass noise filter). |
| `--min-delta USD` | `0.10` | Override the `|Δ$|` noise threshold. |
| `--min-delta-pct PCT` | `1.0` | Override the `|Δ%|` noise threshold. |
| `--sort {delta,cost-a,cost-b,name,status}` | `delta` | Per-section row sort key. Ties break on `key` ascending. |
| `--top N` | unlimited | Cap rows per section after filter+sort. |
| `--sync` | off | Run `sync_cache` + `sync-week` before computing (otherwise reads cache as-is). |
| `--tz TZ` | config | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | off | Emit structured JSON envelope. |
| `--no-color` | off | Disable ANSI color. |

## Exit codes

- `0` — success.
- `1` — `NoAnchorError` (subscription-week anchor unresolvable for an
  `Nw-ago` token), or deferred `--with trend|time`.
- `2` — `WindowMismatchError` (lengths differ without
  `--allow-mismatch`), invalid window token (`ValueError`),
  range-start-after-end, or argparse errors.

Errors emit plain text on stderr (`diff: <message>`) — even with
`--json`. Consumers piping `--json | jq` should check the exit code,
not parse stdout-only.

## JSON envelope

The `--json` payload carries `schema_version: 1` and the following
top-level keys:

```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-04-25T19:30:00Z",
  "subcommand": "diff",
  "windows": {
    "a": { "label", "kind", "start_at", "end_at", "length_days",
           "week_aligned", "full_weeks_count", "used_pct_mode" },
    "b": { ... }
  },
  "mismatched_length": false,
  "normalization": "none",        // or "per-day"
  "options": { ... },             // user-passed flags + defaults
  "sections": [
    {
      "name": "overall|models|projects|cache",
      "scope": "all|cache-active-entries",
      "rows": [
        {
          "key": "...",            // stable ID (e.g. "models:claude-opus-4-7")
          "label": "...",          // display label, may carry "(new)"/"(dropped)"
          "status": "changed|new|dropped",
          "a": { /* MetricBundle */ } | null,
          "b": { /* MetricBundle */ } | null,
          "delta": { /* DeltaBundle, fields nullable per asymmetric rules */ },
          "sort_key": 6.115
        }
      ],
      "hidden_count": 0,
      "columns": [
        { "field", "header", "format", "show_in_overall" }
      ]
    }
  ]
}
```

### Stability contract

- **Stable** (consumers may rely on these): `schema_version`,
  `windows.{a,b}.{label,kind,start_at,end_at,used_pct_mode}`, every
  `sections[].rows[].{key,label,status}`, the `{a,b,delta}` row shape
  with asymmetric-null encoding, `options.*`. Adding optional keys
  does NOT bump `schema_version`. Breaking changes do.
- **NOT stable** (cosmetic / wire-format-internal):
  `sections[].columns[]` and `sort_key` semantics. Treat them as
  presentational hints.

## Caveats

- **Cache section's `cost_usd` is the full cost of cache-active
  entries**, NOT a "cache attribution" share. The JSON tags this with
  `scope: "cache-active-entries"` to make it explicit. The three
  cache-scope rows (`cache:claude`, `cache:codex`, `cache:overall`)
  are independent slices of usage, so summing them does NOT equal
  `overall.a.cost_usd`. Only the Models and Projects sections sum to
  Overall.
- **Errors don't wrap into the JSON envelope.** Even with `--json`,
  parser/window/`NoAnchorError` failures emit plain `diff: …` to
  stderr and exit non-zero. Always check the exit code first.
- **`Used %` after a mid-week reset** uses the post-reset window for
  `this-week`. A `--a this-week --b last-week` comparison with a reset
  in window A will show a smaller "this week" `Used %` reflecting
  spend since the reset, not since the original week start.
- **`--sync` is opt-in**, unlike some other subcommands. By default
  `diff` reads the existing cache; pass `--sync` if you want the
  command to refresh JSONL ingest + weekly cost snapshot before
  computing.

## See also

- `weekly` / `monthly` / `daily` — single-window rollups by time bucket.
- `project` — per-project rollup with Used % attribution.
- `report` — multi-week `$/1%` trend (use this for trend lines, not
  `--with trend`).
- `forecast` — projection: will I cap this week?
