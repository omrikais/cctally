# `codex-daily`

Codex (OpenAI) usage grouped by date. Drop-in replacement for
upstream [`ccusage-codex`](../../README.md#acknowledgments) `daily`,
offline.

> Canonical form: [`cctally codex daily`](codex.md) (this flat form remains as an alias).

## Synopsis

```
cctally codex-daily
    [-s YYYY-MM-DD] [-u YYYY-MM-DD]
    [-b] [-o {asc,desc}]
    [--speed {auto,standard,fast}]
    [--json]
    [-z TZ] [-l LOCALE]
    [--compact] [--color] [--noColor]
    [-O | --offline | --no-offline]
    [-d | --debug] [--debug-samples N]
```

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYY-MM-DD` | Filter from date (inclusive; `YYYY-MM-DD` or `YYYYMMDD`). |
| `-u, --until YYYY-MM-DD` | Filter until date (inclusive). |
| `-b, --breakdown` | Show per-model cost breakdown sub-rows. |
| `-o, --order {asc,desc}` | Sort direction (default `asc`). |
| `--speed {auto,standard,fast}` | Codex pricing tier. `auto` (default) reads `service_tier` from `~/.codex/config.toml`; `fast`\|`priority` there selects fast-tier pricing. `fast`/`standard` force the tier. |
| `--json` | Output JSON matching `ccusage-codex daily` format. |
| `-z, --timezone TZ` | IANA timezone for date bucketing and Date / Last Activity cells. |
| `-l, --locale LOCALE` | No-op; accepted for drop-in compat. |
| `--compact` | Force compact table layout regardless of terminal width. |
| `--color` / `--noColor` | No-op; accepted for drop-in compat (no ANSI emitted). |
| `-O, --offline / --no-offline` | No-op; accepted for drop-in compat (always offline). |
| `-d, --debug` | Emit a stderr "Codex Pricing Debug Report" (totals + the N highest computed-cost sample entries). See [Pricing debug report](#pricing-debug-report---debug). |
| `--debug-samples N` | Cap on top-entry sample rows in the `--debug` report (default 5; `N=0` suppresses the sample block; negatives rejected at parse time). |

## Examples

```bash
cctally codex-daily --since 20260401
cctally codex-daily --since 20260401 --breakdown
cctally codex-daily --since 20260401 --json
cctally codex-daily --order desc
```

## Pricing tier (`--speed`)

`--speed` selects the Codex cost tier, matching `ccusage codex --speed`:

- `auto` (default) — scans `~/.codex/config.toml`; if any `service_tier = "fast"` or `service_tier = "priority"` line is present, fast-tier pricing applies, otherwise standard.
- `fast` — force fast-tier pricing.
- `standard` — force base pricing.

Fast-tier multiplies the per-model cost by a fixed factor: `gpt-5.5` ×2.5, all
other Codex models ×2.0. Detection is a line-scan (a `service_tier` line in any
table counts). `--json` gains no new field — only the `costUSD` figures reflect
the tier.

> `--speed` is a cctally extension on the flat `codex-*` form — the standalone
> `ccusage-codex` binary has no `--speed`. The canonical `cctally codex <cmd>`
> subgroup mirrors `ccusage codex <cmd>`, which does.

## Notes — diverges from upstream `ccusage-codex` on duplicate events

**Older Codex rollouts re-emit `event_msg.token_count` with the same
`last_token_usage` after UI/turn_context updates.** Upstream
`ccusage-codex` sums every emission (~2× overcount on affected
sessions). This codebase dedups by tracking
`info.total_token_usage.total_tokens` and only yielding when the
cumulative strictly advances. Result: ~50% lower numbers than upstream
on historical data, but matches the Codex CLI's own authoritative
counter. Fresh sessions don't re-emit, so new data matches upstream
byte-exactly. **Don't "fix" this back to upstream parity.**

### Token semantics (LiteLLM convention)

In Codex JSONL `last_token_usage`:

- `input_tokens` includes `cached_input_tokens`
- `output_tokens` includes `reasoning_output_tokens`

Cost formula:

```
(input - cached) * input_rate
+ cached * cache_read_rate
+ output * output_rate
```

Reasoning is **not** added separately.

### Table vs JSON divergence

Mirrors upstream: JSON `inputTokens` is inclusive of cached; the
rendered table's Input column shows non-cached (`input - cached`).

### Unknown models

Unknown Codex model names fall back to
`CODEX_LEGACY_FALLBACK_MODEL = "gpt-5"` pricing with `isFallback: true`
in JSON output. One stderr warning per unknown name per process.

### Pricing debug report (`--debug`)

`-d/--debug` writes a **Codex Pricing Debug Report** to stderr (stdout
keeps the normal table/JSON). Unlike the Claude-side `--debug` report,
Codex JSONL records no `costUSD`, so there is nothing to diff against —
the report instead lists the **N highest computed-cost entries**:

```
=== Codex Pricing Debug Report ===
Command: cctally codex-daily
Total entries processed: 1,234
Models seen: gpt-5-codex (1,200), gpt-5 (34)
Total computed cost: $12.345678

=== Sample Top Entries (first 5) ===
File: rollout-abc.jsonl
Timestamp: 2026-05-01T10:00:00+00:00
Model: gpt-5-codex
Recorded cost: (none)
Calculated cost: $0.842310
Tokens: {"input_tokens": 700, ...}
---
```

`--debug-samples N` caps the sample block (default 5; `N=0` prints only
the totals header). Models priced via the `gpt-5` fallback are tagged
`(fallback→gpt-5)` in both "Models seen" and the per-sample `Model:`
line.

## See also

- [`codex-monthly`](codex-monthly.md), [`codex-weekly`](codex-weekly.md), [`codex-session`](codex-session.md)
- [Architecture · Codex token semantics](../architecture.md#codex-token-semantics)
