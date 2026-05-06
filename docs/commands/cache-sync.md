# `cache-sync`

Prime or rebuild the session-entry cache (`cache.db`).

## Synopsis

```
cctally cache-sync
    [--rebuild]
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
- Limit work to one source (Claude or Codex) when the other half is large.

## Options

| Flag | Description |
| --- | --- |
| `--rebuild` | Drop all cached entries and re-ingest from scratch. |
| `--source {claude,codex,all}` | Which ingest half to sync/rebuild. Default `all`. |

## Examples

```bash
cctally cache-sync
cctally cache-sync --rebuild
cctally cache-sync --source codex --rebuild
cctally cache-sync --source claude
```

## Notes

- `cache.db` lives at `~/.local/share/cctally/cache.db`.
- Concurrent ingests are serialized by `fcntl.flock` on
  `cache.db.lock` (Claude) and `cache.db.codex.lock` (Codex). Losers
  read the existing cache without blocking.
- The cache is fully re-derivable from JSONL — `rm cache.db` is always safe.
- Cost is **not** stored in the cache; pricing-dict updates are visible
  on the next read with no rebuild required.

## See also

- [Architecture · cache.db](../architecture.md#the-session-entry-cache-cachedb)
- [Runtime data · cache.db schema](../runtime-data.md#cachedb-schema)
