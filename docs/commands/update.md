# `cctally update`

Self-update cctally for npm and Homebrew installs. Detects the install
method by `realpath(sys.argv[0])`, fetches the latest version from the
matching upstream (npm registry for npm; the Homebrew formula raw blob
for brew), and runs the right install command — `brew update --quiet &&
brew upgrade cctally` or `npm install -g cctally@latest` — with live
output streamed to the terminal (CLI) or modal (dashboard). Source / dev
installs are detected as `unknown` and fall through to a manual recipe.

## Synopsis

```
cctally update [--check | --skip [VERSION] | --remind-later [DAYS]]
               [--version X.Y.Z]
               [--dry-run]
               [--force]
               [--json]
```

The three mode flags (`--check`, `--skip`, `--remind-later`) are mutually
exclusive. With none set, the default mode is **install**.

## Description

`cctally update` is a thin wrapper around the install method's own
upgrade command. It does not bundle its own download or PGP-verify step
— `brew` and `npm` already enforce their package integrity. What it
adds:

- **Install-method detection** from `realpath(sys.argv[0])`. Brew
  matches on `/Cellar/cctally/` (Apple Silicon, Intel, and Linuxbrew all
  funnel through that path); npm matches via `npm prefix -g` plus a
  `<prefix>/lib/node_modules/cctally/` prefix check. Anything else is
  `unknown`. Detection is path-only, no subprocess probes — adds zero
  latency.
- **Auto-suggest banner.** When a newer version is cached in
  `update-state.json`, a one-line banner prints to stderr after the main
  command's output: `↑ cctally 1.7.2 available (you're on 1.5.0). Run
  \`cctally update\`. Skip: cctally update --skip 1.7.2`. Suppressed in
  the same machine-readable contexts as the migration banner (`--json`,
  `--status-line`, `--format`, non-tty stderr, and a fixed list of hot
  hot-path commands like `record-usage`, `hook-tick`, `dashboard`).
- **Dashboard integration.** The dashboard renders an amber `Update
  available` badge in the header when a new version is cached, with a
  modal that streams live subprocess output during install via SSE. The
  modal survives the `os.execvp` that restarts the dashboard server
  post-install — the React SSE consumer auto-reconnects to the new
  server's `/api/events` and `/api/update/stream/<run_id>` channels.
- **Dismissal semantics.** `--skip [VERSION]` appends to a per-version
  ignore list (default = the latest cached version); `--remind-later
  [DAYS]` defers the banner for N days (default 7, range `[1, 365]`).
  Both write to `update-suppress.json`. A newer version landing
  overrides any active remind-later window.
- **Source-install fallback.** When detection returns `unknown` (`git
  clone` + `bin/symlink` install, dev worktree, etc.), `--check` and
  install both print the manual recipe `cd <repo> && git pull &&
  bin/symlink` and exit cleanly (`--check` exit 0; install exit 1).

The check pipeline is detached and lazy: every interactive command
post-runs a TTL gate (default 24h, `update.check.ttl_hours`); on miss,
spawns the hidden `_update-check` subcommand in the background to
refresh state.

## Options

