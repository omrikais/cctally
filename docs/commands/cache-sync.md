# `cache-sync`

Prime or rebuild the compact accounting cache (`cache.db`) and independent transcript/search store (`conversations.db`).

## Synopsis

```
cctally cache-sync
    [--rebuild]
    [--prune-orphans]
    [--prune-conversations]
    [--source {claude,codex,all}]
```

## Purpose

Most commands trigger an incremental delta-ingest implicitly when they
run. Use `cache-sync` when you want to:

- Force the cache up-to-date *now* (so the next interactive command is fast).
- Rebuild from scratch after deleting `cache.db`, schema changes, or
  pricing-dict edits where you want a clean re-derivation (note:
  pricing edits don't actually require a rebuild — cost is computed at
  query time, not stored).
- Recover a corrupt `cache.db` or `conversations.db` without moving or
  unlinking SQLite files by hand. Positively confirmed corruption is preserved
  for forensics, the complete main/WAL/SHM family is quarantined after live
  readers drain, and the requested source scope is re-ingested. The default
  `--source all` recovery rebuilds both transcript providers.
- Prune cache rows left behind by session directories that were removed from disk (for example a deleted git worktree), without paying for a full rebuild — see `--prune-orphans` below.
- Limit work to one source (Claude or Codex) when the other half is large.

## Options

| Flag | Description |
| --- | --- |
| `--rebuild` | Drop all cached entries and re-ingest from scratch. It first checks an existing transcript store and safely recovers confirmed corruption. Waits up to 30s for each provider lock; each transcript-provider phase has a 30-minute no-progress ceiling and exits non-zero if incomplete (see Notes). |
| `--prune-orphans` | Remove cache rows for source files no longer on disk, without a full rebuild (Claude cache only). |
| `--prune-conversations` | Prune conversation transcripts older than `conversation.retention_days` (default 90) right now, without a full rebuild. Reports the rows removed per provider. See `--prune-conversations` below. |
| `--source {claude,codex,all}` | Which ingest half to sync/rebuild. Default `all`. |

## `--prune-orphans`

When Claude Code sessions run inside a git worktree (or any directory) that is later removed, Claude Code deletes that directory's `~/.claude/projects/<encoded-dir>/` transcripts — but `cache.db` keeps tracking their derived accounting rows while `conversations.db` retains their transcript rows. `--prune-orphans` safely cleans both stores directly, far faster than re-ingesting everything with `--rebuild`.

The prune is deliberately conservative. It removes an orphaned file's rows only when it can prove the removal is safe under three gates: the orphan's session is not shared by any surviving on-disk file; every one of the orphan's billable turns has full conversation evidence under its own path; and none of those turns is physically held by a surviving file (so a deduped cost row a survivor still owns is never dropped). Anything it cannot prove safe is left in place and reported as a residual — the command tells you how many orphans it left and points you at `cache-sync --rebuild`, which re-derives the whole cache and clears everything unconditionally.

## `--prune-conversations`

