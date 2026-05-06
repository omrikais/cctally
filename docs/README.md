# Documentation

Reference docs for `cctally`.

## Start here

- [Installation](installation.md) — symlinks, status-line wiring, Python version
- [Configuration](configuration.md) — `config.json` shape and week-start rules
- [Runtime data](runtime-data.md) — what lives in `~/.local/share/cctally/`
- [Architecture](architecture.md) — data flow, caches, week boundaries

## Command reference

See [`commands/`](commands/README.md) for one page per subcommand.

## Project layout

- [`../bin/`](../bin) — the three executables
- [`../CLAUDE.md`](../CLAUDE.md) — instructions for Claude Code agents working in this repo
- [`../agents.md`](../agents.md) — older agent context (overlaps with CLAUDE.md)

## Conventions used in these docs

- Code blocks show the **canonical invocation** (no shell wrappers) unless the
  wrapper adds value. `cctally-dollar-per-percent` ≡ `cctally report --sync-current`.
- Date arguments accept either `YYYYMMDD` or `YYYY-MM-DD` everywhere except
  `record-usage --resets-at` (Unix epoch seconds) and `range-cost -s/-e` (ISO 8601 with offset).
- All "Examples" in command pages are copy-pasteable and use real-shaped data.
