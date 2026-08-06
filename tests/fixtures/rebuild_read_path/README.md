# Pre-change canonical rebuild dumps (#496 S4)

These two files are the equivalence oracle for #496 session S4 ("Make the rebuild read path cheap"). They were captured from the **pre-change** `rebuild_stats_index` at commit `26bd50f5f`, before the streaming read path replaced the two whole-journal traversals. The pre-change implementation does not survive that change, so the reference has to be retained rather than recomputed.

## What each file pins

Both are the `canonical_view` of a `tests/_rebuild_worker_496_s4.py` dump: every row of every table in `_REBUILD_COUNT_TABLES`, all of `journal_effective_events` ordered by `event_id`, `journal_protocol_violations`, `accounts.last_seen_utc`, the cache's `codex_file_accounts` and `codex_file_incarnations`, and a row count plus SHA-256 digest over `quota_window_snapshots` (12,000-odd rows would otherwise be an unreadable multi-megabyte file; the digest still detects drift in any column of any row). Two nonce-valued columns are canonicalized to stable ordinals — `quota_percent_milestones.generation` is minted per re-materialization, so two identical rebuilds legitimately disagree on its literal value.

- `tier1-full-prefix.json` — an unpinned rebuild over the whole Tier 1 fixture. Exercises inline cutover capture.
- `tier1-pinned-before-cutover.json` — a rebuild pinned to the line boundary immediately before the canonical cutover op. Placement alone never reaches the §5.1 suffix fallback, because an unpinned rebuild always uses the current full high-water; this scenario is what proves the pinned-prefix answer is unchanged. Its `accounts.last_seen_utc` for the Claude account is `2024-01-01T02:18:00Z` against the full prefix's `02:29:00Z`, and both resolve the same cutover account — a rebuild that lost the fallback would restamp every legacy Claude observation to `unattributed` and move that column.

## The fixture they were captured over

`bin/build-journal-benchmark-fixture.py::build(target_lines=12000, seed_cache=True)`, which is deterministic and asserts its own production-shaped mix (roughly 93 % Codex quota observations, mean encoded line near the measured 880.6 bytes, three bootstrap plus three observation segments, the cutover op near 92.9 %, all four correction shapes, one `journal_protocol_resolution` op, and a seeded `cache.db` whose quota rows are initially absent).

## Regenerating

Do **not** regenerate these to make a failing comparison pass — the whole point is that they predate the change. If the fixture builder itself legitimately changes, recapture them from a checkout of the pre-change implementation, not from the current tree, and say so in the commit body.
