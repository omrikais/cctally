# `codex-session`

Codex (OpenAI) usage grouped by session. Drop-in replacement for
`ccusage-codex session`, offline.

## Synopsis

```
cctally codex-session
    [-s YYYY-MM-DD] [-u YYYY-MM-DD]
    [-o {asc,desc}]
    [--json]
    [-z TZ] [-l LOCALE]
    [--compact] [--color] [--noColor]
    [-O | --offline | --no-offline]
```

> Note: no `--breakdown` flag. Sessions don't get per-model child rows
> in the upstream layout, so we don't either.

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYY-MM-DD` | Filter from date (inclusive). |
| `-u, --until YYYY-MM-DD` | Filter until date (inclusive). |
| `-o, --order {asc,desc}` | Sort direction by last activity (default `asc` — earliest first). |
| `--json` | Output JSON matching `ccusage-codex session` format. |
| `-z, --timezone TZ` | IANA timezone for date bucketing. |
| `-l, --locale LOCALE` | No-op; accepted for drop-in compat. |
| `--compact` | Force compact table layout. |
| `--color` / `--noColor` | No-op; accepted for drop-in compat. |
| `-O, --offline / --no-offline` | No-op; always offline. |

## Examples

```bash
cctally codex-session
cctally codex-session --since 20260401
cctally codex-session --json
```

## Notes

- Sort defaults to **ascending** (earliest first) — matches upstream
  `ccusage-codex session` and pairs well with terminal scrollback.
- Each row corresponds to one Codex session file at
  `~/.codex/sessions/...`. Codex doesn't have a Claude-style
  `--resume`-across-files concept, so no merging happens here.
- Same dedup, token-semantics, and unknown-model behavior as
  [`codex-daily`](codex-daily.md#notes--diverges-from-upstream-ccusage-codex-on-duplicate-events).

## See also

- [`session`](session.md) — Claude equivalent (does merge resumed sessions)
- [`codex-daily`](codex-daily.md), [`codex-monthly`](codex-monthly.md), [`codex-weekly`](codex-weekly.md)
