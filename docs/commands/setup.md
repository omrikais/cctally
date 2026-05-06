# `cctally setup`

Install cctally into Claude Code. Symlinks user-facing binaries into
`~/.local/bin/` and adds hook entries to `~/.claude/settings.json` (additive).

## Modes

| Mode | What it does |
|---|---|
| `cctally setup` | Install (default) |
| `cctally setup --dry-run` | Show planned changes; modify nothing |
| `cctally setup --status` | Report current install state |
| `cctally setup --uninstall` | Remove hooks + symlinks; keep history |
| `cctally setup --uninstall --purge` | Also wipe `~/.local/share/cctally/` |

## Common flags

- `--yes` / `-y` — skip confirmations
- `--json` — emit machine-readable output

## Hook events installed

`PostToolBatch`, `Stop`, `SubagentStop`. Together they cover every
assistant-message boundary at least once. Each entry's `command` is the
absolute path to `cctally` followed by `hook-tick` (bare, no trailing `&`),
quoted via `shlex.quote` so paths with spaces survive `/bin/sh -c`.
`cctally hook-tick` reads its stdin payload synchronously, then forks
itself so CC's hook returns immediately while sync_cache + OAuth refresh
run in the background child.

## Identification of our entries

`setup` recognizes its own entries by the last two shell tokens of the
`command` field: a path whose basename is `cctally` followed by `hook-tick`.
Both bare and absolute paths match; quoted paths (with spaces) match too.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Hard prerequisite failure |
| 2 | Partial: symlinks created but settings.json write failed |
| 3 | User declined a confirmation under non-`--yes` mode |

## See also

- [`hook-tick`](hook-tick.md) — internal per-fire runtime invoked by hooks
- [`refresh-usage`](refresh-usage.md) — manual OAuth fetch (mostly for debugging)
- [`record-usage`](record-usage.md) — opt-in status-line integration (alternative to hooks)
