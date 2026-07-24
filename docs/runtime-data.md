# Runtime data

All persistent state lives under `~/.local/share/cctally/` (a dev checkout uses `~/.local/share/cctally-dev/` instead). Nothing here is committed to the repo. Most of it re-derives on demand, but two things do not: `stats.db` holds history that cannot be rebuilt, and a few files (`config.json`, `install_id`) "regenerate" only to fresh defaults — the prior value is gone. The file map below is explicit about which is which.

## File map

| Path | Regenerated automatically? | What is lost if deleted? |
| --- | --- | --- |
| `stats.db` | **No** | Your entire recorded history — usage snapshots, cost snapshots, and the percent / 5-hour / budget milestones, reset events, and credit floors derived from them. Not re-derivable from JSONL. Back it up before touching it. |
| `stats.db-wal`, `stats.db-shm`, `cache.db-wal`, `cache.db-shm`, `conversations.db-wal`, `conversations.db-shm` | Yes — SQLite auto-manages them | Nothing once the owning DB is closed cleanly (they checkpoint back into the parent `.db`). Deleting them under a live writer can drop the most recent uncheckpointed writes. |
| `cache.db` | Yes — `cache-sync --rebuild`, or it rebuilds on the next read | Compact accounting, quota observations, source cursors, and Codex thread identity. Re-derived from your `~/.claude` / `~/.codex` JSONL. |
| `conversations.db` | Yes — `cache-sync --rebuild` or the dashboard conversation worker | Transcript prose/events, browse rollups, and full-text indexes. Independently re-derived from the same JSONL without blocking core accounting refresh. |
| `cache.db.repairing` | Yes — normally removed when recovery finishes; a stale owner is reclaimed by the next cache open/rebuild | No user data. It is an atomic repair-owner record (PID + process-start identity + claim token), not a lock file to delete by hand. |
| `cache.db.quarantine-pending.json` | Yes — removed only after every snapshotted family member reaches one completed quarantine incident | No user data. Durable interruption state for a partially moved cache main/WAL/SHM family; leave it for the next opener/rebuild to resume. |
| `quarantine/cache.db-*`, `logs/cache-corruption-forensics-*` | No — retained incident evidence | The damaged SQLite family and its diagnostic metadata. Safe recovery creates these before rebuilding; preserve them when investigating recurring corruption. |
| `cache.db.lock`, `cache.db.codex.lock`, `conversations.db.lock`, `conversations.db.codex.lock`, `conversations.db.maintenance.lock`, `config.json.lock` | Yes — `fcntl.flock` files, re-created on demand | Nothing — they carry no data. |
| `config.json` | Yes, **but only to defaults** | Your saved settings (`display.tz`, the `dashboard.*` keys, `telemetry.enabled`, week-start, budget, alert config, …). It comes back empty/default — your preferences are not recovered. See [configuration.md](configuration.md). |
| `install_id` | Yes, **but as a new identity** | Your anonymous telemetry identity rotates — a fresh random id mints on the next beat, so the install count may count you once more. Equivalent to `cctally telemetry reset`. Never leaves your machine. |
| `hwm-7d`, `hwm-5h` | Yes — climbs back from snapshots | The 7-day / 5-hour high-water-mark floor used by the status line and reports. It re-derives from `weekly_usage_snapshots` and re-climbs on subsequent ticks. |
| `pending-reset-zero-7d` | Yes — re-armed on the next tick | A transient reset-to-zero debounce marker. At worst the debounce re-arms (a real weekly reset then fires one tick later). Best-effort. |
| `update-state.json`, `update-suppress.json`, `update-check.last-fetch`, `update.lock`, `update.log`(`.1`) | Yes | Update-check bookkeeping plus your "skip this version" suppression — regenerating loses a dismissed-version choice and the last-check time (a fresh check just fires sooner). No user data. |
| `hook-tick.last-fetch`(`.lock`), `logs/` (`hook-tick.log`(`.1`), `migration-errors.log`, `record-usage` output) | Yes | Throttle timestamps and diagnostic logs only — no usage data. `logs/` re-creates on the next background call. |
| `telemetry.last-beat`, `telemetry.notice-shown`, `telemetry.first-seen` | Yes | Telemetry cadence markers (last-beat time, the one-time first-run notice flag, the first-seen grace anchor). Regenerating them may re-show the notice or re-open the 24-hour opt-out grace. |
| `stats.db.bak-*` | **No** — a manual backup | Whatever snapshot of `stats.db` you (or a recovery step) saved. It is a *backup*, not re-derivable; if it is your only copy of some history, that history is gone with it. |
| `data.db`, `usage.db` | n/a — legacy | Files from earlier iterations with no current writer. Safe to remove. |