Conversation transcripts (`conversations.db`'s `conversation_messages` and Codex `codex_conversation_events`, plus their FTS indexes) grow without bound — a normal sync only ever adds. `--prune-conversations` removes transcripts older than `conversation.retention_days` (default 90; set `cctally config set conversation.retention_days off` to disable, or a positive integer to change the window) right now, so you don't have to wait for the dashboard's automatic once-a-day pass.

Eligibility is decided per conversation from the authoritative message rows, never a rollup: a session (Claude) or conversation (Codex) is pruned only when **every** one of its messages is older than the cutoff — a conversation with any recent activity is kept whole. Only transcript rows are removed; cost/usage history (`daily`/`weekly`/`report`/…) and compact Codex analytics metadata are untouched, and everything pruned is re-derivable from the underlying JSONL. Each whole conversation is committed separately while the conversation-store maintenance and provider locks remain held for the full pass, bounding WAL growth without exposing a partially deleted conversation. Deleting rows frees pages inside `conversations.db`; run `cctally db vacuum --db conversations` when a legacy non-incremental store needs explicit compaction.

The command reports the number of sessions/messages (Claude) and conversations/events (Codex) removed. It skips (exit 1) if a sync or another maintenance operation is holding the conversation-store locks — retry shortly.

You rarely need to run this by hand: the dashboard self-heals these orphans automatically (once at startup and periodically while running), so `--prune-orphans` is mainly for headless or one-off cleanup.

## Examples

```bash
cctally cache-sync
cctally cache-sync --rebuild
cctally cache-sync --prune-orphans
cctally cache-sync --source codex --rebuild
cctally cache-sync --source claude
```

## Notes

- `cache.db` lives at `~/.local/share/cctally/cache.db`.
- `conversations.db` lives beside it and has independent Claude/Codex cursors and locks.
- Core accounting/quota commits before transcript ingestion. If the transcript
  store is unavailable, a routine sync reports that degradation but retains a
  successful core result; an explicit `--rebuild` exits non-zero because the
  requested full rebuild was incomplete.
- Large histories can spend much longer rebuilding transcript/search rows than
  compact accounting rows. Explicit rebuilds report the active transcript
  provider and phase (`open`, `sync-start`, `lock`, `prepare`, `ingest`,
  `rollup`/`finalize`, `checkpoint`, `retention`, `close`) with monotonic
  elapsed time, plus file progress every 200 files.
- Each explicit Claude or Codex transcript rebuild runs in its own process with
  a 30-minute no-progress ceiling. Each phase transition or completed/skipped
  source file refreshes that bound, so a large rebuild that is still advancing
  is not stopped by an arbitrary total wall-clock limit. If a phase emits no
  progress for 30 minutes, cctally terminates the worker, exits non-zero, and names
  `provider=… store=conversations.db phase=…`. Already-committed core
  accounting/quota rows remain intact. SQLite rolls back only the active
  transcript transaction and previously committed transcript files remain
  integrity-clean. If the clean full walk/finalization had not completed, its
  durable provider rebuild marker also keeps the partial store retry-required.
  In every phase, re-run the provider-specific command printed in the diagnostic.
- Concurrent ingests are serialized by `fcntl.flock` on
  `cache.db.lock` / `cache.db.codex.lock` for accounting and
  `conversations.db.lock` / `conversations.db.codex.lock` for transcripts.
  Routine auto-syncs that lose the race read the existing store without blocking.
- `--rebuild` is different: it waits up to 30 seconds for each cache or
  transcript provider lock, then exits non-zero if it still can't acquire one
  (for example while a dashboard is actively syncing), instead of silently
  doing nothing and reporting success. Re-run it once the other process
  releases the lock. `--prune-orphans` behaves the same way for the cache lock.
- Corrupt-file recovery is classifier-gated for both derived stores: lock
  contention, permissions, disk-full errors, and SQL mistakes are never
  destructive recovery signals. Recovery writes a forensics bundle with the
  precise trigger origin, then quarantines the complete SQLite family only
  after the store maintenance lock, every relevant provider lock, and the
  open-handle drain checks pass **and** `integrity_check` confirms corruption.
  An `ok`, unavailable, or unwritable probe preserves the family and propagates
  the original failure. Confirmed recovery recreates the affected store and
  retries the requested provider plan once. If
  corruption occurs in the Codex leg of `--source all`, Claude is re-ingested
  again on the replacement family; a failed or contended provider walk remains
  non-zero.
- `cache.db.repairing` records a repair PID, kernel process-start identity, and
  unique claim token. A live matching owner blocks new opens. Dead, malformed,
  or PID-reused ownership is reclaimed under the maintenance lock; platforms
  that cannot re-read a process-start identity use a bounded 30-minute lease.
  If a repair is killed at any phase, the next opener or
  `cache-sync --rebuild` resumes from the surviving corrupt/quarantined/fresh
  state without manual marker deletion.
- `conversations.db.repairing` uses the same process-start-qualified ownership,
  while `conversations.db.recovery.json` durably records every transcript
  provider that must be rebuilt after whole-family quarantine. A killed
  recovery reclaims stale ownership and finishes the recorded provider set on
  the next `cache-sync --rebuild`, even when that retry names only one source;
  the recovery record clears only after all pending provider markers clear.
  Public transcript readers, bounded title decoration, and Doctor decline the
  store while that record exists, so an empty or one-provider intermediate
  rebuild is never published as healthy.
- Transcript integrity probes use a same-volume copy-on-write clone of the
  locked main/WAL family, bounded to five seconds per member. They never fall
  back to a full byte copy; when the filesystem lacks clone/reflink support,
  recovery fails closed with the live family untouched. Stale private probe
  directories from a killed owner are removed under the same exclusion locks.
- Whole-family quarantine publishes
  `<store>.quarantine-pending.json` before its first rename. The next
  maintenance-exclusive opener completes the same incident after a killed
  owner or move failure; it does not create a fresh DB until every snapshotted
  main/WAL/SHM member is accounted for in quarantine.
- Both stores are fully re-derivable from JSONL. Prefer
  `cctally cache-sync --rebuild`; never unlink a SQLite main/WAL/SHM family
  while any cctally process may still have it open.
- Cost is **not** stored in the cache; pricing-dict updates are visible
  on the next read with no rebuild required.

## See also

- [Architecture · cache.db](../architecture.md#the-session-entry-cache-cachedb)
- [Runtime data · cache.db schema](../runtime-data.md#cachedb-schema)
