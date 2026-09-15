# Architecture

`bin/cctally` is the executable and re-export surface of a **stdlib-only** Python 3 CLI; it eagerly loads sibling command/glue modules (`bin/_cctally_*.py`) and reusable `_lib_*.py` modules from the same directory. Optional dependencies such as `rich` stay command-lazy. Thin bash wrappers exist for selected commands. The `dashboard/web/` Vite/React island is the one build-time surface; its built output is committed to `dashboard/static/` so the runtime stays zero-dep. This page maps out the major data flows so you can find the right subcommand for the right question.

## Data sources

| Source | Used by |
| --- | --- |
| Claude Code status-line JSON (`rate_limits`) | `record-usage` |
| Claude Code session JSONLs (`~/.claude/projects/**/*.jsonl`) | Claude usage/cost commands that read session data via `cache.db` (`report` without `--sync-current` is stats-only) |
| Codex CLI session JSONLs (`~/.codex/sessions/**/*.jsonl`) | every `codex-*` command via `cache.db` |

## Storage layers

```text
Claude status line / OAuth / operator decisions ──► append-only journal
Codex rollout quota observations ─────────────────► append-only journal
                                                        │
                                 single-flight ingest / derivation
                                 journal derived events before commit
                                                        │
                                                        ▼
                                                   stats.db index
                                               (usage, cost snapshots,
                                                milestones, credits)
Provider JSONL ──► accounting cursor ──► cache.db ──► query-time priced reports
             └──► transcript cursor ──► conversations.db ──► transcript readers

report joins usage × cost snapshots in stats.db by the documented WeekRef.
Dashboard resources compose these stores with explicit provenance and scope.
```

The journal preserves observations, operator decisions, derived events and
completed corrections. `stats.db` is its disposable, epoch-versioned index;
losing the journal loses durable history. Provider JSONL remains the source
for reconstructible accounting and transcript records, rather than being
duplicated wholesale into the journal. See [runtime-data.md](runtime-data.md)
for table details and the authority/connection contract below.

## Authority, connections and service boundaries

| State | Authority and owner | Connection and publication rule |
|---|---|---|
| Provider JSONL | Provider writes; discovery identifies root, physical file and incarnation | Accounting and transcript consumers retain independent byte cursors; advancing one never advances the other |
| Observation journal | Append-only observations, operations, committed derived facts and corrections | Append/fsync under the leaf journal lock; pin a high-water prefix before replay |
| `stats.db` | Disposable journal projection; live derivation goes through `run_stats_ingest` | DELETE/FULL policy; journal events precede the transaction commit; rows and applied-prefix pairs commit atomically; alerts dispatch afterward |
| `cache.db` | Reconstructible accounting, quota and identity projections | WAL/NORMAL; global writer flock before Codex provider flock; source/pricing revisions invalidate derived consumers |
| `conversations.db` | Reconstructible normalized messages, rollups, titles, turns and search indexes | WAL/NORMAL; independent provider cursors/locks; readers attach accounting read-only and apply account-local TEMP views |
| Process caches | Disposable accelerators, never durable truth | Generation and all semantic input revisions must agree; cold fallback preserves information |
| Published dashboard | Complete snapshot plus separately owned activity state | `_SnapshotRef` owns activity counters; `SSEHub` coalesces each subscriber to its latest complete delivery |

Product openers are not pure query APIs: `open_db`, `open_cache_db` and
`open_conversations_db` can run policy, schema, recovery or maintenance work.
Calling a handler GET therefore does not establish that its transitive path is
free of persistent writes. Raw read-only inspection uses SQLite `mode=ro`
without those openers. Account TEMP tables are connection-local scope, not
persistent writes, and must remain possible in a future query interface.

The target boundary is an initial **in-process Python query service**, followed
by explicit ingestion, projection, publication and maintenance interfaces.
This is a refactoring contract, not a claim those services already exist:

