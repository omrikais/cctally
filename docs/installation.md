# Installation

## Distribution channels

cctally ships through three channels. They land identical functionality; pick the one that matches your environment.

### Homebrew tap

```bash
brew install omrikais/cctally/cctally
```

The formula installs `python@3.13` if it's not already present and pins cctally's shebang to that keg, so the tool keeps working even if your system Python changes. Symlinks `cctally` and the user-facing wrappers (`cctally-tui`, `cctally-dashboard`, `cctally-forecast`, …) into `/opt/homebrew/bin/` (Apple Silicon) or `/usr/local/bin/` (Intel/Linuxbrew).

After `brew install`, run `cctally setup` once to register Claude Code hooks and bootstrap the local SQLite cache.

### npm

```bash
npm install -g cctally
```

The package bundles `bin/cctally` (the Python script) and the dashboard static assets. The `cctally` command on PATH is a ~30-line Node shim that resolves `python3` and `exec`s the bundled script. **Windows is not supported** — use WSL or a native Linux/macOS environment.

If you have a custom Python install, set `CCTALLY_PYTHON`:

```bash
export CCTALLY_PYTHON=/opt/homebrew/bin/python3.13
```

### From source

```bash
git clone https://github.com/omrikais/cctally
cd cctally
./bin/cctally setup
```

Useful when you want to run unreleased commits or iterate on contributions. `setup` symlinks `bin/cctally*` into `~/.local/bin/`.

## Migrating from the status-line snippet

If you already wired `cctally record-usage` into `~/.claude/statusline-command.sh`,
you don't have to do anything; it keeps working. To switch to the new
hook-based path, run `cctally setup`. The legacy snippet will be detected
and you'll be told it's safe to remove (we don't touch your file).

## Migrating from an earlier install pattern

If you previously wired cctally into Claude Code via hand-installed scripts
under `~/.claude/hooks/` (e.g. `record-usage-stop.py`,
`usage-poller-{start,stop}.py`, `usage-poller.py`), `cctally setup` will
detect them and offer to migrate: unwire the matching settings.json
entries, move the files to a timestamped backup directory under
`~/.claude/`, and best-effort stop any active background daemon. See
[Migrating from a prior install pattern](commands/setup.md#migrating-from-a-prior-install-pattern)
for the details and the new `--migrate-legacy-hooks` /
`--no-migrate-legacy-hooks` flags.

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
