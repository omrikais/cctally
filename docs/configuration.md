# The `config.json` file: shape, reserved keys, and week-start rules

**Looking for the list of settings you can change?** That is
[`commands/config.md`](commands/config.md), which documents every key
`cctally config set` accepts, its values, its default, and whether the
dashboard can write it. This page covers the file itself: where it lives,
what it looks like on disk, the reserved `collector.*` block, and how the
week-start fallback resolves.

`config.json` lives at `~/.local/share/cctally/config.json` and is
auto-created on first run with a random collector token.

## Shape

```json
{
  "collector": {
    "host": "127.0.0.1",
    "port": 17321,
    "token": "<32-hex-chars, generated on first run>",
    "week_start": "monday"
  }
}
```

| Key | Type | Default | Used by |
| --- | --- | --- | --- |
| `collector.host` | string | `127.0.0.1` | reserved for an HTTP collector mode (not currently wired in the CLI surface) |
| `collector.port` | int | `17321` | reserved (see above) |
| `collector.token` | string | random 32 hex chars | reserved (see above) |
| `collector.week_start` | string | `monday` | week-boundary fallback for `sync-week` / `report` / `weekly` / `codex-weekly` when no explicit `--resets-at` or `--week-start-name` override is supplied |

The four `collector.*` keys above are **reserved**: they are read from the
file but are not `cctally config set` keys, so they do not appear in the
[allowed-keys table](commands/config.md#allowed-keys). Edit them in the file.

A real `config.json` holds keys of three kinds, and knowing which kind you
are looking at tells you how to change it.

1. **Settable keys.** Most of what you will see — the `display`, `alerts`,
   `budget`, `dashboard`, `update`, `statusline`, `telemetry`,
   `conversation`, `storage` and `codex` blocks — is written by
   `cctally config set` and is documented in the
   [allowed-keys table](commands/config.md#allowed-keys).
2. **The reserved `collector.*` block** described above.
3. **Keys the CLI reads from the file but does not let you set.** These are
   hand-edited and do not appear in the allowed-keys table. The
   `oauth_usage` block is one, documented in
   [`commands/refresh-usage.md`](commands/refresh-usage.md); the
   `alerts.weekly_thresholds` and `alerts.five_hour_thresholds` lists are
   two more, documented in [`commands/alerts.md`](commands/alerts.md). This
   is not a complete inventory: when a key is absent from the allowed-keys
   table, read the page for the command that consumes it.

Allowed `week_start` values: `monday`, `tuesday`, `wednesday`, `thursday`,
`friday`, `saturday`, `sunday`.

## Week-start resolution order

For commands that bucket by subscription week (`sync-week`, `report`,
`weekly`, `codex-weekly`):

1. `--resets-at` epoch from the most recent usage snapshot (hour-accurate
   anchor; only Claude side, only when `weekly_usage_snapshots` has data)
2. Explicit `--week-start-name` flag (when the command supports it)
3. `collector.week_start` from `config.json`
4. Hard default `monday`

`codex-weekly` skips step 1 (no Codex equivalent of `--resets-at`).

## Precedence vs. embedded defaults

The `collector.*` block only overrides what's listed above; every other
setting is documented in [`commands/config.md`](commands/config.md). Model
pricing (`CLAUDE_MODEL_PRICING`, `CODEX_MODEL_PRICING`) is hardcoded in the
script and not configurable — see
[architecture.md](architecture.md#pricing) for why.

## Editing safely

The file is plain JSON. Edit it however you like; the CLI re-reads it on
every invocation.

If the file is malformed, the loader prints one warning to stderr and falls
back to in-memory defaults for that invocation. **It does not rewrite the
file, so your edits are still there** — fix the JSON and the next run picks
it up. The bad bytes are only replaced when something legitimately saves the
config (a `cctally config set`, or a dashboard settings save), which writes
the whole file atomically under the writer lock.

Prefer `cctally config set` over hand-editing where a key supports it: it
validates the value before persisting, so a written config never fails a
later read.
