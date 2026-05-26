# `claude`

Claude-source usage reports under a hierarchical subgroup. Drop-in for
`ccusage claude <cmd>` — paste a `ccusage claude …` command verbatim and it
runs offline.

> Each leaf shares its engine with the matching flat `cctally <cmd>` alias —
> the output (table / `--json` / exit code) is identical. The flat forms
> (`cctally daily`, …) remain fully supported as back-compat aliases; the
> subgroup form is the canonical, going-forward syntax.

## Synopsis

```
cctally claude <command> [flags]

<command> ∈ { daily, monthly, weekly, session, blocks }
```

## Subcommands

| Subcommand | Same engine as | Page |
| --- | --- | --- |
| `cctally claude daily` | `cctally daily` | [daily.md](daily.md) |
| `cctally claude monthly` | `cctally monthly` | [monthly.md](monthly.md) |
| `cctally claude weekly` | `cctally weekly` | [weekly.md](weekly.md) |
| `cctally claude session` | `cctally session` | [session.md](session.md) |
| `cctally claude blocks` | `cctally blocks` | [blocks.md](blocks.md) |

Every flag, exit code, and output shape is exactly that of the linked flat
page — there are no behavior differences. See each page for the full option
reference.

## Examples

```bash
cctally claude daily --since 2026-05-01
cctally claude blocks
cctally claude weekly --json
cctally claude session --breakdown
```

## Notes

- The subgroup mirrors ccusage's `claude` surface only. cctally-only Claude
  commands (`five-hour-blocks`, `project`, `diff`, `range-cost`,
  `cache-report`, `report`, `forecast`, `percent-breakdown`) are not added
  under `claude` — invoke them at the top level.
- Bare `cctally claude` (no subcommand) exits non-zero with a
  command-required error.
- No runtime deprecation warning is emitted by the flat forms; they are
  non-canonical, not deprecated.

## See also

- [`codex`](codex.md) — the Codex-source subgroup (`ccusage codex …`)
- The flat aliases: [`daily`](daily.md), [`monthly`](monthly.md),
  [`weekly`](weekly.md), [`session`](session.md), [`blocks`](blocks.md)