## `stats.db` schema

A **disposable index** materialized from the append-only journal (`~/.local/share/cctally/journal/`), which is the durable truth since the DB journal redesign (§7.1). `stats.db` is stamped at a single `STATS_INDEX_EPOCH` (1000) rather than versioned by migrations; its 13-migration legacy registry is **frozen** (only used to bring a pre-cutover install to the export baseline). A version mismatch — newer or older binary — self-heals by **rebuild from the journal** (`cctally db rebuild --db stats`); `DowngradeDetected`-bricking no longer applies to `stats.db`, and `db recover --db stats` is retired. Corruption self-heals the same way (forensics → quarantine → rebuild, no human step). A schema change bumps the epoch, never adds a stats migration.

The live schema is **15 tables**. The three original snapshot tables keep their full detail below; the rest are the 5-hour, reset/credit, budget, and framework-ledger tables added since.

| Table | Purpose |
| --- | --- |
| `weekly_usage_snapshots` | One row per `record-usage` tick — 7-day usage % and the optional 5-hour bucket (detailed below). |
| `weekly_cost_snapshots` | One row per `sync-week` — computed USD cost for a week window (detailed below). |
| `percent_milestones` | One row per integer weekly-percent crossing per week per reset segment (detailed below). |
| `five_hour_blocks` | One row per API-anchored 5-hour block, with materialized cost and token totals recomputed each tick. |
| `five_hour_block_models` | Per-model rollup child rows for a `five_hour_blocks` row. |
| `five_hour_block_projects` | Per-project rollup child rows for a `five_hour_blocks` row. |
| `five_hour_milestones` | Per-percent cost milestones inside a 5-hour block (the 5h analogue of `percent_milestones`). |
| `five_hour_reset_events` | Recorded 5-hour reset boundaries / in-place 5h credit segments. |
| `week_reset_events` | Recorded weekly re-anchor events (≥25pp / reset-to-zero auto-credit path). |
| `weekly_credit_floors` | In-place weekly partial-credit floors written by `record-credit` (framework-untracked table). |
| `budget_milestones` | Vendor-tagged budget-threshold crossings — `UNIQUE(vendor, period_start_at, period, threshold)`, `vendor ∈ {claude, codex}`. |
| `project_budget_milestones` | Per-project budget-threshold crossings. |
| `projected_milestones` | Projected-pace alert crossings (the forecast "on track to cap" axis). |
| `schema_migrations` | Migration-framework ledger — applied handlers (name + timestamp). |
| `schema_migrations_skipped` | Migration-framework ledger — skipped handlers (name + timestamp + reason). |

### `weekly_usage_snapshots`

One row per `record-usage` call. Tracks 7-day usage % and (optionally) the 5-hour bucket.

Key columns: `captured_at_utc`, `week_start_date`, `week_end_date`, `week_start_at` (hour-accurate ISO), `week_end_at`, `weekly_percent`, `five_hour_percent`, `five_hour_resets_at`, `payload_json` (raw rate_limits blob).

### `weekly_cost_snapshots`

