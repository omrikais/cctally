# `cache-sync`

Prime or rebuild the session-entry cache (`cache.db`).

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
- Prune cache rows left behind by session directories that were removed from disk (for example a deleted git worktree), without paying for a full rebuild — see `--prune-orphans` below.
- Limit work to one source (Claude or Codex) when the other half is large.

## Options

| Flag | Description |
| --- | --- |
| `--rebuild` | Drop all cached entries and re-ingest from scratch. Waits up to 30s for the cache lock and exits non-zero if it can't acquire it (see Notes). |
| `--prune-orphans` | Remove cache rows for source files no longer on disk, without a full rebuild (Claude cache only). |
| `--prune-conversations` | Prune conversation transcripts older than `conversation.retention_days` (default 180) right now, without a full rebuild. Reports the rows removed per provider. See `--prune-conversations` below. |
| `--source {claude,codex,all}` | Which ingest half to sync/rebuild. Default `all`. |

## `--prune-orphans`

When Claude Code sessions run inside a git worktree (or any directory) that is later removed, Claude Code deletes that directory's `~/.claude/projects/<encoded-dir>/` transcripts — but `cache.db` keeps tracking those now-deleted JSONL files and all their derived cost and conversation rows, because a normal sync only ever *adds* on-disk files and never prunes. `--prune-orphans` cleans them up directly, far faster than re-ingesting everything with `--rebuild`.

The prune is deliberately conservative. It removes an orphaned file's rows only when it can prove the removal is safe under three gates: the orphan's session is not shared by any surviving on-disk file; every one of the orphan's billable turns has full conversation evidence under its own path; and none of those turns is physically held by a surviving file (so a deduped cost row a survivor still owns is never dropped). Anything it cannot prove safe is left in place and reported as a residual — the command tells you how many orphans it left and points you at `cache-sync --rebuild`, which re-derives the whole cache and clears everything unconditionally.

## `--prune-conversations`

Conversation transcripts (`cache.db`'s `conversation_messages` and Codex `codex_conversation_events`, plus the FTS search index over them) grow without bound — a normal sync only ever adds. `--prune-conversations` removes transcripts older than `conversation.retention_days` (default 180; set `cctally config set conversation.retention_days off` to disable, or a positive integer to change the window) right now, so you don't have to wait for the dashboard's automatic once-a-day pass.

Eligibility is decided per conversation from the authoritative message rows, never a rollup: a session (Claude) or conversation (Codex) is pruned only when **every** one of its messages is older than the cutoff — a conversation with any recent activity is kept whole. Only the transcript rows are removed; the cost/usage history (`daily`/`weekly`/`report`/…) and the Codex analytics metadata are untouched, and everything pruned is re-derivable from the underlying JSONL. Deleting the rows frees pages inside `cache.db` but does not shrink the file on disk — run [`cctally db vacuum`](db.md#db-vacuum) to reclaim that space.

The command reports the number of sessions/messages (Claude) and conversations/events (Codex) removed. It skips (exit 3) if a sync or another maintenance operation is holding the cache locks — retry shortly.

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
- Concurrent ingests are serialized by `fcntl.flock` on
  `cache.db.lock` (Claude) and `cache.db.codex.lock` (Codex). Routine auto-syncs that lose the race read the existing cache without blocking.
- `--rebuild` is different: it waits up to 30 seconds for the cache lock, then exits non-zero if it still can't acquire it (for example while a dashboard is actively syncing), instead of silently doing nothing and reporting success. Re-run it once the other process releases the lock. `--prune-orphans` behaves the same way.
- The cache is fully re-derivable from JSONL — `rm cache.db` is always safe.
- Cost is **not** stored in the cache; pricing-dict updates are visible
  on the next read with no rebuild required.

## See also

- [Architecture · cache.db](../architecture.md#the-session-entry-cache-cachedb)
- [Runtime data · cache.db schema](../runtime-data.md#cachedb-schema)
