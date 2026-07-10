# Ancient-DB → head long-haul fixtures (#279 S7 W2)

Frozen historical DB fixtures driven end-to-end by
`tests/test_migration_ancient_to_head.py`.

- `stats.sqlite` — a pre-framework stats.db (`user_version=0`, no
  `schema_migrations`) in the historical shapes the 12 stats migrations
  transform.
- `cache.sqlite` — a pre-001 legacy cache.db (duplicate `session_entries`, no
  conversation tables, no `speed` column) for the cache 001 dedup-wipe.
- `cache-midera.sqlite` — the mid-era legacy unsplit-FTS cache shape for the
  010 → 016 → 018 FTS interaction.
- `corpus/projects/-fake-proj/sess-a.jsonl` — the synthetic JSONL a real
  `sync_cache` repopulates the wiped cache from (so the 008/009/010 recompute
  gate is satisfied).

## FREEZE DISCIPLINE — regen is deliberate-only

The schemas inside `bin/build-ancient-migration-fixtures.py` are HISTORICAL
CONSTANTS. Do NOT "refresh" them from `_apply_cache_schema` / `create_stats_db`
/ current `_cctally_core` DDL — the whole point is to represent shapes the
current code no longer emits, so the migration chain has something real to
migrate. Regenerate ONLY when a deliberate decision requires it (e.g. a new
migration needs a new legacy column), by running
`python3 bin/build-ancient-migration-fixtures.py` and hand-reviewing the diff.
The builder is intentionally NOT a `build_per_migration_*` in
`build-migrations-fixtures.py`, so the #197 byte-idempotency guard does not
auto-discover it.