One row per `sync-week` invocation. Stores the computed USD cost for a week window, plus the calculation `mode` (`auto` / `calculate` / `display`) and optional `project` filter.

### `percent_milestones`

`UNIQUE(week_start_date, percent_threshold, reset_event_id)` — one row per integer percent crossing per week **per reset segment** (the `reset_event_id` column, default `0`, segments a week that was re-anchored or credited mid-week). Written by `record-usage` when a snapshot crosses a new threshold. Stores `cumulative_cost_usd`, `marginal_cost_usd`, and (since A1) `five_hour_percent_at_crossing`.

**Never backfilled.** A milestone written today reflects reality at *that* moment; rewriting it later with current cost would erase the historical marginal-cost signal.

## `cache.db` schema

Fully re-derivable from JSONL through `cache-sync --rebuild`. It carries the
compact Claude and Codex accounting/quota estates. Transcript/search state lives
separately in `conversations.db`; do not unlink either SQLite family beneath a
live process. Classified corruption is preserved in forensics/quarantine only
after the maintenance handshake proves the main/WAL/SHM family has no live
handles. The failed open or ingest is retried once against the recreated cache;
`--source claude|codex|all` still controls which provider rows are re-derived,
and `all` restarts both provider legs if either leg triggers family replacement.
An atomic pending-quarantine record makes the three family renames resumable;
recreation waits until the complete snapshotted family is present in one
incident directory.

### Claude side

- `session_files` — per-JSONL ingest checkpoint (`path`, `size_bytes`, `mtime_ns`, `last_byte_offset`, `last_ingested_at`, `session_id`, `project_path`)
- `session_entries` — one row per assistant message (`source_path`, `line_offset`, `timestamp_utc`, `model`, `msg_id`, `req_id`, token counts, `cost_usd_raw`)

`UNIQUE(msg_id, req_id)` partial index dedups across resumed sessions.

### Codex side

- `codex_session_files` — same shape plus `last_session_id`, `last_model`, `last_total_tokens`
- `codex_session_entries` — `UNIQUE(source_path, line_offset)` per `event_msg.token_count` event

### Conversation viewer

The read-only transcript reader is backed by `conversations.db`: `conversation_sessions`, `conversation_messages`, `conversation_file_touches`, `conversation_ai_titles`, the consolidated `conversation_fts(text, search_tool, search_thinking)` index, the parallel Codex event/normalized/rollup families, independent source cursors, and transcript-only `cache_meta` rebuild flags. A conversation connection may attach `cache.db` read-only for cost/token joins; core cache connections never attach `conversations.db`. Both stores re-derive independently from JSONL; see [dashboard.md](commands/dashboard.md) for the reader, privacy gate, and search-depth surface.

### Cost

Pricing-derived cost is **not stored**: it is computed at query time from `CLAUDE_MODEL_PRICING` / `CODEX_MODEL_PRICING`, so a pricing-dict edit takes effect on the next read with no invalidation. The one recorded-cost value that *is* cached is `session_entries.cost_usd_raw` — the raw `costUSD` from the JSONL line when the vendor supplied one (most modern Claude Code sessions omit it).

## Safe destructive ops

```bash
rm ~/.local/share/cctally/hwm-7d            # high-water mark resets, re-climbs from snapshots
cctally cache-sync --rebuild                # explicit cache rebuild
cctally cache-sync --source codex --rebuild # Codex half only
```

`conversations.db` is re-derivable, but never unlink its main file beneath a
live dashboard or other cctally process: SQLite's main/WAL/SHM files are one
family. Prefer `cache-sync --rebuild`. To recover a corrupt transcript store,
stop every cctally process first, then move the complete
`conversations.db`/`conversations.db-wal`/`conversations.db-shm` family aside
together before rebuilding. Two files deserve a harder pause: `stats.db` (and
any `stats.db.bak-*` that is your only backup of it) holds weeks of history that
cannot be recovered, and `config.json` comes back only as empty defaults, so
deleting it discards your saved settings.
