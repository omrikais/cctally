# Changelog

All notable changes to this project are documented in this file. Format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `cctally setup` — one-command install: symlinks user-facing binaries into
  `~/.local/bin/` and adds additive hook entries (`PostToolBatch`, `Stop`,
  `SubagentStop`) to `~/.claude/settings.json`. Includes `--dry-run`,
  `--status`, `--uninstall`, `--uninstall --purge` modes.
- `cctally hook-tick` — internal per-fire runtime invoked by Claude Code
  hooks. Reads CC hook payload from stdin, runs `sync_cache`, conditionally
  refreshes OAuth usage (default 30s throttle).
- `~/.local/share/cctally/logs/hook-tick.log` — rotating per-fire log
  (1 MB cap, single-generation rotation).
- `~/.local/share/cctally/hook-tick.last-fetch` — OAuth throttle marker
  (sentinel file owned by hook-tick).
- Fixture harnesses: `bin/cctally-setup-test` (13 scenarios) and
  `bin/cctally-hook-tick-test` (7 scenarios), both wired into
  `bin/cctally-test-all`.

### Changed
- Default integration is now hook-based. The legacy status-line snippet
  (`cctally record-usage` from `~/.claude/statusline-command.sh`) is no
  longer the recommended path but **remains fully supported** as an opt-in
  alternative documented in `docs/commands/record-usage.md`.
- `docs/installation.md` rewritten around `cctally setup`.
