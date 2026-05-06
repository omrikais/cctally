# Runtime data

All persistent state lives under `~/.local/share/cctally/`.
Nothing here is committed to the repo. Everything except `stats.db` is
fully re-derivable.

## File map

| Path | Purpose | Re-derivable? |
| --- | --- | --- |
| `stats.db` | Authoritative SQLite — usage snapshots, cost snapshots, percent milestones | **No** — historical record |
| `cache.db` | Read-through delta cache for JSONL session entries (Claude + Codex) | Yes — `cache-sync --rebuild` |
| `cache.db.lock` | `fcntl.flock` serializing concurrent Claude ingests | Yes (auto-managed) |
| `cache.db.codex.lock` | Same, for Codex ingests | Yes (auto-managed) |
| `config.json` | Collector + week-start config (see [configuration.md](configuration.md)) | Yes — defaults regenerate |
| `hwm-7d` | High-water mark floor for the 7-day usage % display | Yes (climbs back from snapshots) |
| `logs/` | Diagnostic logs from background `record-usage` calls | Yes |
| `data.db`, `usage.db`, `stats.db.bak-*` | Legacy / backup files from earlier iterations | Yes |
| `claude-usage-sync.user.js` | Userscript artifact (separate concern) | n/a |

## `stats.db` schema

Three tables, all created in `open_db()` with inline `ALTER TABLE` migrations
(no migration framework):

### `weekly_usage_snapshots`

One row per `record-usage` call. Tracks 7-day usage % and (optionally) the
5-hour bucket.

Key columns: `captured_at_utc`, `week_start_date`, `week_end_date`,
`week_start_at` (hour-accurate ISO), `week_end_at`, `weekly_percent`,
`five_hour_percent`, `five_hour_resets_at`, `payload_json` (raw rate_limits
blob).

### `weekly_cost_snapshots`

One row per `sync-week` invocation. Stores the computed USD cost for a week
window, plus the calculation `mode` (`auto` / `calculate` / `display`) and
optional `project` filter.

### `percent_milestones`

`UNIQUE(week_start_date, percent_threshold)` — one row per integer percent
crossing per week. Written by `record-usage` when a snapshot crosses a new
threshold. Stores `cumulative_cost_usd`, `marginal_cost_usd`, and (since
A1) `five_hour_percent_at_crossing`.

**Never backfilled.** A milestone written today reflects reality at *that*
moment; rewriting it later with current cost would erase the historical
marginal-cost signal.

## `cache.db` schema

### Claude side

- `session_files` — per-JSONL ingest checkpoint (`path`, `size_bytes`,
  `mtime_ns`, `last_byte_offset`, `last_ingested_at`, `session_id`,
  `project_path`)
- `session_entries` — one row per assistant message (`source_path`,
  `line_offset`, `timestamp_utc`, `model`, `msg_id`, `req_id`, token counts,
  `cost_usd_raw`)

`UNIQUE(msg_id, req_id)` partial index dedups across resumed sessions.

### Codex side

- `codex_session_files` — same shape plus `last_session_id`, `last_model`,
  `last_total_tokens`
- `codex_session_entries` — `UNIQUE(source_path, line_offset)` per
  `event_msg.token_count` event

Cost is **never stored** in `cache.db` — it's computed at query time from
`CLAUDE_MODEL_PRICING` / `CODEX_MODEL_PRICING` so pricing-dict updates take
effect immediately.

## Safe destructive ops

```bash
rm ~/.local/share/cctally/cache.db          # rebuild on next read
rm ~/.local/share/cctally/hwm-7d            # high-water mark resets
cctally cache-sync --rebuild          # explicit cache rebuild
cctally cache-sync --source codex --rebuild  # Codex half only
```

`stats.db` is the only file you should hesitate before deleting — it has
weeks of history that can't be recovered.
