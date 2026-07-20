# `cctally db`

Migration / DB-management subcommand. Eight actions: `status`, `skip`,
`unskip`, `recover`, `repair`, `backup`, `checkpoint`, and `vacuum`.

## Synopsis

```
cctally db status [--json]
cctally db skip <migration-name> [--reason "<text>"]
cctally db unskip <migration-name>
cctally db recover --db {cache,stats} [--yes]
cctally db repair --db stats --yes
cctally db backup --db {cache,stats} [--output <path>]
cctally db checkpoint [--db {cache,stats}] [--json]
cctally db vacuum [--db {cache,stats,all}]
```

## Description

`cctally` runs schema migrations on every `open_db()` (stats.db) and
`open_cache_db()` (cache.db) invocation via a small in-process framework.
Migrations are numbered (`001_…`, `002_…`), per-DB, registered via
`@stats_migration` / `@cache_migration` decorators in `bin/cctally`. The
`db` subcommand surfaces this state and offers a manual poison-pill
escape.

Spec: `docs/superpowers/specs/2026-05-06-migration-framework-design.md`.

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

## `cctally db recover --db {cache,stats} [--yes]`

Reverts a **version-ahead** DB to this binary's known schema head
(issue #145). A DB whose `PRAGMA user_version` exceeds the running
binary's registry head was last touched by a newer/unreleased cctally
(e.g. a `main`/dev checkout that carries an unreleased migration was
run against the shared prod data dir). Without recovery every
DB-opening command errors with `DowngradeDetected` and bricks.

`recover` trims the unknown (ahead) markers from both
`schema_migrations` and `schema_migrations_skipped`, then reconciles
`user_version` to the known head (or to `0` when a known marker is
missing, so the next open re-runs the still-pending known migrations
idempotently). Any extra tables/columns the unknown migration created
are left inert. It bypasses `open_db()` / `open_cache_db()` (raw
`sqlite3.connect`) so it never re-triggers the dispatcher, and is a
no-op when the DB is not ahead.

| Flag | Description |
| --- | --- |
| `--db {cache,stats}` | **Required.** Which DB to recover. |
| `--yes` | **Required for `--db stats`.** stats.db holds non-re-derivable snapshots/milestones; the revert may leave orphan schema behind and need a re-record/re-sync, so it refuses without explicit consent. |

- **`--db cache`** heals **without** `--yes` — cache.db is fully
  re-derivable (`cctally cache-sync --rebuild` rebuilds it). In normal
  operation a version-ahead cache.db **auto-heals** on the next
  cache-opening command (the dispatcher opts cache.db into in-place
  recovery); `db recover --db cache` is the explicit, on-demand path.
- **`--db stats`** without `--yes` prints the hazard and refuses
  (exit 2); with `--yes` it trims the markers and reverts
  `user_version`.
- **Prod guard (issue #146).** `--db stats` also refuses (exit 2,
  DB untouched) when a **dev/worktree checkout** binary is pointed at
  the real prod data dir (`~/.local/share/cctally`) — trimming markers
  on the installed release's non-re-derivable stats.db could corrupt
  it. Run the installed binary instead, or override with
  `CCTALLY_ALLOW_PROD_MIGRATION=1`. This mirrors the #142 migration
  guard; `--db cache` is exempt (re-derivable).

### Exit codes

`0` heal or no-op (not ahead / file absent); `2` `--db stats` invoked
without `--yes` while the DB is ahead, **or** the #146 prod guard
refused a dev-checkout recovery of the real prod stats.db.

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
and would create a missing DB. It relies on SQLite's own file locking
plus a 15 s `busy_timeout`; it is **best-effort** — if a reader/writer
holds the target off past the timeout it reports `busy` rather than
hanging. There is **no prod guard and no `--yes`** — a checkpoint is safe
from any instance (a dev checkout drains the dev data dir; the installed
binary drains prod).

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

## `cctally db vacuum [--db {cache,stats,all}]`

Reclaim disk space by rewriting the database file compactly (SQLite `VACUUM`).
Deleting rows — for example the transcript retention prune (`cache-sync
--prune-conversations`, or the dashboard's automatic once-a-day pass) — frees
pages *inside* the file but never shrinks it on disk; `db vacuum` is what
actually returns that space to the filesystem. `--db` selects `cache` (default),
`stats`, or `all`.

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
