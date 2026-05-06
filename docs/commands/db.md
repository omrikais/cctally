# `cctally db`

Migration / DB-management subcommand. Three actions: `status`, `skip`, `unskip`.

## Synopsis

```
cctally db status [--json]
cctally db skip <migration-name> [--reason "<text>"]
cctally db unskip <migration-name>
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
- **Banner suppression.** `db status` / `db skip` / `db unskip`
  self-suppress the migration-error banner (the `db` namespace shows
  failure state in its own output or is mid-fix). Other interactive
  commands continue to render the banner when failures are pending.
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
