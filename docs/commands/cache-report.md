# `cache-report`

Claude cache diagnostics or Codex cached-input/token-reuse analytics across
days or sessions. Provider sections stay separate because their token and cache
semantics differ.

> **Claude cost coverage:** Claude dollar and token totals are
> [transcript-derived lower bounds](../claude-cost-coverage.md), not exact
> `/usage` billing totals. Codex accounting is unaffected.

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
- **Net $** — `Saved – Wasted`; more than `1e-9` USD below zero means caching is costing you
- **Anomaly glyph (⚠)** — Net $ is more than `1e-9` USD below zero, or Cache % drops ≥15pp vs. the trailing median
- **Eval** — which of the four evaluation states the row is in (see [Evaluation states](#evaluation-states))

Claude financial fields use each retained response's effective
`message.usage.speed`. Current Opus 5/4.8 fast rows use the 2x fast rate;
historical retained Opus 4.6/4.7 fast rows use 6x. Cache reads and both
cache-write TTLs stack on that effective base rate, so Total, Saved, Wasted,
and Net remain internally consistent. Standard, missing, malformed, unsupported,
and recorded-cost rows keep their existing behavior; Claude has no inferred or
user-selected speed flag.

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
| `--json` | Machine-readable JSON. Claude row anomaly objects include `triggered`, `reasons`, and `unevaluated`; the additive `unevaluated` key keeps `schemaVersion: 1`. |
| `--anomaly-threshold-pp PP` | Claude Cache% drop threshold for the `cache_drop` trigger. Default `15`. This flag does **not** read `config.json` — see [Anomaly threshold sources](#anomaly-threshold-sources). |
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

### Anomaly threshold sources

There are two thresholds with the same meaning and no connection between
them, and this is deliberate rather than an oversight.

- **This command** uses `--anomaly-threshold-pp`, whose default is `15`.
  `cctally cache-report` never reads `config.json`, so a stored value has no
  effect here. Pass the flag to change it for one invocation.
- **The dashboard and the TUI** read
  `config.json`'s `cache_report.anomaly_threshold_pp`, which the dashboard's
  cache-report settings write through `POST /api/settings`.

So a threshold you set in the dashboard changes what the dashboard and the
TUI flag, and changes nothing about what this command prints. The stored key
is also not a `cctally config set` key: it is absent from the
[allowed-keys table](config.md#allowed-keys) for that reason.

- **Effectively-zero Net $ is not anomalous.** The `net_negative` trigger uses
  the repository-wide `1e-9` USD tolerance, so a correctly rounded floating
  residue between `-1e-9` and zero does not raise a warning. A value below
  `-1e-9` still does.

### Evaluation states

Every data row carries one of four states in the **Eval** column. Before this
column existed the terminal marked only triggered rows, so an unmarked row
could mean three different things and the reader had no way to tell which.

| Eval | Meaning |
|---|---|
| `anomaly` | At least one predicate triggered. The row also keeps its red ⚠ glyph and its coloured reason cells. |
| `clear` | Every applicable predicate ran and none triggered. |
| `partial` | Some applicable predicates ran and at least one could not be evaluated — most often `cache_drop` on a thin baseline. |
| `not eval` | No applicable predicate could be evaluated for this row. |

`anomaly` takes precedence over the other three: a row whose `net_negative`
triggered is reported as an anomaly even when `cache_drop` beside it could not
be evaluated.

Which predicates are *applicable* depends on the provider. Claude can evaluate
both `net_negative` and `cache_drop`. Codex can only ever evaluate
`cache_drop`, so a Codex row where that one predicate was skipped is
`not eval` rather than `partial` — not applicable is not the same as
unevaluated.

The same four states drive the dashboard's Cache Report panel. The two
implementations are pinned to each other by a shared truth table, so the
terminal and the dashboard cannot disagree about a row.

`--json` is unchanged: it has published `anomaly.triggered`,
`anomaly.reasons` and `anomaly.unevaluated` since the state was first
distinguished, and the column above is derived from exactly those fields.

**Terminal width.** The **Eval** column is a twelfth column, and at the
default 120-column width the daily table has no room for it alongside every
existing column, so the layout's ultra-compact fallback drops the **Input**
column — the same fallback the by-session table has always used at that width.
Widen your terminal to see both, or read `--json`, which carries every field
regardless of width.

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

- **Anomaly baseline skips when samples are thin.** The
  `cache_drop` trigger needs ≥5 daily rows or ≥10 session rows in the
  trailing `--anomaly-window-days` window. With fewer samples, the trigger
  is skipped to avoid first-two-weeks false positives. The skip itself has
  not changed; what changed is that the terminal now says the skip happened
  (`partial` or `not eval` in the **Eval** column) instead of leaving the row
  indistinguishable from an evaluated-clean one. Widen `--days`, or inspect
  both lists with
  `--days N --json | jq '.days[].anomaly | {reasons, unevaluated}'`.
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
