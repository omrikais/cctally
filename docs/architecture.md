# Architecture

Single-file Python CLI (~4350 lines, stdlib only) at
`bin/cctally`. Two bash wrappers in the same directory.
This page maps out the major data flows so you can find the right
subcommand for the right question.

## Data sources

| Source | Used by |
| --- | --- |
| Claude Code status-line JSON (`rate_limits`) | `record-usage` |
| Claude Code session JSONLs (`~/.claude/projects/**/*.jsonl`) | every Claude usage/cost command via `cache.db` |
| Codex CLI session JSONLs (`~/.codex/sessions/**/*.jsonl`) | every `codex-*` command via `cache.db` |

## Storage layers

```
status line ──► record-usage ──► stats.db (weekly_usage_snapshots, percent_milestones)
                                       ▲
                                       │ joined per WeekRef
                                       ▼
session JSONLs ──► sync_cache() ──► cache.db (session_entries) ──► sync-week ──► weekly_cost_snapshots
                            ▲                                                          │
                            │                                                          ▼
                            └────── daily/monthly/weekly/blocks/range-cost/cache-report/session
                                                                                       │
report ◄─── joins weekly_usage_snapshots × weekly_cost_snapshots ◄─────────────────────┘
```

See [runtime-data.md](runtime-data.md) for the full schema of both DBs.

## The session-entry cache (`cache.db`)

Every JSONL-reading command goes through a delta cache:

1. On invocation, `sync_cache()` walks `~/.claude/projects/` (or
   `~/.codex/sessions/` for Codex commands).
2. For each file, it reads `(size_bytes, mtime_ns)` and compares to
   `session_files.last_byte_offset`.
3. New bytes are tail-ingested into `session_entries`. Old bytes are not
   re-read.
4. Queries (`iter_entries()`) then run against `session_entries` instead of
   re-parsing JSONL.

**Concurrency:** `fcntl.flock` on `cache.db.lock` (Claude) and
`cache.db.codex.lock` (Codex) serializes writers. Losers read the existing
cache without blocking.

**Pricing freshness:** cost is **not** stored in the cache. It's computed
at query time from `CLAUDE_MODEL_PRICING` / `CODEX_MODEL_PRICING`. Update
the dict, and the next read sees the new prices — no invalidation.

**Resilience:** the cache is fully re-derivable. `rm cache.db` or
`cache-sync --rebuild` is always safe. If `cache.db` can't be opened (e.g.
read-only fs), `get_entries()` falls back to direct JSONL parse.

## Week boundaries

The hardest part of the codebase. Subscription weeks are anchored to the
`--resets-at` epoch reported by the Claude Code status line — but Anthropic
jitters that timestamp, so the code normalizes to the nearest hour
boundary.

Resolution order (for commands that need a week start):

1. The most recent `weekly_usage_snapshots.week_start_at` (hour-accurate)
2. Explicit `--week-start-name` CLI flag
3. `config.json → collector.week_start`
4. Hard default `monday`

For the `weekly` command, weeks where snapshots exist use the snapshot
boundaries; weeks before the earliest snapshot extrapolate by 7-day
multiples from the earliest known anchor.

## Week matching (`WeekRef`)

`report` joins `weekly_usage_snapshots` × `weekly_cost_snapshots` per
week. The join key prefers exact `week_start_at` (ISO timestamp) and falls
back to `week_start_date` (date-only) for backward compatibility with older
rows that predate the hour-accurate column.

## Pricing

`CLAUDE_MODEL_PRICING` and `CODEX_MODEL_PRICING` are **hardcoded** dicts
near the top of the script. When Anthropic / OpenAI ship a new model:

- Add an entry to the appropriate dict.
- Sessions using unrecognized Claude models log a warning and contribute
  zero cost.
- Sessions using unrecognized Codex models fall back to
  `CODEX_LEGACY_FALLBACK_MODEL = "gpt-5"` pricing with `isFallback: true`
  in JSON output (mirrors upstream
  [`ccusage-codex`](../README.md#acknowledgments) behavior). One stderr
  warning per unknown name per process.

## Codex token semantics

Codex `last_token_usage` follows the LiteLLM convention:

- `input_tokens` includes `cached_input_tokens`
- `output_tokens` includes `reasoning_output_tokens`

Cost formula:

```
(input - cached) * input_rate
+ cached * cache_read_rate
+ output * output_rate
```

Reasoning is **not** added separately.

## Intentional divergence from upstream `ccusage-codex`

Older Codex rollouts re-emit `event_msg.token_count` with the same
`last_token_usage` after UI/turn_context updates. Upstream
`ccusage-codex` sums every emission (~2× overcount on affected sessions).
This codebase dedups by tracking `info.total_token_usage.total_tokens` and
only yielding when the cumulative strictly advances. See
`_iter_codex_jsonl_entries_with_offsets`.

Result: ~50% lower numbers than upstream on historical data, but matches
the Codex CLI's own authoritative cumulative counter. Fresh Codex sessions
don't re-emit, so new data matches upstream byte-exactly. **Do not "fix"
this back to upstream parity.**

## Schema migrations

No migration framework. New columns are added inline in `open_db()` via:

```python
cols = {r["name"] for r in conn.execute("PRAGMA table_info(<table>)").fetchall()}
if "<new_col>" not in cols:
    conn.execute("ALTER TABLE <table> ADD COLUMN <new_col> <type>")
```

Follow this pattern when extending the schema.

## Diagnostics

`cctally doctor` is a pure-function kernel (`bin/_lib_doctor.py`) wrapping read-only inspections of every diagnostic source — install symlinks, hook activity, OAuth state, migration markers, snapshot freshness, dashboard bind safety, update-state files. The kernel takes a `DoctorState` dataclass assembled by `doctor_gather_state` in `bin/cctally` (the I/O layer reusing existing helpers like `_db_status_for`, `_setup_count_hook_entries`, `_load_update_state`). The same kernel powers the CLI report (text + JSON), the dashboard SSE envelope's aggregate `doctor` block, and the `GET /api/doctor` full-payload endpoint.

## Where to read next

- [runtime-data.md](runtime-data.md) — exact schema of `stats.db` and `cache.db`
- [`commands/cache-report.md`](commands/cache-report.md) — most complex command, exercises most of the architecture
- [`commands/weekly.md`](commands/weekly.md) — anchor extrapolation and the `WeekRef` story end-to-end
