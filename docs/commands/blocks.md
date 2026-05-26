# `blocks`

Claude usage grouped by 5-hour session block.

> Canonical form: [`cctally claude blocks`](claude.md) (this flat form remains as an alias).

`cctally blocks` aims for parity with `ccusage blocks`'s output shape but
implements two intentional improvements over upstream:

1. **5h block boundaries follow Anthropic's `rate_limits.5h.resets_at`**
   (10-minute granularity), not the legacy hour-floor algorithm. A first
   message at 7:10 PM anchors the block at 7:10, not 7:00. ccusage still
   uses `floor_to_hour` (`rust/crates/ccusage/src/blocks.rs:51-54`).
2. **Duplicate-pair dedup matches ccusage's `should_replace_deduped_entry`**
   (`rust/crates/ccusage/src/claude_loader.rs:531`) — higher-token-total
   wins, `speed`-set breaks ties. Fixed in v1.12.0; pre-fix versions kept
   the streaming-intermediate row and systematically under-counted output
   tokens on tool-using turns.

## Synopsis

```
cctally blocks
    [-s YYYYMMDD] [-u YYYYMMDD]
    [-b] [-m {auto,calculate,display}] [--json]
```

## Purpose

5-hour blocks are how Anthropic meters short-window usage; this view
makes them visible without round-tripping to `ccusage`'s network calls.

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYYMMDD` | Filter from date (inclusive). |
| `-u, --until YYYYMMDD` | Filter until date (inclusive). |
| `-b, --breakdown` | Show per-model cost breakdown. |
| `-m, --mode {auto,calculate,display}` | Cost source (drop-in for `ccusage blocks --mode`). `auto` (default) uses the recorded `costUSD` from JSONL when present, else computes from embedded pricing — this is the pre-Session-C behavior. `calculate` always computes from embedded pricing, ignoring any recorded `costUSD`. `display` shows the recorded `costUSD` only, rendering `$0.00` when an entry has none (ccusage-faithful). The mode is honored on both the main grouping AND the active canonical-swapped block. Most modern Claude Code JSONL omits `costUSD`, so under `display` near-everything reports `$0`. (Note: `cctally five-hour-blocks` accepts `--mode` only as a no-op — see that page.) |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). Note: `--since`/`--until` keep upstream-parity host-local parsing (documented divergence). |
| `--json` | Output JSON matching `ccusage blocks` format. |

## Examples

```bash
cctally blocks --since 20260414
cctally blocks --since 20260410 --until 20260416
cctally blocks --since 20260414 --breakdown
cctally blocks --since 20260414 --json
```

## Notes

- Each 5-hour window is anchored to Anthropic's real
  `rate_limits.5h.resets_at` value (recorded by `record-usage` into
  `weekly_usage_snapshots`) when a snapshot exists for that window.
- When no recorded snapshot covers a window, the start time falls back
  to the heuristic `floor(first_cc_entry_in_window) + 5h`. Heuristic
  rows are flagged with a `~` prefix on the start time, and a footer
  legend (`~ = approximate start...`) appears at the bottom of the
  table when any heuristic rows are present. The bucket may drift
  from the real Anthropic boundary by up to ~5h on heuristic rows
  (e.g., a window initiated via claude.ai web or the API while
  `record-usage` wasn't running).
- `--breakdown` adds child rows per model under each parent block.
- `--json` output is a superset of upstream `ccusage blocks --json`:
  non-gap blocks include an additive `"anchor": "recorded" | "heuristic"`
  field indicating which path produced the start time. Gap blocks omit
  the field. All other keys match upstream.

## See also

- [`daily`](daily.md) — same data, calendar-day buckets
- [`five-hour-blocks`](five-hour-blocks.md) — API-anchored analytics view (no heuristic rows)
- [`record-usage`](record-usage.md) — supplies the anchor that defines block boundaries