| Flag | Default | Description |
|---|---|---|
| `--check` | off | Read state and print availability info; with `--force`, refresh first. No install, no mutation outside `update-state.json`. |
| `--skip [VERSION]` | latest cached | Append `VERSION` to `skipped_versions`; you won't be reminded again about that exact version. Idempotent. |
| `--remind-later [DAYS]` | 7 | Defer the banner for N days (range `[1, 365]`); `0` or `>365` exits 2. A newer version arriving overrides the deferral. |
| `--version X.Y.Z` | (latest) | Install a pinned version. **npm only** — brew exits 2 with a manual `brew uninstall && brew install <tarball>` recipe, since Homebrew has no versioned formulae. Validation delegates to the existing `_SEMVER_RE`, so prerelease forms (`1.7.0-rc.3`) are accepted. |
| `--dry-run` | off | Print the steps that would run; touch nothing (state file is not written, even by detection's tier-C `npm prefix -g` probe). |
| `--force` | off | Bypass the TTL gate on `--check` (force a fresh remote fetch). Ignored on install (install always ignores TTL). |
| `--json` | off | Machine-readable output. Most useful with `--check`; install mode emits a single result envelope on completion. Suppresses the auto-suggest banner. |

## Examples

```bash
# What's the current state? (Reads cache; refreshes if TTL elapsed.)
cctally update --check

# What's the latest right now, ignoring TTL?
cctally update --check --force

# JSON for scripting / dashboard.
cctally update --check --json

# Install the latest (npm or brew, auto-detected).
cctally update

# Pin to a specific version (npm only).
cctally update --version 1.6.4

# Stop reminding me about 1.7.2 specifically.
cctally update --skip 1.7.2

# Defer the banner for 14 days (or until a newer version drops).
cctally update --remind-later 14

# Preview without installing.
cctally update --dry-run
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success — install completed, `--check` ran (any network outcome including rate-limited / fetch-failed), `--skip` / `--remind-later` recorded. No separate code for "already up to date" or "transient network error during --check" — both exit 0 (matches `refresh-usage` precedent). |
| 1 | Install failure — subprocess returned non-zero, write-permission preflight failed (npm), `method=unknown` on install, lock acquisition failed because another `cctally update` is in progress. |
| 2 | Usage error — invalid flag combination (mode flags + `--version`), invalid SemVer, `--remind-later` days out of range, `--version` on a brew install. |

## Files

All under `~/.local/share/cctally/`:

- `update-state.json` — version-check cache (current/latest version,
  install method, last-known-good values across rate-limits).
  Schema-versioned (`_schema: 1`).
- `update-suppress.json` — user dismissals (`skipped_versions[]`,
  `remind_after`). Schema-versioned (`_schema: 1`).
- `update.log` — append-only run log (one line per event, ISO-8601 UTC
  prefix). Caps at 1 MB and rotates to `update.log.1` (single-slot;
  second rotation overwrites). Failed-install logs are preserved for
  diagnostics.
- `update.lock` — concurrency lock during install. PID + start-time +
  command. Stale-lock recovery via `kill(pid, 0)` + `ProcessLookupError`.
- `update-check.last-fetch` — zero-byte mtime sentinel for the TTL gate.
  Touched **before** the network fetch so a crash mid-check doesn't
  trigger immediate retry storms.

## Configuration

```json
{
  "update": {
    "check": {
      "enabled": true,
      "ttl_hours": 24
    }
  }
}
```

| Key | Default | Range | Description |
|---|---|---|---|
| `update.check.enabled` | `true` | bool | Set to `false` to disable both the background version check and the auto-suggest banner. The `cctally update` subcommand itself still works (you can run it manually). |
| `update.check.ttl_hours` | `24` | `[1, 720]` | How often to refresh the cached version. `1` is hourly; `720` is monthly. Outside the range exits 2 (cross-field validation, mirrors the `oauth_usage` validation pattern). |

Set via `cctally config set update.check.enabled false` or
`cctally config set update.check.ttl_hours 168`. The dashboard mirrors
these via `POST /api/settings`.

## Dashboard integration

When a newer version is cached in `update-state.json`, the dashboard
header renders an amber `Update available` badge. Clicking it opens an
**Update modal** that:

- Shows current vs. latest, the install method, and the install command
  that will run.
- Streams stdout / stderr live from the install subprocess via SSE
  (`/api/update/stream/<run_id>`), the same way the install logs to
  `update.log`.
- Surfaces the `cctally update --version <X.Y.Z>` flag through a
  text input for npm installs only; brew shows the manual `brew
  uninstall && brew install <tarball>` recipe instead.
- Has **no abort UI** — closing the modal mid-install does NOT kill the
  subprocess. Aborting `npm install -g` / `brew upgrade` mid-run can
  leave the install half-applied; we'd rather let it complete and surface
  the result on the next page load.
- After a successful install, the server `os.execvp`'s itself to pick
  up the new binary. The React SSE consumer detects the disconnect,
  retries with exponential backoff, and reconnects to the new server —
  the modal stays open and renders the post-restart "Updated to X.Y.Z"
  toast.

CSRF: `POST /api/update` and `POST /api/update/dismiss` join the
existing `_check_origin_csrf` gated set (Origin host:port vs. Host
header). Read endpoints (`GET /api/update/status`, `GET
/api/update/stream/<run_id>`) are unguarded — they only return data
already on disk.

## Source-install fallback

When `realpath(sys.argv[0])` doesn't match a `/Cellar/cctally/` segment
or an `npm prefix -g`-rooted `lib/node_modules/cctally/`, the install
method is `unknown`. This is the expected case for source / dev
installs (`git clone https://github.com/omrikais/cctally && bin/symlink`,
or running `bin/cctally` directly from a checkout).

`cctally update --check` prints:

```
Current   1.5.0
Latest    1.7.2
Method    unknown  (auto-detected)

Install method is 'unknown' — automatic update unavailable.
If you installed from source: cd <repo> && git pull && bin/symlink
```

`cctally update` (install mode) prints the same recipe and exits 1.

## See also

- [`cctally config`](config.md) — read or change `update.check.enabled` /
  `update.check.ttl_hours`.
- [`cctally setup`](setup.md) — installs hooks and `~/.local/bin/`
  symlinks for cctally; also adds `cctally-update` to that symlink list.
- [`cctally release`](release.md) — the publish side. The first version
  that ships `cctally update` reaches users via existing manual update
  workflows; from that version onward, auto-suggest covers future
  updates.
