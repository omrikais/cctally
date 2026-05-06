# Configuration

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

`config.json` only overrides what's listed above. Model pricing
(`CLAUDE_MODEL_PRICING`, `CODEX_MODEL_PRICING`) is hardcoded in the script
and not configurable — see [architecture.md](architecture.md#pricing) for why.

## Editing safely

The file is plain JSON. Edit it however you like; the CLI re-reads it on
every invocation. If the file is malformed, the loader silently regenerates
defaults (and overwrites your edits) — back up before experimenting.
