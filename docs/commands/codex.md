# `codex`

Codex (OpenAI) usage reports and native quota views under a hierarchical
subgroup.
`daily`/`monthly`/`session` are drop-ins for `ccusage codex <cmd>` — paste a
`ccusage codex …` command verbatim and it runs offline. `weekly` is a cctally
extension (upstream has no `ccusage codex weekly`).

> The four accounting leaves share their engine with the matching flat
> `cctally codex-<cmd>` alias — the output (table / `--json` / exit code) is
> identical. The flat `codex-*` forms remain fully supported as back-compat
> aliases (drop-ins for the standalone `ccusage-codex` binary); the subgroup
> form is the canonical, going-forward syntax for the hierarchical
> `ccusage codex …` surface. The provider-aware leaves below pin the matching
> shared command to Codex instead.

## Provider-aware analytics

Alongside the existing Codex compatibility reports, the `codex` subgroup pins
the five shared analytics leaves to the Codex provider:

```text
cctally codex project
cctally codex diff
cctally codex range-cost
cctally codex cache-report
cctally codex report
```

These are fixed-source aliases for the corresponding flat commands with
`--source codex`; they intentionally do not accept a contradictory `--source`
flag. The parallel `cctally claude {project,diff,range-cost,cache-report,report}`
forms pin Claude. There is no `cctally all` subgroup: use the flat command with
`--source all`, which always renders Claude then Codex instead of blending
provider-native rows.

Use `cctally codex cache-report` for Codex **token reuse** (inclusive input,
cached input, non-cached input, reasoning-inclusive output, and native cost),
not a Claude cache-hit rate. Use `cctally codex report` for independent native
quota-window/logical-limit series; it never turns them into a single Codex or
cross-provider weekly percentage.

The older `codex-daily`, `codex-monthly`, `codex-weekly`, and `codex-session`
commands remain their existing accounting-report family. Their ordinary
terminal/JSON/empty output is unchanged; they now also accept read-only
`--config PATH` and the share output flags documented in [share.md](share.md).

## Synopsis

```
cctally codex <command> [flags]

<command> ∈ { daily, monthly, session, weekly, project, diff, range-cost, cache-report, report, percent-breakdown, quota }
```

## Subcommands

| Subcommand | Same engine as | Drop-in for | Page |
| --- | --- | --- | --- |
| `cctally codex daily` | `cctally codex-daily` | `ccusage codex daily` | [codex-daily.md](codex-daily.md) |
| `cctally codex monthly` | `cctally codex-monthly` | `ccusage codex monthly` | [codex-monthly.md](codex-monthly.md) |
| `cctally codex session` | `cctally codex-session` | `ccusage codex session` | [codex-session.md](codex-session.md) |
| `cctally codex weekly` | `cctally codex-weekly` | _cctally extension_ | [codex-weekly.md](codex-weekly.md) |
| `cctally codex project` | `cctally project --source codex` | _cctally extension_ | [project.md](project.md) |
| `cctally codex diff` | `cctally diff --source codex` | _cctally extension_ | [diff.md](diff.md) |
| `cctally codex range-cost` | `cctally range-cost --source codex` | _cctally extension_ | [range-cost.md](range-cost.md) |
| `cctally codex cache-report` | `cctally cache-report --source codex` | _cctally extension_ | [cache-report.md](cache-report.md) |
| `cctally codex report` | `cctally report --source codex` | _cctally extension_ | [report.md](report.md) |
| `cctally codex percent-breakdown` | native seven-day quota milestones | _cctally extension_ | [codex-percent-breakdown.md](codex-percent-breakdown.md) |
| `cctally codex quota …` | native nested surface | _cctally extension_ | [codex-quota.md](codex-quota.md) |

For the four accounting leaves, every flag, exit code, and output shape is
exactly that of the linked flat page — there are no behavior differences. The
provider-aware leaves follow their linked shared-command reference with the
source fixed to Codex. The native quota surface has its own reference below.

## Examples

```bash
cctally codex daily --since 2026-05-01
cctally codex monthly --breakdown
cctally codex session --json
cctally codex weekly
cctally codex percent-breakdown
cctally codex quota statusline
cctally codex quota forecast --json
```

## Notes

- Naming convention: the flat hyphenated `codex-*` forms are drop-ins for the
  standalone `ccusage-codex` binary; the hierarchical `codex <cmd>` subgroup
  is the drop-in for upstream's `ccusage codex <cmd>` subgroup. cctally mirrors
  upstream's own dual surface one-to-one.
- `cctally codex weekly` (and the flat `cctally codex-weekly`) have no upstream
  counterpart — week-start day is read from `config.json`.
- `cctally codex quota` is a native, nested-only cctally extension. It has no
  flat `codex-quota` alias and is not a `ccusage-codex` drop-in. Its five
  leaves keep every source root and logical quota limit independent; see
  [codex-quota.md](codex-quota.md) for selectors, local-rollout freshness, and
  the JSON contracts.
- `--speed {auto,standard,fast}` is accepted by the four accounting leaves
  (`daily`, `monthly`, `session`, and `weekly`), the five fixed-source
  provider-aware analytics leaves (`project`, `diff`, `range-cost`,
  `cache-report`, and `report`), `cctally codex percent-breakdown`, and
  `cctally codex quota breakdown`. For the
  fixed-source analytics leaves it selects the Codex query-time pricing tier
  just as `--source codex` does on their flat counterparts; it does not change
  their fixed Codex source. On quota it is accepted only by `breakdown`, where
  it selects query-time cost correlation; the other quota leaves do not accept
  `--speed`. On the accounting subgroup forms this is faithful to `ccusage
  codex <cmd> --speed`; on the flat `codex-*` aliases it is a cctally extension
  (the standalone `ccusage-codex` binary has no `--speed`). `auto` (the
  default) reads `service_tier` from `~/.codex/config.toml`. See the relevant
  leaf page's "Pricing tier (`--speed`)" section for details.
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
- [`codex-quota`](codex-quota.md) — native quota history, status, forecast,
  reset blocks, and percent-crossing breakdown
- [`codex-percent-breakdown`](codex-percent-breakdown.md) — the canonical
  seven-day per-percent cost table
