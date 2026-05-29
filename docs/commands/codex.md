# `codex`

Codex (OpenAI) usage reports under a hierarchical subgroup.
`daily`/`monthly`/`session` are drop-ins for `ccusage codex <cmd>` — paste a
`ccusage codex …` command verbatim and it runs offline. `weekly` is a cctally
extension (upstream has no `ccusage codex weekly`).

> Each leaf shares its engine with the matching flat `cctally codex-<cmd>`
> alias — the output (table / `--json` / exit code) is identical. The flat
> `codex-*` forms remain fully supported as back-compat aliases (drop-ins for
> the standalone `ccusage-codex` binary); the subgroup form is the canonical,
> going-forward syntax for the hierarchical `ccusage codex …` surface.

## Synopsis

```
cctally codex <command> [flags]

<command> ∈ { daily, monthly, session, weekly }
```

## Subcommands

| Subcommand | Same engine as | Drop-in for | Page |
| --- | --- | --- | --- |
| `cctally codex daily` | `cctally codex-daily` | `ccusage codex daily` | [codex-daily.md](codex-daily.md) |
| `cctally codex monthly` | `cctally codex-monthly` | `ccusage codex monthly` | [codex-monthly.md](codex-monthly.md) |
| `cctally codex session` | `cctally codex-session` | `ccusage codex session` | [codex-session.md](codex-session.md) |
| `cctally codex weekly` | `cctally codex-weekly` | _cctally extension_ | [codex-weekly.md](codex-weekly.md) |

Every flag, exit code, and output shape is exactly that of the linked flat
page — there are no behavior differences. See each page for the full option
reference.

## Examples

```bash
cctally codex daily --since 2026-05-01
cctally codex monthly --breakdown
cctally codex session --json
cctally codex weekly
```

## Notes

- Naming convention: the flat hyphenated `codex-*` forms are drop-ins for the
  standalone `ccusage-codex` binary; the hierarchical `codex <cmd>` subgroup
  is the drop-in for upstream's `ccusage codex <cmd>` subgroup. cctally mirrors
  upstream's own dual surface one-to-one.
- `cctally codex weekly` (and the flat `cctally codex-weekly`) have no upstream
  counterpart — week-start day is read from `config.json`.
- `--speed {auto,standard,fast}` is shared across every codex leaf (it rides the
  common Codex arg helper). On the subgroup forms this is faithful to
  `ccusage codex <cmd> --speed`; on the flat `codex-*` aliases it is a cctally
  extension (the standalone `ccusage-codex` binary has no `--speed`). `auto`
  (the default) reads `service_tier` from `~/.codex/config.toml`. See any leaf
  page's "Pricing tier (`--speed`)" section for details.
- **Totals lower than `ccusage-codex`?** Expected on older sessions, and
  cctally is the accurate one — older Codex CLI versions re-emit duplicate
  `token_count` events, which `ccusage-codex` double-counts (up to ~2×) while
  cctally dedups to match Codex's own ledger. Recent sessions match byte-for-byte.
  See [codex-daily · duplicate-event divergence](codex-daily.md#notes--diverges-from-upstream-ccusage-codex-on-duplicate-events).
- Bare `cctally codex` (no subcommand) exits non-zero with a command-required
  error.
- No runtime deprecation warning is emitted by the flat forms; they are
  non-canonical, not deprecated.

## See also

- [`claude`](claude.md) — the Claude-source subgroup (`ccusage claude …`)
- The flat aliases: [`codex-daily`](codex-daily.md),
  [`codex-monthly`](codex-monthly.md), [`codex-session`](codex-session.md),
  [`codex-weekly`](codex-weekly.md)
