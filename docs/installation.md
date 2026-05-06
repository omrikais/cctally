# Installation

## Migrating from the status-line snippet

If you already wired `cctally record-usage` into `~/.claude/statusline-command.sh`,
you don't have to do anything; it keeps working. To switch to the new
hook-based path, run `cctally setup`. The legacy snippet will be detected
and you'll be told it's safe to remove (we don't touch your file).

## Requirements

- Python 3.13+ (stdlib only, no `pip install` needed).
- macOS or Linux. Tested primarily on macOS (zsh).
- Claude Code installed and run at least once (`~/.claude/` must exist).

## Install

```bash
git clone https://github.com/omrikais/cctally
cd cctally
./bin/cctally setup
```

`cctally setup` will:
- Symlink the user-facing binaries into `~/.local/bin/`
- Add three hook entries (`PostToolBatch`, `Stop`, `SubagentStop`) to
  `~/.claude/settings.json` (additive: it never modifies your existing entries)
- Bootstrap the local SQLite cache and (if you've authenticated with Claude)
  fetch the first usage snapshot

If `~/.local/bin` isn't on your PATH yet, `setup` will print the line to add to
your shell rc.

## Verify

```bash
cctally setup --status
cctally daily
cctally dashboard
```

## Uninstall

```bash
cctally setup --uninstall            # remove hooks + symlinks; keep history
cctally setup --uninstall --purge    # also wipe ~/.local/share/cctally/
```

## Optional: opt-in status-line integration (no OAuth API calls)

If you'd rather have your existing status line feed cctally directly (and avoid
the OAuth API roundtrips that the hook path makes once per ~30s), see
[`docs/commands/record-usage.md`](commands/record-usage.md). The two paths are
not mutually exclusive; both go through the same `record-usage` funnel and
dedupe correctly.
