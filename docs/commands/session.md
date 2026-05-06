# `session`

Claude usage grouped by `sessionId`. Resumed sessions (same `sessionId`
across multiple JSONL files via `claude --resume`) collapse into a single
row. 11-column layout that parallels [`codex-session`](codex-session.md).

## Synopsis

```
cctally session
    [-s YYYYMMDD] [-u YYYYMMDD]
    [-b] [-o {asc,desc}]
    [--json]
```

## Options

| Flag | Description |
| --- | --- |
| `-s, --since YYYYMMDD` | Filter from date (inclusive). |
| `-u, --until YYYYMMDD` | Filter until date (inclusive). |
| `-b, --breakdown` | Show per-model cost breakdown sub-rows. |
| `-o, --order {asc,desc}` | Sort direction by last activity (default `asc` — earliest first). |
| `--tz TZ` | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |
| `--json` | Output JSON. |

## Examples

```bash
cctally session
cctally session --since 20260401
cctally session --since 20260401 --breakdown
cctally session --json
cctally session --order desc
```

## How resume merging works

Each JSONL file under `~/.claude/projects/` is associated with a
`sessionId` extracted from the first line carrying it. The
`session_files.session_id` column in `cache.db` stores that mapping.
This command groups `session_entries` by `sessionId` (joined on
`source_path`), so all entries from a `--resume`-extended session
collapse into one row.

The `Directory` column shows the most-recent project if the resume
crossed `cwd`s. The JSON output's `sourcePaths` array preserves the
full list of files.

## Gotchas

- **`session_files` is populated lazily.** On the first command run
  after a deploy, some entries may briefly lack `session_id` /
  `project_path`. The aggregator falls back to the filename UUID as
  `sessionId` and emits a one-shot stderr warning:
  `Warning: N entries lacked session_files rows (cache may be catching up).`
  Subsequent runs backfill the metadata via `sync_cache()`.
- Sort defaults to **ascending** (earliest first) to match
  `codex-session`'s "scrollback-friendly" default.

## See also

- [`codex-session`](codex-session.md) — Codex equivalent (same column layout)
- [Architecture · cache.db](../architecture.md#the-session-entry-cache-cachedb)
