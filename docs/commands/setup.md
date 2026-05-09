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
- `--migrate-legacy-hooks` — auto-accept the legacy-hook migration prompt (install-mode only)
- `--no-migrate-legacy-hooks` — auto-decline the legacy-hook migration prompt (install-mode only)

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

## Migrating from a prior install pattern

If your machine has hooks from an earlier install pattern — hand-installed
Python scripts under `~/.claude/hooks/` (`record-usage-stop.py`,
`usage-poller-start.py`, `usage-poller-stop.py`, `usage-poller.py`) plus
their `Stop` / `SubagentStart` / `SubagentStop` entries in
`~/.claude/settings.json` — `cctally setup` will detect them and offer to
migrate. The migration:

- Unwires the matching entries from `~/.claude/settings.json`.
- Moves the `.py` files to `~/.claude/cctally-legacy-hook-backup-<UTC ts>/`
  (reversible — files are moved, not deleted).
- Best-effort stops any currently-active background daemon spawned by
  those hooks (so you don't have to wait out its multi-hour timer or
  reboot for the new wiring to fully take effect).

By default `cctally setup` prompts on a TTY. Pass `--migrate-legacy-hooks`
to auto-accept (useful for non-interactive setups; also implied by
`--yes`), or `--no-migrate-legacy-hooks` to skip without prompting. Both
flags are install-mode only — they're rejected with exit code 2 if
combined with `--status` or `--uninstall`. Under `--json` or a
non-interactive stdin, the prompt is skipped silently and the migration
runs only when one of the two flags is set explicitly.

`cctally setup --status` reports the current legacy-hook state in both
text (under "Legacy bespoke hooks") and `--json` (under
`legacy.bespoke_hooks`). `cctally setup --dry-run --migrate-legacy-hooks`
previews the migration without touching disk.

## See also

- [`hook-tick`](hook-tick.md) — internal per-fire runtime invoked by hooks
- [`refresh-usage`](refresh-usage.md) — manual OAuth fetch (mostly for debugging)
- [`record-usage`](record-usage.md) — opt-in status-line integration (alternative to hooks)
