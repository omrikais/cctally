# `codex-session`

Codex (OpenAI) usage grouped by session. Drop-in replacement for
`ccusage-codex session`, offline.

> Canonical form: [`cctally codex session`](codex.md) (this flat form remains as an alias).

## Synopsis

```
cctally codex-session
    [-s YYYY-MM-DD] [-u YYYY-MM-DD]
    [-o {asc,desc}]
    [--speed {auto,standard,fast}]
    [--json]
    [-z TZ] [-l LOCALE]
    [--compact] [--color] [--noColor]
    [-O | --offline | --no-offline]
    [-d | --debug] [--debug-samples N]
```

> Note: no `--breakdown` flag. Sessions don't get per-model child rows
> in the upstream layout, so we don't either.

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYY-MM-DD` | Filter from date (inclusive). |
| `-u, --until YYYY-MM-DD` | Filter until date (inclusive). |
| `-o, --order {asc,desc}` | Sort direction by last activity (default `asc` — earliest first). |
| `--speed {auto,standard,fast}` | Codex pricing tier. `auto` (default) reads `service_tier` from `~/.codex/config.toml`; `fast`\|`priority` there selects fast-tier pricing. `fast`/`standard` force the tier. |
| `--json` | Output JSON matching `ccusage-codex session` format. |
| `-z, --timezone TZ` | IANA timezone for date bucketing. |
| `-l, --locale LOCALE` | No-op; accepted for drop-in compat. |
| `--compact` | Force compact table layout. |
| `--color` / `--noColor` | No-op; accepted for drop-in compat. |
| `-O, --offline / --no-offline` | No-op; always offline. |
| `-d, --debug` | Emit a stderr "Codex Pricing Debug Report" (totals + top-N highest computed-cost entries). |
| `--debug-samples N` | Cap on top-entry sample rows (default 5; `N=0` suppresses the block). |

## Examples

```bash
cctally codex-session
cctally codex-session --since 20260401
cctally codex-session --json
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

## Notes

- Sort defaults to **ascending** (earliest first) — matches upstream
  `ccusage-codex session` and pairs well with terminal scrollback.
- Each row corresponds to one Codex session file at
  `~/.codex/sessions/...`. Codex doesn't have a Claude-style
  `--resume`-across-files concept, so no merging happens here.
- Same dedup, token-semantics, and unknown-model behavior as
  [`codex-daily`](codex-daily.md#notes--diverges-from-upstream-ccusage-codex-on-duplicate-events).
- `--debug` emits the same "Codex Pricing Debug Report" shape — see
  [Pricing debug report](codex-daily.md#pricing-debug-report---debug).

## See also

- [`session`](session.md) — Claude equivalent (does merge resumed sessions)
- [`codex-daily`](codex-daily.md), [`codex-monthly`](codex-monthly.md), [`codex-weekly`](codex-weekly.md)
