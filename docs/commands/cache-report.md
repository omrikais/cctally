# `cache-report`

Claude cache diagnostics or Codex cached-input/token-reuse analytics across
days or sessions. Provider sections stay separate because their token and cache
semantics differ.

## Synopsis

```
cctally cache-report
    [--days DAYS] [--since SINCE] [--until UNTIL]
    [--by-session]
    [--offline] [--project PROJECT] [--json]
    [--anomaly-threshold-pp PP] [--anomaly-window-days N] [--no-anomaly]
    [--sort {date,net,cache,recent,cost,anomaly,reuse}]
    [--source {claude,codex,all}] [--speed {auto,standard,fast}]
```

## Purpose

For Claude, surface cache behavior so a prompt-caching regression becomes
visible in days rather than dollars. For Codex, surface cached-input/token reuse
without relabeling it as Claude cache behavior.

## What it shows

**Claude cache diagnostics** show:

- **Cache %** = `cache_read_tokens / (input + cache_create + cache_read)`
- **$ Saved** — counterfactual no-cache cost minus actual cost
- **$ Wasted** — cache-write premium that did not yield enough reads
- **Net $** — `Saved – Wasted`; negative means caching is costing you
- **Anomaly glyph (⚠)** — `Net $ < 0` or Cache % drops ≥15pp vs. the trailing median

**Codex token reuse** shows inclusive input, cached input, non-cached input,
cached-input percent, output, reasoning output, and source-native cost. It has
no cache-hit rate, cache-create/read tokens, savings, waste, net, or Claude
anomaly verdict. Per-model rows remain visible for both providers.

## Options

| Flag | Description |
| --- | --- |
| `--days N` | Recent days to include (default `7`). |
| `--since` / `--until` | ISO 8601 window bounds. Override `--days` when set. |
| `--by-session` | Group by source-native session identity instead of by date. Adds identity / Last Activity / Project columns. |
| `--offline` | No-op (pricing always embedded). |
| `--project PROJECT` | Filter to a specific project. |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | Machine-readable JSON. |
| `--anomaly-threshold-pp PP` | Claude Cache% drop threshold for the `cache_drop` trigger. Default `15`. |
| `--anomaly-window-days N` | Claude trailing baseline window in days. Default `14`. |
| `--no-anomaly` | Disable Claude `cache_drop` and `net_negative` triggers. |
| `--sort` | Override the source-native sort order. `reuse` is Codex-only; `net`, `cache`, and `anomaly` are Claude-only. |
| `--source {claude,codex,all}` | Analytics provider; default `claude`. `cctally claude cache-report` and `cctally codex cache-report` are fixed-source equivalents. |
| `--speed {auto,standard,fast}` | Codex pricing tier; default `auto`. Applies to Codex/all, not a non-default Claude-only request. |

## Examples

```bash
cctally cache-report
cctally cache-report --days 14
cctally cache-report --since 2026-04-10 --until 2026-04-18
cctally cache-report --by-session --days 14
cctally cache-report --by-session --sort cache
cctally cache-report --json
cctally codex cache-report --since 2026-07-14 --until 2026-07-16 --sort reuse
cctally cache-report --source all --by-session --json
```

## Gotchas

### Codex token reuse, not a cache-hit rate

`--source codex` is a **Codex Token Reuse Report**, not a Claude cache report.
It reports inclusive input, cached input, non-cached input (`input - cached`,
floored at zero), cached-input percent (when input is positive), output,
reasoning output, and source-native cost. It has no cache-create/read tokens,
hit count/rate, savings, waste, net, or Claude anomaly verdict. In particular,
`cacheHitPercent` is deliberately not a Codex wire field.

`--sort reuse` is Codex-only and sorts descending by cached-input percent.
Codex accepts `date`, `recent`, and `cost`; `net`, `cache`, and `anomaly` are
Claude-only. `--anomaly-*` and `--no-anomaly` are rejected for Codex when
explicitly non-default, while in `--source all` they affect the Claude section
only. `--by-session` groups Codex by qualified conversation/root; all-source
output renders the Claude cache and Codex reuse sections separately.

Codex can retain truthful daily/model reuse when project metadata is missing;
the direct source becomes `partial` and marks its project-metadata section
`unavailable`. A requested Codex `--project` filter instead requires the
qualified join and returns the explicit unavailable envelope with exit 3. The
all-source form retains both source blocks and has the same exit-3 rule for that
requested filter.

- **Anomaly baseline silent-skips when samples are thin.** The
  `cache_drop` trigger needs ≥5 daily rows or ≥10 session rows in the
  trailing `--anomaly-window-days` window. With fewer samples, the trigger
  is silently skipped (no warning) — looks like a missed regression but
  is the correct behavior to avoid first-two-weeks false positives.
  Widen `--days` or inspect `--days N --json | jq '.days[].anomaly.reasons'`.
- `--since` / `--until` accept either pure-date (`2026-04-10`) or
  full-ISO (`2026-04-10T10:00:00Z`). Mixed-format same-day windows
  collapse to empty (e.g. `--since 20260418 --until 2026-04-18`) — fix:
  use the same format on both ends.
- As a `cache-report`-only convenience, any other form accepted by
  Python's `datetime.fromisoformat` also parses — notably
  space-separated datetimes (`'2026-04-10 10:00:00'`) and ISO week-dates
  (`2026-W18-1`). A full datetime carries its own time component, so it
  is used verbatim (no end-of-day rounding on `--until`). The sibling
  date commands (`daily` / `monthly` / `weekly` / `blocks`) accept only
  the two forms above.
- `--by-session` collapses Claude `--resume` chains into one row using
  the `session_files.session_id` mapping.
- Per-model child rows are always rendered. There is no flag to suppress
  them.

## See also

- [`cache-sync`](cache-sync.md) — prime the cache this command queries
- [Architecture · cache.db](../architecture.md#the-session-entry-cache-cachedb)
