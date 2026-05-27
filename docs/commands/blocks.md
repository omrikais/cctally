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
3. **Block projection uses the real reset start, not entry-span.** cctally
   projects `tokens ÷ (now − real reset start) × full 5h window`; ccusage uses
   `current + (tokens ÷ (last entry − first entry)) × time remaining`
   (`projectBlockUsage`). cctally's model leverages the API-anchored window
   start that ccusage only guesses, so the `Projected Usage` numbers differ by
   design. The `-a` box and the table's PROJECTED footer both reflect this
   formula.

## Synopsis

```
cctally blocks
    [-s YYYYMMDD] [-u YYYYMMDD]
    [-a] [-r] [-t N|max] [-n N]
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
| `-a, --active` | Show only the active block, rendered as the multi-line "Current Session Block Status" box (burn rate + projection + optional token-limit status). Drop-in for `ccusage blocks -a`. When no block is active, prints `No active session block found.` to stdout and exits 0 (JSON: `{"blocks": [], "message": "No active block"}`). |
| `-r, --recent` | Keep only blocks from the last 3 days, plus the active block. Drop-in for `ccusage blocks -r`. |
| `-t, --token-limit N\|max` | Token limit for the quota `%` column / projection warnings. An integer keys the `%`/REMAINING/PROJECTED surface (and the `-a` box's Token Limit Status) to that explicit value — even with no completed history; `max` (the default when `-t` is omitted) derives the limit from the largest completed block. |
| `-n, --session-length N` | Accepted for ccusage drop-in compat but a **no-op** — cctally blocks follow Anthropic's real 5-hour resets and are not re-sizable. A value `<= 0` is rejected (exit 1), mirroring ccusage's "Session length must be a positive number". |
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
cctally blocks -a                 # just the active block, as a detail box
cctally blocks -r                 # last 3 days + active
cctally blocks -t 500000          # %/REMAINING/PROJECTED keyed to 500k tokens
cctally blocks -a -t 1200000      # active box with a Token Limit Status block
```

## Active block (`-a`)

`-a/--active` filters to the single live block and renders it as a detail box
instead of a table row, mirroring `ccusage blocks -a`:

```
 ╭────────────────────────────────╮
 │                                │
 │  Current Session Block Status  │
 │                                │
 ╰────────────────────────────────╯
Block Started: 2026-05-27, 6:50:00 p.m. (2h 15m ago)
Time Remaining: 2h 45m

Current Usage:
  Input Tokens:     12,400
  Output Tokens:    48,200
  Total Cost:       $18.42

Burn Rate:
  Tokens/minute:    9,300
  Cost/hour:        $8.19

Projected Usage (if current rate continues):
  Total Tokens:     2,000,000
  Total Cost:       $41.10

Token Limit Status:
  Limit:            1,200,000 tokens
  Current Usage:    1,090,000 (90.8%)
  Remaining:        110,000 tokens
  Projected Usage:  166.7% EXCEEDS LIMIT
```

- **Burn Rate** / **Projected Usage** sections appear only when the block has a
  burn rate / projection (a brand-new block with zero elapsed time has
  neither). The projection uses cctally's real-reset formula (improvement #3
  above), so its numbers differ from ccusage by design.
- **Token Limit Status** appears only when `-t` is passed explicitly (any
  value, incl. `max`) and resolves to a positive limit. Without `-t`, the
  sub-block is omitted. Status colors: `> 100%` red `EXCEEDS LIMIT`, `> 80%`
  yellow `WARNING`, else green `OK`.
- **Heuristic active block.** If no Anthropic 5h reset was recorded for the
  live window, the block start is approximate: the `Block Started` line is
  `~`-prefixed and a legend
  (`~ = approximate start (no Anthropic 5h reset recorded for this window)`)
  is appended below `Time Remaining` — the box equivalent of the table's `~`
  prefix + footer legend.

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
- **Auto-derived token limit.** When `-t` is omitted (or `-t max`) and a
  completed block supplies a baseline, `blocks` prints
  `Using max tokens from previous sessions: N` to stdout before the table
  (matching ccusage's `logger.info`). This line is suppressed under `--json`.
- **`-n` is a documented no-op.** cctally blocks are anchored to Anthropic's
  real 5-hour resets and are not re-sizable, so a numeric `-n` value is
  accepted (for drop-in compat) and ignored. Only `-n <= 0` is an error
  (exit 1).
- `--json` output is a superset of upstream `ccusage blocks --json`:
  non-gap blocks include an additive `"anchor": "recorded" | "heuristic"`
  field indicating which path produced the start time. Gap blocks omit
  the field. With an explicit positive `-t`, each active block carrying a
  projection also gains an additive
  `"tokenLimitStatus": {"limit", "projectedUsage", "percentUsed", "status"}`
  key (`status ∈ "exceeds" | "warning" | "ok"`). All other keys match
  upstream.

## See also

- [`daily`](daily.md) — same data, calendar-day buckets
- [`five-hour-blocks`](five-hour-blocks.md) — API-anchored analytics view (no heuristic rows)
- [`record-usage`](record-usage.md) — supplies the anchor that defines block boundaries
