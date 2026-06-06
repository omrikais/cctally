# `cctally db`

Migration / DB-management subcommand. Four actions: `status`, `skip`,
`unskip`, `recover`.

## Synopsis

```
cctally db status [--json]
cctally db skip <migration-name> [--reason "<text>"]
cctally db unskip <migration-name>
cctally db recover --db {cache,stats} [--yes]
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
- **Banner suppression.** `db status` / `db skip` / `db unskip` /
  `db recover` self-suppress the migration-error banner (the `db`
  namespace shows failure state in its own output or is mid-fix). Other
  interactive commands continue to render the banner when failures are
  pending.
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
