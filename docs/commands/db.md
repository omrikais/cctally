# `cctally db`

Migration / DB-management subcommand. Eleven actions: `status`, `skip`,
`unskip`, `rebuild`, `rederive`, `journal-repair`, `recover`, `repair`,
`backup`, `checkpoint`, and `vacuum`.

## Synopsis

```
cctally db status [--json]
cctally db skip <migration-name> [--reason "<text>"]
cctally db unskip <migration-name>
cctally db rebuild --db stats [--json]
cctally db rederive --family claude-usage [--yes] [--json]
cctally db journal-repair [--violation <fingerprint> ...] [--yes] [--json]
cctally db recover --db cache [--yes]   # --db stats is retired (see below)
cctally db repair --db stats --yes
cctally db backup --db {cache,stats} [--output <path>]
cctally db checkpoint [--db {cache,stats}] [--json]
cctally db vacuum [--db {cache,conversations,stats,all}]
```

## Description

`cctally` runs schema migrations on `open_cache_db()` (cache.db) and
`open_conversations_db()` (conversations.db) via a small in-process
framework. Migrations are numbered (`001_…`, `002_…`), per-DB, registered
via `@cache_migration` / `@conversations_migration` decorators in
`bin/cctally`. The `db` subcommand surfaces this state and offers a manual
poison-pill escape.

**`stats.db` is different since the journal redesign (§7.1):** it is a
disposable index materialized from the append-only journal, stamped at a
single `STATS_INDEX_EPOCH` (1004) rather than versioned by migrations.
Its 13-migration legacy registry is **frozen** — no new stats migration is
ever written; a schema change bumps the epoch, and any version mismatch
self-heals by **rebuild** (`db rebuild --db stats`), never by trim-and-revert.
`db status` still lists the frozen stats registry for provenance.

Spec: `docs/superpowers/specs/2026-05-06-migration-framework-design.md`,
`docs/superpowers/specs/2026-07-22-db-journal-redesign-design.md`.

## `cctally db status`

Renders applied / pending / failed / skipped state for every migration
in both DBs.

| Flag | Description |
| --- | --- |
| `--json` | Emit machine-readable JSON (`schema_version: 1`) instead of human-readable text. |

### Text output

```
$ cctally db status
stats.db (~/.local/share/cctally/stats.db)  version 4 / 4 known
  ✓ 001_five_hour_block_models_backfill_v1   applied 2026-04-30T12:34:56Z
  ✓ 002_five_hour_block_projects_backfill_v1 applied 2026-04-30T12:34:56Z
  ✓ 003_merge_5h_block_duplicates_v1         applied 2026-05-04T08:12:11Z
  ✓ 004_some_new_thing                       applied 2026-05-06T09:15:00Z

cache.db (~/.local/share/cctally/cache.db)  version 1 / 1 known
  ✓ 001_codex_total_tokens                   applied 2026-04-22T11:22:33Z
```

Glyphs: `✓` applied, `✗` failed, `·` pending, `~` skipped. The `version
N / M known` header reads `PRAGMA user_version` for `N` and the
in-memory registry length for `M`.

### Exit codes

`0` success.

## `cctally db skip <name>`

Marks a migration as skipped — the dispatcher will not invoke its
handler. For migrations that genuinely cannot succeed on a particular
machine.

| Flag | Description |
| --- | --- |
| `--reason "<text>"` | Free-text reason; surfaced in `db status`. Recommended. |

`<name>` accepts:
- Bare form (`003_merge_5h_block_duplicates_v1`) — looked up in both
  registries; ambiguous if it appears in both.
- Qualified form (`stats.db:003_…` or `cache.db:003_…`) — looked up
  only in the named registry.

### Exit codes

`0` success; `1` already applied / already skipped / unknown name; `2`
ambiguous bare name (must qualify).

## `cctally db unskip <name>`

Removes a skip mark. The migration runs again on the next `open_db()` /
`open_cache_db()`.

