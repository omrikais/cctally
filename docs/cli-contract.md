# cctally CLI contract — exit codes & JSON envelope

This page documents two cross-cutting conventions that every `cctally` subcommand is expected to follow: the exit-code taxonomy and the `--json` output envelope. It is the reference for anyone scripting against cctally or adding a new subcommand. The conventions were reconciled and, where cheap, aligned in issue #279 Session 6; the deliberate exceptions are called out explicitly so they read as decisions rather than drift.

## Exit codes

cctally exit codes fall into three groups.

**`0` — success.** Every command returns `0` when it does what you asked. Read-only diagnostics that merely report state (`db status`, a healthy `doctor`) also return `0`.

**`1` — bad input in the ccusage-parity family.** The commands that are drop-in replacements for `ccusage` / `ccusage-codex` deliberately keep `ccusage`'s exit `1` on a bad `--since`/`--until` date or bad flag combination, so a script written against upstream keeps working byte-for-byte: `daily`, `monthly`, `weekly`, `session`, the Codex `codex-daily`/`codex-monthly`/`codex-weekly`/`codex-session` reports, and `blocks`. This is the same class of decision as the Codex dedup divergences — parity is a feature, not an oversight. Four cctally-native commands also still exit `1` on their own bad-date input as **documented legacy exceptions**, not because it is the intended convention: `cache-report` and `range-cost` route bad dates through the same exit-`1` path, and `sync-week` and `percent-breakdown` reach exit `1` via the generic uncaught-`ValueError` → `Error:` handler. The first two are cheap to convert but the latter two ride a broader uncaught-exception path whose conversion is a larger change; aligning only half would create a new inconsistency, so all four are recorded here as legacy exit-`1` surfaces and left for a follow-up rather than partially migrated. Separately, `pricing-check` uses `1` to mean **"an actionable finding exists"** — a business-logic result, not bad input — which is orthogonal to this taxonomy and documented on its own page.

**`2` — usage/validation error in a cctally-native command.** The cctally-only, spec-designed commands exit `2` on their own usage or validation errors, matching Python's own argparse convention (argparse's `parser.error()` calls `sys.exit(2)`, and an unknown subcommand or bad flag is `2`). Members: `diff`, `budget`, `five-hour-blocks`, `five-hour-breakdown`, `pricing-check` (usage errors — distinct from its finding-code `1` above), `doctor`, `telemetry`, `config`, `record-usage`, and — **as of #279 Session 6** — `project` and `forecast`, which were previously misfiled at `1`. The shared share-flag validator (`_share_validate_args`, wired into the nine share-enabled commands) also exits `2` on a bad `--format`/`--output`/`--copy`/`--open` combination, regardless of which family the host command belongs to. This means a parity-family command such as `daily` already mixes both codes by error class: `1` for a bad date, `2` for a bad share flag.

**`3` and up — command-specific staged severity.** A handful of commands encode *which stage* failed in the exit code, and these numbers must not be flattened into the taxonomy above — reusing `2` for a generic "usage error" in one of them would collide with an already-meaningful code. These are, per command:

| Command | Codes |
| --- | --- |
| `setup` | `1` hard prerequisite failure · `2` partial (symlinks ok, `settings.json` write failed) · `3` user declined confirmation |
| `record-credit` | `2` validation/refusal · `3` database error |
| `refresh-usage` | `2` no OAuth token · `3` network failure · `4` malformed response · `5` internal `record-usage` failure |
| `alerts test` | `1` notifier binary missing · `2` `--threshold` out of range · `3` other spawn error |
| `db skip` / `db unskip` | `1` unknown name / already-applied · `2` ambiguous bare name |
| `db recover` | `2` `--db stats` without `--yes`, or prod-guard refusal |
| `db checkpoint` | `3` the target DB stayed busy / the WAL was not fully truncated through the timeout (`0` = drained, already-small, or DB absent) |
| `tui` | `1` fatal (missing `rich`, narrow terminal, renderer crash) · `2` argument error |
| `statusline` | `1` unparseable/non-object stdin JSON · `2` argparse / `--config` error |
| share surface | `2` flag-combo error · `3` output-filename collision exhaustion |