| Interface | Input and output | Invariant |
|---|---|---|
| Query | Provider/root/account scope, window, bounded page, expected generation → rows, provenance, continuation or typed degradation | No persistent mutation/recovery; preserve TEMP account isolation and complete useful fields |
| Discovery | Root identity and consumer position → scoped change batch and high-water | Separate acknowledgement per accounting/transcript consumer; gap/replacement forces explicit reconciliation |
| Ingestion | Source incarnation, byte range and prior cursor → committed changed identities and next cursor | Cursor advances with its own rows; id-stable updates and truncations are changes |
| Projection | Complete change sequence, pricing/account/window semantics → committed generation | Never mix generations; gaps rebuild from authority; old writer cannot publish over a newer generation |
| Publication | Complete projection generation, source watermarks, time deadline → versioned resource | Fresh publication time is not proof of fresh source data; partial hydration is identified |
| Maintenance | Store identity, policy, generation and bounded work budget → resumable progress/refusal | Respect writer/reader admission and preserve last-complete data; no live-family unlink shortcut |

The first proposed module is `bin/_lib_query_context.py`: bounded query
connection ownership, provider/account scope, generation checks and typed
readiness results. It delegates existing domain kernels and initially serves
one conversation reader. No module is added by this documentation change.
Start with query ownership because its transitive mutation is observable and
its interface can be adopted by one production reader before broad extraction.
Retain current Python/SQLite/React/SSE foundations. A process or native helper
requires measured benefit in CPU, memory, failure isolation and end-to-end
latency, including its own startup, serialization and shutdown costs.

Lock acquisition order is maintenance → journal ingest → global cache writer
→ Codex cache writer → conversation writers (Claude then Codex) → artifact
retention → SQLite transaction → journal append. Never acquire an earlier
lock while holding a later one. The existing rebuild preservation exception
holds artifact-retention **shared** under stats maintenance **exclusive** while
acquiring cache writers; it is a specific documented protocol, not permission
for a new inversion. A SQLite write transaction never spans a flock acquisition.

Durable identity includes provider, root key, physical source/incarnation,
logical session/message identity, account attribution/provenance, canonical
physical quota window, billing-cycle segments, pricing fingerprint and semantic
mutation/render revisions. Paths and directory basenames alone do not identify
accounts or sessions. A same-id finalization must advance the revision even
when row count and maximum id do not move.

Current publication gives atomic replacement within each retained snapshot;
it does not establish a common source frontier across independently ingested
stores. The target requires explicit per-consumer watermarks and a consistent
generation, with typed degradation when coherence cannot be proved. Never
infer that guarantee from `generated_at`, a content hash or SSE connectivity.

## The session-entry cache (`cache.db`)

Every JSONL-reading command goes through a delta cache:

1. On invocation, `sync_cache()` walks `~/.claude/projects/` (Codex commands use the parallel `sync_codex_cache()` over `~/.codex/sessions/`, with its own `codex_session_files`/`codex_session_entries` tables and `cache.db.codex.lock`).
2. For each file, it reads `(size_bytes, mtime_ns)` and compares to the Claude-side `session_files.last_byte_offset`.
3. New bytes are tail-ingested into the Claude-side `session_entries`. Old bytes are not
   re-read.
4. Queries (`iter_entries()`) then run against `session_entries` instead of
   re-parsing JSONL.

**Concurrency:** `cache.db.lock` is the global compact-cache writer/checkpoint
flock. Claude takes it directly; Codex takes it before its provider-specific
`cache.db.codex.lock`. Schema and recovery work first takes the maintenance
flock, preserving the total order
`maintenance → global cache → Codex provider → SQLite transaction`. Transcript
writers use the independent `conversations.db.lock`,
`conversations.db.codex.lock`, and maintenance lock, so a large reingest never
extends the core sync critical section.

**Pricing freshness:** raw tokens remain available for query-time calculation;
some derived costs and rollups are materialized and carry pricing provenance.
Changing an embedded table is not sufficient to certify every retained or
long-running reader. Preserve fingerprint ordering, invalidation and stale-writer
refusal when changing a pricing or query boundary.

**Resilience:** both derived stores are fully re-derivable; use
`cache-sync --rebuild` rather than unlinking a live SQLite family. Classified
cache corruption converges through a single forensics-first, whole-family
quarantine contract at open time or during ingest, then retries once. An
`all`-provider recovery restarts the complete Claude→Codex plan because both
providers share the replaced physical family. A durable pending-quarantine
record makes individual main/WAL/SHM renames resumable and forbids recreation
after a partial move. Repair ownership uses PID plus process-start identity so
a dead owner or reused PID cannot permanently wedge readers. Non-corruption
failures never quarantine. If `cache.db` can't be opened (e.g. read-only fs),
`get_entries()` falls back to direct JSONL parse.