This command also writes `PRAGMA user_version = 0` to invalidate the
dispatcher's fast-path cache. Without this invalidation, a DB whose
`user_version == len(registry)` (achieved when every migration is
applied OR skipped) would short-circuit the next open and never
re-check the now-empty skip set. The `0` value forces a full registry
walk.

### Exit codes

`0` success (including no-op when the migration wasn't skipped); `1`
unknown name; `2` ambiguous bare name.

## `cctally db rebuild --db stats`

Rebuilds `stats.db` from the append-only journal (DB journal redesign
§9). Since the journal redesign, `stats.db` is a **disposable index**
materialized from `~/.local/share/cctally/journal/` — the durable truth
is the journal, not the SQLite file. `db rebuild` is the explicit
remediation surface: it replays the whole journal into a fresh scratch
index, validates it, and then **publishes it transactionally into the
live `stats.db`** (#496 S3), reporting per-table row counts, lines
folded, and duration.

Publication is chosen by whether the destination can be operated on, not
by what triggered the rebuild. The normal case is the in-place publish
above, and it **leaves no quarantined copy** — preservation is a
consequence of destroying a file, and an in-place publish destroys
nothing. Use `cctally db backup --db stats` when you want a snapshot
before rebuilding. Physical replacement is the fallback, taken only when
the destination is structurally unusable (`SQLITE_CORRUPT` /
`SQLITE_NOTADB`); that path still forensics-quarantines the old family
first, and only then does the command print the "previous stats.db
quarantined" line and fill in `quarantineDir`.

It is held under the stats **maintenance lock** and takes the bounded
**ingest lock**, so it serializes with a concurrent auto-heal and with a
live ingest cycle. Journal replay is side-effect-free — a rebuild
**never fires an alert**.

| Flag | Description |
| --- | --- |
| `--db stats` | **Required.** Only `stats.db` is journal-backed. cache.db / conversations.db rebuild from surviving provider JSONL — use `cctally cache-sync --rebuild`. |
| `--json` | Emit a `schemaVersion: 1` envelope (`segmentsRead`, `linesFolded`, `malformed`, `durationSeconds`, `rowsByTable`, `totalRows`, `quarantineDir`, `forensicsPath`, `journalConflicts`, `journalProtocolViolations`, `journalAcknowledgedProtocolViolations`, `selectorDesynchronized`, `quotaCacheCoverage`, `segmentElision`, `publication`, `cacheRecovery`) instead of text. `selectorDesynchronized` is `null` in the healthy case; when the replaced index's durable selector prefix sat behind its own applied journal cursor it carries `coveredSegment` / `coveredOffset` / `cursorSegment` / `cursorOffset` / `gapBytes` / `gapByteCap` / `gapExceedsCap`. An ingest pass that cannot validate the durable generation advances the cursor without advancing the selector, and the next pass re-folds the gap silently, so this is the only surface on which a repeatedly degrading generation check is visible. The re-fold is capped: `gapBytes` is the distance between the two coordinates (`null` when it could not be determined), `gapByteCap` is the cap, and `gapExceedsCap` is `true` when the gap is wide enough that the live path stops re-folding it and degrades to full selection on every tick until a rebuild realigns the two. `quotaCacheCoverage` reports what the Codex quota cache leg did: `status` is `covered` when the journal-to-cache coverage certificate proved the cache already held every cache-relevant record in the pinned prefix, in which case the leg took **no** cache writer flock and replayed nothing; `recovered` when it replayed; `skipped` when the journal carried nothing for it; and `failed` when the cache write did not commit. `reason` names the coverage verdict, `coveredHighWater` the boundary reached, and `replayedObservations` how many observations were re-materialized. `publication` and `cacheRecovery` state the rebuild's two outcomes separately, because publication success and cache-recovery completeness are different questions and a consumer must not be able to read one as the other. `publication` carries `ok` (reaching this payload at all means the index was built, validated and published, so it is always `true`) and `statsQuotaProjectionIncomplete` (whether the published generation carries the durable incomplete-quota-projection flag, which gates every quota-projection read until a later `cctally cache-sync` or dashboard start reconciles it). `cacheRecovery` carries `phase` (`covered`, `recovered`, `skipped`, `incomplete`, `failed`, or `notRun` when the leg did not run at all), `complete` (whether the covered prefix reached the pinned target — always a boolean, never `null`; a leg that never ran had no duty and reports `true`), `coveredHighWater` (the boundary reached, `null` when none was resolvable) and `remainder` (`null` when complete, otherwise `observations`, `chunksRemaining` and `reason`). A rebuild with an uncovered remainder still exits 0 and still prints the existing success line. `segmentElision` reports which journal segments the rebuild skipped instead of reading, and why it skipped or read each one. A rebuild may skip a segment only when the coverage certificate proves the cache already holds every record in it and the segment holds nothing else the stats index needs, so this block is how an operator sees whether that shortcut applied at all. It carries `elidedSegments`, `elidedLines` and `elidedBytes` (what was skipped), `scannedSegments` (how many were read instead), `coverage` (the certificate verdict the plan was made against, `ok` when it was usable), `resolutionSeen` (`true` once a `journal_protocol_resolution` operation was decoded, after which nothing further is skipped), and `refusals`, a `{segment: reason}` map naming the first condition each segment failed. Every refusal is a silent full read — there is no stderr line for one — so this block and the rebuild record are the only places a skipped or refused segment is reported. `elidedSegments: 0` with a populated `refusals` map is the ordinary state on an install that has not yet completed a rebuild, because the summaries a plan needs are written by the pass before it. `segmentsRead` is unrelated to this block and its meaning is unchanged: it counts the journal segments in the pinned prefix, which is what it counted before segment skipping existed. It is not a count of segments physically opened; `segmentElision.scannedSegments` is that number. |

- **Prod guard (issue #146).** A **dev/worktree checkout** binary
  refuses to rebuild the real prod `stats.db` (`~/.local/share/cctally`)
  unless overridden with `CCTALLY_ALLOW_PROD_MIGRATION=1`. Run the
  installed binary instead.
- Auto-heal already runs this path automatically on positively-classified
  corruption (forensics → quarantine → rebuild → retry, no human step);
  `db rebuild` is the manual, on-demand form.

### Quarantined same-revision groups (#374)

A journal written by a pre-quarantine binary can contain two or more `evt`
lines sharing an `(id, rev)` with different content — a state the append-only
journal can never un-write. The rebuild no longer aborts on that: it selects the
**first-written** variant as a provisional winner and reports the group. The
text summary prints the count, up to ten event ids with their revision and
variant count (an `... and N more` line stands in for the rest), and the
`db rederive` remedy; `--json` carries every group in an additive
`journalConflicts` array of `{eventId, revision, contentHashes, selectedHash}`.

**Exit code stays `0`** — the index is complete and usable. `doctor` reports the
same groups at WARN under `journal.conflicts`, and
`cctally db rederive --family claude-usage` resolves them by superseding each
group at the next revision.

`journalConflicts` is deliberately a **different key** from the `conflicts` key
documented for `db rederive --json` below, which means command-validation
failures. The two are never interchangeable.

### Tainted structural correction batches (#402 Task A)

The selector recognizes seven structural classes: marker conflict, commit
without begin, begin/commit manifest mismatch, record-order violation, manifest
action-sequence mismatch, manifest actions-hash mismatch, and duplicate
action-sequence conflict. The affected correction batch is tainted as a whole;
none of its markers or actions enter the rebuilt index. Valid batches before or
after it still participate normally, so the highest valid revision wins.

The rebuild exits `0` with a usable index and names each omitted batch/kind in
text. `--json` carries unacknowledged omissions in the complete deterministic
`journalProtocolViolations` array. Acknowledged omissions remain visible in
`journalAcknowledgedProtocolViolations`, augmented with `auditId`,
`journalHighWater`, and `journalPrefixHash`. Acknowledgement never makes a
tainted batch effective. Invalid record field shapes still fail the selector
and the rebuild.

### Exit codes

`0` rebuilt — including when event groups were quarantined or structural
batches were tainted and omitted; `2` the #146 prod guard refused a
dev-checkout rebuild of the real prod stats.db; `3` the rebuild itself failed.

## `cctally db journal-repair [--violation <fingerprint> ...] [--yes]`

Previews and records an operator decision to keep exact structurally invalid
correction batches quarantined. It is not generic SQLite recovery and does not
rederive or choose a correction action.

Preview is the default:

```
cctally db journal-repair
cctally db journal-repair --json
```

It reports the pinned journal high-water and prefix hash plus deterministic
unacknowledged and acknowledged violation arrays. Preview is strictly
read-only: it does not create a DB, SQLite sidecar, lock file, config/update
state, cursor/HWM file, journal line, or alert.

Mutation requires both an explicit fingerprint selection and `--yes`:

```
cctally db journal-repair \
  --violation sha256:<exact-fingerprint> \
  --yes
```

`--violation` is repeatable. A bare `--yes`, a duplicate or unknown
fingerprint, or a selection whose journal prefix changes before the repair
locks are acquired is rejected before append. Re-run the preview and select
the current exact fingerprints.

Apply takes the stats maintenance lock and journal ingest lock in the
repository order, revalidates the initial preview, and appends one
`journal_protocol_resolution` audit record. The audit names each exact
`{batch_id, kind, fingerprint}` and binds the decision to the reviewed raw
journal prefix. Existing segment bytes are never edited; they remain an exact
prefix followed by the single audit line. The command then rebuilds only
through that audit line via the common scratch build, validation, handle-drain,
and atomic cutover path.

The invalid batch remains wholly tainted after acknowledgement. A later
divergent marker/action or reused batch id produces a new fingerprint and
returns to the unacknowledged list. Repeating the same exact invocation appends
nothing (`already-resolved`). If the process dies after audit append or during
scratch rebuild, the next identical invocation reports `recovered` after
publishing one usable index from the one durable audit. A live stats reader
causes a safe refusal; stop the dashboard or other holder, then rerun the
identical command. If any post-audit rebuild stage fails, exit remains `3` and
JSON still reports the durable acknowledgement and audit id rather than the
stale pre-append state.

`--json` emits a `schemaVersion: 1` envelope with
`status`, `journalHighWater`, `journalPrefixHash`,
`unacknowledgedViolations`, `acknowledgedViolations`,
`selectedViolations`, `auditId`, `rebuild`, and `errors`. Successful apply also
includes the reviewed prefix fields. Keys evolve additively.

### Exit codes

`0` preview, applied, recovered, or already resolved; `2` selection,
locked-prefix revalidation, malformed-protocol, or prod-guard refusal; `3`
lock, append, live-handle, or rebuild failure.

## `cctally db rederive --family claude-usage [--yes]`

Re-runs the closed Claude-usage derivation family over retained raw Claude
observations and operator records using the current code, then compares that
result with the journal's effective decisions. It corrects derivation bugs; it
does not edit hand-entered account labels or other operator truth.

The command is **preview-only by default**. Preview fixes an append-only journal
prefix, copies a stable `cache.db`/WAL prefix to disposable scratch, opens that
copy in a read-only SQLite transaction, and reports the deterministic plan
without creating source lock/WAL files, appending journal records,
replacing `stats.db`, writing config/HWM files, refreshing a provider, or
dispatching alerts. Add `--yes` to append one manifest-checked correction batch
and atomically rebuild the disposable stats index.

```
cctally db rederive --family claude-usage
cctally db rederive --family claude-usage --json
cctally db rederive --family claude-usage --yes
```

The apply path:

1. Locks stats maintenance, cache maintenance, journal ingest, and the cache
   writer in the established total order.
2. Plans against one journal high-water and one stable read-only cache view.
3. Revalidates that high-water while holding the journal leaf lock, then
   appends the whole ordered batch without interleaving.
4. Rebuilds only through the batch commit high-water. A raw observation
   appended afterward remains unread for the next normal ingest cycle.

Original journal lines are never rewritten or deleted. A crash before the
commit leaves an ineffective incomplete batch; retry appends the same
deterministic batch to completion. A crash after commit but before/during the
stats swap leaves the correction durable; rerun the same command with `--yes`
to recover it without appending a divergent second batch. A successful rerun is
a clean no-op. Rebuild never dispatches historical alerts.

`stats.db` is disposable, so no automatic backup is required. If you want a
point-in-time copy for manual comparison, run `cctally db backup --db stats`
before apply. Back up `cache.db` separately if the retained cost source itself
needs archival; `db rederive` reads but never mutates it.

The initial `claude-usage` family covers accepted weekly usage, weekly cost,
weekly and five-hour reset/credit decisions, closed five-hour blocks, and their
dependent milestones. Historical budget/projected configuration is not
retained, so those stale Claude latches are retired and re-materialize from
current config on later live activity. Codex quota and Codex budget/projection
state remain outside this family. Missing cache tables/columns, account-specific
cost inputs, or a known cache-write TTL split fail before mutation.

**Pre-cutover history is preserved, never re-derived (#426).** Observations are
only journaled from the cutover onwards, so the rows the cutover exported as
`b:<table>:<rowid>` lines are themselves the only durable truth for everything
older — no replay can reproduce them, because the family's own derivation only
ever mints natural keys (`sa:`, `wcs:`, `pm:`, …). Those events are held out of
the diff entirely: never tombstoned, never rewritten from a re-derivation that
does not cover them. The same protection covers every owned event when no Claude
observation is retained at all, since a diff against an empty desired set can
only be destructive. `preservedEventCount` in the JSON reports how many events a
plan protected. Everything the retained observations do cover still diffs
normally, so an obsolete derivation still retires.

If an earlier `claude-usage` batch already retired that history — a
`db rederive --yes` before this fix dropped every pre-cutover weekly usage and
cost snapshot, collapsing a 12-week `$/1%` trend to the weeks since the cutover
— the next plan **revives** it at `rev + 1` from the journal's own retained
lines. Run `cctally db rederive --family claude-usage` and apply it with `--yes`
to restore the history. A tombstone written by anything other than this family's
own re-derivation is a deliberate retirement and is left alone.

| Flag | Description |
| --- | --- |
| `--family claude-usage` | **Required.** The only supported family in this release. |
| `--yes` | Apply the previewed plan, or finish recovery of a completed batch. Without it, the command is read-only. |
| `--json` | Emit a stamped `schemaVersion: 1` object. |

JSON always includes `status`, `family`, `journalHighWater`, `batchId`,
`planHash`, `actionCounts`, `preservedEventCount`, `conflicts`,
`journalConflicts`, `dataGaps`, `errors`, `rebuild`, and `noOp`. If a readable journal exists, input and
retained-source errors preserve the already-captured `journalHighWater`.
`status` is `preview`, `applied`, `recovered`, `no-op`, `conflict`,
`missing-source`, or `failed`. New optional keys may be added without a schema
version bump.

`conflicts` is a list of **command-validation failure messages** (unsupported
family, the prod guard, a structural journal protocol error).
`journalConflicts` (#374) is a different thing entirely: the quarantined
same-revision groups this plan will resolve, each
`{eventId, revision, contentHashes, selectedHash}`. A conflicted group is
superseded at the next revision **even when the provisional winner already
matches the desired re-derivation** — otherwise the plan would report `retain`
and the group would survive in the append-only journal forever.

The dev/worktree-to-production guard applies to `--yes`: use the installed
binary for the real `~/.local/share/cctally` data, or explicitly set
`CCTALLY_ALLOW_PROD_MIGRATION=1`. Preview remains safe from a checkout because
it does not mutate the target data.

### Exit codes

`0` preview, apply, recovery, or no-op; `2` unsupported input, a journal
protocol conflict, missing retained source data, or the production guard; `3`
lock contention, cache/SQLite I/O, append, or rebuild failure.

## `cctally db recover --db cache [--yes]`

Reverts a **version-ahead** `cache.db` to this binary's known schema
head (issue #145). A cache.db whose `PRAGMA user_version` exceeds the
running binary's registry head was last touched by a newer/unreleased
cctally (e.g. a `main`/dev checkout that carries an unreleased migration
was run against the shared prod data dir). Without recovery every
cache-opening command errors with `DowngradeDetected` and bricks.

> **`--db stats` is RETIRED** (DB journal redesign §7.1). `stats.db` is
> now a disposable index stamped at a single `STATS_INDEX_EPOCH`, not a
> versioned migration target, so a version mismatch self-heals by
> **rebuild** rather than trim-and-revert. `cctally db recover --db
> stats` exits 2 with a pointer to `cctally db rebuild --db stats`.

`recover` trims the unknown (ahead) markers from both
`schema_migrations` and `schema_migrations_skipped`, then reconciles
`user_version` to the known head (or to `0` when a known marker is
missing, so the next open re-runs the still-pending known migrations
idempotently). Any extra tables/columns the unknown migration created
are left inert. It bypasses `open_cache_db()` (raw `sqlite3.connect`) so
it never re-triggers the dispatcher, and is a no-op when the DB is not
ahead.

| Flag | Description |
| --- | --- |
| `--db {cache,stats}` | **Required.** `cache` recovers; `stats` is retired (exits 2 → `db rebuild`). |
| `--yes` | Accepted but not required for `--db cache` (re-derivable). |

- **`--db cache`** heals **without** `--yes` — cache.db is fully
  re-derivable (`cctally cache-sync --rebuild` rebuilds it). In normal
  operation a version-ahead cache.db **auto-heals** on the next
  cache-opening command (the dispatcher opts cache.db into in-place
  recovery); `db recover --db cache` is the explicit, on-demand path.

### Exit codes

`0` heal or no-op (not ahead / file absent); `2` `--db stats` (retired —
use `db rebuild`).

## `cctally db repair --db stats --yes`

Recovers a physically malformed `stats.db` through SQLite's
corruption-tolerant `.recover` operation (issue #314). This is distinct from
`db recover`, which only reconciles a database whose schema version is ahead of
the running binary.

Stop the dashboard and other cctally processes first. The command refuses when
another writer holds the database. It also requires `--yes`, honors the existing
dev-checkout-to-production guard, and requires a recovery-capable `sqlite3`
command-line shell. cctally probes `.recover` before copying or changing any
database bytes; a distro build without `SQLITE_ENABLE_DBPAGE_VTAB` is rejected
with an installation hint for the official sqlite.org CLI. There is deliberately
no `--force` race bypass for the non-re-derivable stats database.

The repair sequence is fail-safe:

1. Create a crash-recoverable repair marker that blocks new cctally stats
   opens, then refuse unless all earlier main/WAL/SHM handles are closed.
2. Prove the database is malformed, acquire SQLite's writer lock, and preserve
   exact `stats.db`, `stats.db-wal`, and `stats.db-shm` bytes under a
   timestamped `stats.db.bak-corrupt-malformed-*` family before replacing
   anything.
3. Checkpoint all committed WAL frames into the old main file, acquire one
   write exclusion that remains held through recovery and replacement, and
   recover a private same-filesystem main-file copy.
4. Restore SQLite's WAL-aware effective `PRAGMA user_version`, run full
   `PRAGMA integrity_check`, and verify `weekly_usage_snapshots` remains
   readable and row-count equal. If the
   source count cannot be read, refuse the automated swap rather than claim
   preservation without proof. Report other table-count losses or unreadable
   source tables explicitly.
5. Atomically replace the live main file while the continuous writer guard is
   still held, then close the old handle and remove only the now-empty stale
   WAL/SHM sidecars. The recovered file is mode `0600`.

A failure before replacement leaves the live logical contents in place (a WAL
checkpoint may have changed their physical representation) and keeps the exact
pre-checkpoint corrupt family for manual analysis. Replacement failure leaves
the coherent old main file and empty sidecars in place. A healthy database is
refused before a backup or replacement is created. `cache.db` is fully
re-derivable and is not a repair target; use `cctally cache-sync --rebuild`
instead.

### Exit codes

`0` repaired (or stats.db absent); `2` missing `--yes`, healthy-database
refusal, or dev-to-production guard refusal; `3` database still active,
`sqlite3` unavailable or missing `.recover` support, recovery/import failure,
or verification failure.

## `cctally db backup --db {cache,stats} [--output <path>]`

Creates a consistent, standalone SQLite backup. Without `--output`, the
destination is a timestamped sibling (`stats.db.bak-*` or `cache.db.bak-*`). An
existing destination is never overwritten.

This command uses SQLite's online backup API. It captures committed WAL content
into one verified database file while normal readers and writers may continue;
the result needs no `-wal` or `-shm` sidecar. This is the supported backup path.

**Never `cp`, restore, move, or replace a live `stats.db` or its sidecars while
cctally is running.** Copying `stats.db` plus whatever `-wal`/`-shm` files happen
to exist is not an atomic SQLite snapshot and can create the corruption this
command is designed to prevent. Stop cctally before restoring a backup.

### Exit codes

`0` verified backup created (or source absent); `2` destination exists or its
parent is absent; `3` SQLite backup/integrity or filesystem failure. If stats.db
is already malformed, the error points to `cctally db repair --db stats --yes`.

## `cctally db checkpoint [--db {cache,stats}] [--json]`

Fast, non-destructive WAL drain (issue #297). Runs a single `PRAGMA
wal_checkpoint(TRUNCATE)` to flush the write-ahead-log frames into the
main DB and shrink the `-wal` file back to zero. It does **not** do a
full ingest walk (the distinction from `cache-sync`, and why it still
works when the syncs themselves are what's wedged), changes no data, no
schema, and no `user_version`.

The recurring symptom this fixes: during a heavy multi-agent session the
`cache.db-wal` file ratchets up to multi-GB and never shrinks, making
every write crawl past the busy timeout so `cctally` commands fail with
`Error: database is locked`. In normal operation the WAL cap
(`PRAGMA journal_size_limit`) plus a forced end-of-sync checkpoint keep
the WAL contained; this command is the manual escape hatch and the
`doctor` `cache.db WAL size` remediation for a pathological case.

It opens the target via a **raw existing-file-only** connection
(`sqlite3.connect("file:<path>?mode=rw", uri=True)`, guarded by an
`exists()` check) — explicitly **not** `open_cache_db()` / `open_db()`,
which apply schema, run the migration dispatcher, can delete Codex rows,
and would create a missing DB. For `cache.db`, it first takes the same
shared side of `cache.db.maintenance.lock` and then the same global writer
flock as Claude/Codex sync, using the 15 s timeout as one bounded lock
wait. A manual checkpoint therefore cannot start during recovery's
drain/quarantine handshake or overlap a cache write/end-of-sync
checkpoint. SQLite's own locking still excludes readers. The command is
**best-effort** — if either layer stays busy it reports `busy` rather than
hanging. There is **no prod guard and no
`--yes`** — a checkpoint is safe from any instance (a dev checkout drains
the dev data dir; the installed binary drains prod).

| Flag | Description |
| --- | --- |
| `--db {cache,stats}` | Which DB to drain. Default **`cache`** (the DB that bloats, and the re-derivable one). No `--db all`. |
| `--json` | Emit a `schemaVersion: 1` envelope instead of text. |

- **`truncated`** = the checkpoint reset the WAL (`busy=0`) **and** the
  `-wal` file is now zero-length/absent. A checkpoint can copy some
  frames yet still report `busy=1` (partial) — that is **not**
  `truncated`.
- **Missing target DB** → exit `0` with `no <db> database file present;
  nothing to drain` (a missing re-derivable cache is not an error). The
  raw connect never creates the file.

### `--json` fields

`schemaVersion` (always first), `db`, `walBytesBefore`, `walBytesAfter`,
`framesCheckpointed`, `busy`, `truncated`, `present`.

### Exit codes

`0` drained, already-small, or DB absent; `3` (staged) the target stayed
`busy` / the WAL was not fully truncated through the timeout — an
actionable "something is still holding it" signal.

## `cctally db vacuum [--db {cache,conversations,stats,all}]`

Reclaim disk space by rewriting the database file compactly (SQLite `VACUUM`). Use `--db conversations` for space freed by transcript retention; `--db all` includes `cache.db`, `conversations.db`, and `stats.db`.
Deleting rows — for example the transcript retention prune (`cache-sync
--prune-conversations`, or the dashboard's automatic once-a-day pass) — frees
pages *inside* the file but never shrinks it on disk; `db vacuum` is what
actually returns that space to the filesystem. `--db` selects `cache` (default),
`conversations`, `stats`, or `all`.

This is **never automatic** and always explicit. VACUUM needs exclusive access:
the command drains the WAL and rewrites the file under a real SQLite
`PRAGMA locking_mode=EXCLUSIVE`, so a running dashboard (or any other cctally
process reading the DB) makes it **fail promptly** rather than hang or race —
stop the dashboard and retry. Because VACUUM writes a full temporary copy of the
database, the command also refuses up front when free disk is below roughly twice
the file size plus its WAL. On success it reports the space reclaimed.

### Exit codes

`0` reclaimed (or the DB is absent — nothing to do); `3` (staged) the target is
in use (stop the dashboard / other cctally processes and retry), a maintenance
operation is already running, or free disk is below the required margin.

## Notes

- **Failure recovery.** A failed migration writes a block to
  `~/.local/share/cctally/logs/migration-errors.log` and renders a
  one-line banner on the next interactive command. Read the log; fix
  the root cause; the next `open_db()` retries automatically. If the
  failure is environment-specific (e.g., FK collision unique to your
  data), `cctally db skip` is the escape hatch.
- **No `down()`.** This framework does not support rollback / down
  migrations. Per-migration transactional safety inside `BEGIN`/`COMMIT`
  handles partial-failure rollback; full reversibility is not a goal.
- **Banner suppression.** All `db` actions self-suppress the migration-error banner
  (the whole `db` namespace shows failure state in its own output or is
  mid-fix). Other interactive commands continue to render the banner when
  failures are pending.
- **`db status` is read-only and uses raw `sqlite3.connect()`.** It
  does NOT go through `open_db()` / `open_cache_db()`, and therefore
  does NOT trigger the migration dispatcher on this invocation.
  Rationale: a poison-pill failed migration shouldn't re-fail every
  time you try to inspect state. Trade-off: if a fresh dispatcher run
  WOULD have advanced state on this open, `db status` won't observe
  that — re-run any other cctally subcommand first to drive the
  dispatcher, then re-run `db status`.
- **`db skip` on a virgin install converts subsequent `open_db()` from
  fresh-install to upgrade-user state.** The skip command creates
  `schema_migrations` / `schema_migrations_skipped` (and the marker
  rows it needs) before any `open_db()` has run, so the dispatcher's
  fresh-install detection — which checks whether `schema_migrations`
  existed before its own `CREATE TABLE IF NOT EXISTS` — returns False
  on the next open. Concrete impact: handlers run their bodies
  instead of being stamped via the fresh-install fast-path. The
  framework's existing handlers are empty-table fast-paths or no-ops
  on empty data, so behavior is preserved; this note exists so
  future-Claude doesn't get confused when a migration body executes
  on a brand-new machine after a `db skip`.

## See also

- `docs/superpowers/specs/2026-05-06-migration-framework-design.md` —
  the full design.
- `bin/cctally-migrations-test` — harness covering 9 framework
  mechanics scenarios + per-migration goldens loop.