## JSON envelope (`--json`)

**camelCase `schemaVersion` for every current and future adoption.** Reporting `--json` payloads carry a top-level `schemaVersion` integer, rendered **first** (insertion order controls `json.dumps` output order, so the key is always at the top of the payload). The single home for stamping it is `bin/_lib_json_envelope.py::stamp_schema_version`, which returns a shallow copy with the key inserted first and never mutates its input. As of #279 Session 6 the following surfaces carry `schemaVersion: 1`: `daily` / `daily --instances` / `monthly` / `weekly` / `session`, the Codex `codex-daily`/`codex-monthly`/`codex-weekly`/`codex-session` reports, `blocks`, `forecast`, `report` (a.k.a. `dollar-per-percent`), `project`, `range-cost`, `cache-report`, `percent-breakdown`, `sync-week`, `telemetry`, and the `budget set`/`unset` actions — plus the surfaces that already carried it (`five-hour-blocks`, `five-hour-breakdown`, `budget` status, `record-credit`, `pricing-check`). The envelope holds on **empty and error** payloads too, not just the happy path, so a consumer that keys on `schemaVersion` never loses it exactly when the result is empty.

**Frozen legacy spellings.** Some surfaces shipped a different key before the convention was settled. These are frozen as-is — no dual-emit, no deprecation, no renaming — and consumers must accept them: `diff`, `doctor`, `db status`, `refresh-usage`, and `setup` emit snake_case `schema_version`; `update --check --json` emits `_schema`. New adoptions always use camelCase `schemaVersion`; the legacy spellings are grandfathered, not a precedent.

**Additive evolution.** Adding an optional key to a payload never bumps `schemaVersion` — consumers **MUST tolerate unknown keys** (the same contract `diff` already states repo-wide). Only a breaking shape change (removing or renaming a key, changing a value's type or meaning) bumps the version. This is why the `schemaVersion` addition itself is a non-breaking change: deleting the key from any stamped payload reproduces the pre-#279 bytes exactly.

**Deliberate parity departures & non-envelope surfaces.** The Codex empty-result sentinel (`codex-{daily,monthly,session}` with no data) is byte-exact `ccusage-codex` parity and uses compact `separators=(",", ":")`; it is stamped anyway, so its stamped-empty form is a documented departure from upstream (the compact formatting is preserved, only the leading `schemaVersion` is added). Two `--json`-ish surfaces are intentionally **not** envelopes and are not adoption targets: `range-cost --total-only --json` prints a bare number (the `--total-only` precedence wins before `args.json`), and `update --dry-run --json` emits an **NDJSON stream** (one JSON object per step, not a single document).

**Known-unversioned remainder.** `config get` / `config set --json` are the one reporting-adjacent `--json` surface left unversioned. Config's JSON is a CRUD echo of the written/read value, not a reporting payload, and it has no shared chokepoint — the key is emitted ad-hoc at ~14 per-key sites, making it the single most expensive retrofit for the least benefit (it fails the "all *reporting* `--json` surfaces" scope). `config unset` has no `--json` flag at all. Adopt `schemaVersion` here opportunistically only if config's JSON ever changes shape.

## Adding a subcommand

Registration in `bin/_cctally_parser.py` is table-driven: `build_parser()` iterates an ordered `_REGISTRATION` table, so adding a command is a two-part change in one file — write a `_build_<cmd>_parser(subparsers, name, *, help_text, xref=None)` builder (resolving `c = _cctally()` **inside** the function, never at import time) and add its row to the table at the position where you want it to appear in `--help` (table order is registration order is help order). Dual-registered commands (the `claude`/`codex` subgroups) get two rows with their verbatim `xref` strings. A hidden command sets `help=argparse.SUPPRESS` on its `add_parser` call inside the builder; the `__preview` maintainer command additionally carries a call-time `predicate` so the public-mirror parser shape omits it. If the new command has a `--json` surface, stamp it via `stamp_schema_version` per the envelope convention above; if it has bad-input paths, pick its exit code per the taxonomy above (a new cctally-native command uses `2`).
