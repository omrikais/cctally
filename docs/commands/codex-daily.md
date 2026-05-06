# `codex-daily`

Codex (OpenAI) usage grouped by date. Drop-in replacement for
upstream [`ccusage-codex`](../../README.md#acknowledgments) `daily`,
offline.

## Synopsis

```
cctally codex-daily
    [-s YYYY-MM-DD] [-u YYYY-MM-DD]
    [-b] [-o {asc,desc}]
    [--json]
    [-z TZ] [-l LOCALE]
    [--compact] [--color] [--noColor]
    [-O | --offline | --no-offline]
```

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYY-MM-DD` | Filter from date (inclusive; `YYYY-MM-DD` or `YYYYMMDD`). |
| `-u, --until YYYY-MM-DD` | Filter until date (inclusive). |
| `-b, --breakdown` | Show per-model cost breakdown sub-rows. |
| `-o, --order {asc,desc}` | Sort direction (default `asc`). |
| `--json` | Output JSON matching `ccusage-codex daily` format. |
| `-z, --timezone TZ` | IANA timezone for date bucketing and Date / Last Activity cells. |
| `-l, --locale LOCALE` | No-op; accepted for drop-in compat. |
| `--compact` | Force compact table layout regardless of terminal width. |
| `--color` / `--noColor` | No-op; accepted for drop-in compat (no ANSI emitted). |
| `-O, --offline / --no-offline` | No-op; accepted for drop-in compat (always offline). |

## Examples

```bash
cctally codex-daily --since 20260401
cctally codex-daily --since 20260401 --breakdown
cctally codex-daily --since 20260401 --json
cctally codex-daily --order desc
```

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

## See also

- [`codex-monthly`](codex-monthly.md), [`codex-weekly`](codex-weekly.md), [`codex-session`](codex-session.md)
- [Architecture · Codex token semantics](../architecture.md#codex-token-semantics)
