# Commands

One page per subcommand. Pages follow a consistent shape:

> Synopsis · Purpose · When to use · Options · Examples · Notes · See also

## Setup, recording, and reporting

| Command | Page |
| --- | --- |
| `setup` | [setup.md](setup.md) — install cctally into Claude Code (hooks + symlinks) |
| `hook-tick` | [hook-tick.md](hook-tick.md) — internal per-fire runtime (invoked by hooks) |
| `record-usage` | [record-usage.md](record-usage.md) |
| `refresh-usage` | [refresh-usage.md](refresh-usage.md) — manual OAuth usage-API refresh |
| `sync-week` | [sync-week.md](sync-week.md) |
| `report` | [report.md](report.md) |
| `percent-breakdown` | [percent-breakdown.md](percent-breakdown.md) |
| `forecast` | [forecast.md](forecast.md) |
| `project` | [project.md](project.md) |

## Diagnostics

| Command | Page |
| --- | --- |
| `cache-report` | [cache-report.md](cache-report.md) |
| `cache-sync` | [cache-sync.md](cache-sync.md) |
| `range-cost` | [range-cost.md](range-cost.md) |

## Claude usage rollups

| Command | Page |
| --- | --- |
| `daily` | [daily.md](daily.md) |
| `monthly` | [monthly.md](monthly.md) |
| `weekly` | [weekly.md](weekly.md) |
| `blocks` | [blocks.md](blocks.md) |
| `session` | [session.md](session.md) |

## Codex usage rollups

| Command | Page |
| --- | --- |
| `codex-daily` | [codex-daily.md](codex-daily.md) |
| `codex-monthly` | [codex-monthly.md](codex-monthly.md) |
| `codex-weekly` | [codex-weekly.md](codex-weekly.md) |
| `codex-session` | [codex-session.md](codex-session.md) |

## Releases

| Command | Page |
| --- | --- |
| `release` | [release.md](release.md) — stamp CHANGELOG, cut SemVer tag, propagate to public mirror, create GitHub Release |

## Wrappers

The bash wrappers in `bin/` exist for muscle-memory shortcuts:

- `cctally-dollar-per-percent` ≡ `cctally report --sync-current`
- `cctally-sync-week` ≡ `cctally sync-week`
- `cctally-forecast` ≡ `cctally forecast`
- `cctally-project` ≡ `cctally project`

Each forwards `"$@"`, so all flags from the underlying command work.