## The transcript/search store (`conversations.db`)

`sync_claude_conversations()` and `sync_codex_conversations()` maintain their
own source cursors, normalized rows, browse rollups, and FTS indexes. Conversation
readers open this file as `main` and attach `cache.db` read-only for cost/token
and compact Codex-thread metadata. Core accounting connections never attach the
transcript store, so it can be missing, locked, or rebuilding without blanking
the dashboard's accounting/quota panels.

**JSONL dedup tiebreaker (v1.12.0+).** Two `type:assistant` rows in a
single `~/.claude/projects/**/*.jsonl` can share the same
`(message.id, requestId)` pair when Claude Code emits a streaming
intermediate (`output_tokens=1`, no `speed` field) followed by a
post-stream finalization (`output_tokens=N`, `speed="standard"`). Cache
ingest and direct-JSONL parse both pick the higher-token row, breaking
ties on `speed`-presence. This matches ccusage's
`should_replace_deduped_entry` (`claude_loader.rs:531`). The cache's
`session_entries` UNIQUE index on `(msg_id, req_id)` is partial
(`WHERE msg_id IS NOT NULL AND req_id IS NOT NULL`); the ingest INSERT
uses `ON CONFLICT(msg_id, req_id) WHERE msg_id IS NOT NULL AND req_id
IS NOT NULL DO UPDATE … WHERE …` to match it.

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
in `bin/_lib_pricing.py`. When Anthropic / OpenAI ship a new model:

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

When `--speed fast` is in effect (or `--speed auto` resolves to fast from
`~/.codex/config.toml`'s `service_tier`), the whole per-entry cost is multiplied
by a per-model fast-tier factor (`gpt-5.5` ×2.5, otherwise ×2.0).

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

Two patterns, one rule each:

- **Column additions** use `add_column_if_missing(...)` — an idempotent guard that adds the column when absent. No marker row, no version bump.
- **Derived cache/conversation data-shape changes** (backfills, dedups, renames,
  table rewrites) use their migration registry and schema version. Current
  stats schema changes bump `STATS_INDEX_EPOCH` and rebuild from the journal;
  the stats migration registry describes the legacy, pre-cutover path.

Do **not** write inline `if "<col>" not in cols: ALTER TABLE …` blocks in `open_db()` — that is the anti-pattern the framework replaces.

The operator surface is `cctally db status`, `db skip` / `db unskip` for
registry migrations, `db recover --db cache` for a version-ahead cache, and
`db rebuild --db stats` for the journal index. `db recover --db stats` is
retired. A dev-checkout binary refuses production forward migrations by
default. A supported upgrade needs an explicit prior-package/schema fixture,
recovery evidence and an installed-path check; an old binary is not
automatically a safe rollback. Internal module or derived-schema changes must
preserve the public CLI JSON/exit contracts through adapters and migrations.

## Diagnostics

`cctally doctor` is a pure-function kernel (`bin/_lib_doctor.py`) wrapping read-only inspections of every diagnostic source — install symlinks, hook activity, OAuth state, migration markers, snapshot freshness, dashboard bind safety, update-state files. The kernel takes a `DoctorState` dataclass assembled by `doctor_gather_state` in `bin/_cctally_doctor.py` (the I/O layer reusing existing helpers like `_db_status_for`, `_setup_count_hook_entries`, `_load_update_state`). The same kernel powers the CLI report (text + JSON), the dashboard SSE envelope's aggregate `doctor` block, and the `GET /api/doctor` full-payload endpoint.

## Where to read next

- [runtime-data.md](runtime-data.md) — exact schema of `stats.db` and `cache.db`
- [`commands/cache-report.md`](commands/cache-report.md) — most complex command, exercises most of the architecture
- [`commands/weekly.md`](commands/weekly.md) — anchor extrapolation and the `WeekRef` story end-to-end
