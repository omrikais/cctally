# `cctally setup`

Install cctally into each available local provider. It symlinks user-facing
binaries into `~/.local/bin/`, adds Claude hook entries to
`~/.claude/settings.json` when Claude Code is initialized, and independently
manages native Codex handlers in every configured Codex home. A Codex-only
machine does not need a `~/.claude/` directory.

## Modes

| Mode | What it does |
|---|---|
| `cctally setup` | Install (default) |
| `cctally setup --dry-run` | Show planned changes; modify nothing |
| `cctally setup --status` | Report current install state |
| `cctally setup --uninstall` | Remove hooks + symlinks; keep history |
| `cctally setup --uninstall --purge` | Also wipe `~/.local/share/cctally/` |

## Common flags

- `--yes` / `-y` — skip confirmations
- `--json` — emit machine-readable output
- `--force-dev` — allow setup to run from a git checkout (see Dev-checkout refusal)
- `--migrate-legacy-hooks` — auto-accept the legacy-hook migration prompt (install-mode only)
- `--no-migrate-legacy-hooks` — auto-decline the legacy-hook migration prompt (install-mode only)

## Dev-checkout refusal

When run from a git checkout (a `.git` entry at the repo root), `cctally
setup` — both install and `--uninstall` — refuses with exit code `2` and
prints a message explaining why, without touching `~/.claude/settings.json`.
The hooks in `settings.json` point at your installed (npm/brew) cctally;
rewriting them from a dev checkout would repoint them at the dev binary (or
remove them), breaking the installed instance. Run setup from the installed
binary instead, or pass `--force-dev` to override when you intend to install
dev-pointing hooks. The refusal is independent of `CCTALLY_DATA_DIR`: setting
that env var relocates the dev data dir but still does not let setup rewrite
the prod hooks.

## Upgrade self-heal (new symlinks)

When you upgrade via `npm install -g cctally@<newer>` and that release ships a
new `cctally-<subcmd>` binary, its `~/.local/bin/` symlink would normally be
missing until you re-ran `cctally setup` (`doctor` would warn about the missing
link in the meantime). The npm postinstall now best-effort runs an internal
`repair-symlinks` pass to close that gap: it additively creates `~/.local/bin/`
symlinks for any newly added subcommands, so they become reachable immediately
after the upgrade with no manual step.

The pass is strictly additive and gated to existing installs:

- It only fills genuinely-empty `~/.local/bin/` slots; present, wrong-target,
  dangling, and hand-rolled non-symlink slots are left untouched.
- It is gated to *existing* installs — it acts only when at least one cctally
  symlink is already present, so a fresh install stays hands-off and still
  prints the "run `cctally setup`" hint (which is also where the additive hooks
  and SQLite cache get bootstrapped).
- It touches only `~/.local/bin/` symlinks — never hooks, `settings.json`, or
  the cache.
- It can never fail the npm install: if python is missing or the pass errors,
  it is silently skipped and the standard setup hint still prints.

`repair-symlinks` is internal plumbing — hidden from `cctally --help`, invoked
by the postinstall — and refuses to run from a dev checkout. You should not need
to invoke it directly.

## Homebrew installs

On a Homebrew install, the policy is: **brew owns `<prefix>/bin/`; it never
owns `~/.local/bin/`.** The formula already symlinks `cctally` (and every
`cctally-*` subcommand) into `<prefix>/bin/`, which is version-stable and
self-heals on `brew upgrade`. So `cctally setup` on a brew install:

- **Skips creating `~/.local/bin/` symlinks entirely.** Commands reach your
  PATH through the formula's `<prefix>/bin/`, so a second set of links would
  only dangle after `brew cleanup` removes the old keg. `cctally setup` prints
  a line like `✓ Brew install detected — commands are on PATH via <prefix>/bin/;
  skipping ~/.local/bin/ symlinks`, and suppresses the usual "not on your
  PATH" warning.
- **Points Claude Code hooks at the stable `<prefix>/bin/cctally`** — not the
  versioned keg path under `<prefix>/Cellar/cctally/<version>/` — so the hook
  entries in `~/.claude/settings.json` survive `brew cleanup`.
- **Cleans up leftover links.** Legacy `~/.local/bin/` links from a prior
  install pattern are removed when safe: links to an old keg
  (`<prefix>/Cellar/cctally/`) or the npm `cctally` shim are removed when the
  command is still reachable elsewhere or the link is dangling; retired
  command names (e.g. `cctally-release`, which went private) are removed
  unconditionally. A hand-rolled link pointing somewhere unrelated is left
  untouched.

If cctally is reachable *only* through a legacy `~/.local/bin/` link to an old
keg (so removing it would break your only working copy), setup deliberately
leaves the link in place and instead prints a PATH-fix hint: put `<prefix>/bin`
on your PATH (e.g. `eval "$(brew shellenv)"`), then re-run `cctally setup` to
clean the link.

`cctally setup --status` and `cctally setup --dry-run` reflect the skip. In
`--status` the PATH row reads `brew: commands via <prefix>/bin`; in `--dry-run`
the symlink line reads `Brew install — would skip ~/.local/bin/ symlinks
(commands on PATH via <prefix>/bin/)`. The `--json` envelopes gain brew-specific
keys under `symlinks`:

| Mode | `symlinks` keys (brew only) |
|---|---|
| `cctally setup --json` (install) | `skipped: true`, `reason: "brew"`, `stale_removed: [<names cleaned up>]` (alongside the usual `created`/`already`/`replaced`/`total: 0`/`destination`) |
| `cctally setup --dry-run --json` | `skipped: true`, `reason: "brew"`, `would_create: 0`, `would_remove_stale: [<names that would be cleaned up>]` (alongside `already: 0`/`blocked: []`/`destination`/`total: 0`) |

## Hook events installed

`PostToolBatch`, `Stop`, `SubagentStop`. Together they cover every
assistant-message boundary at least once. Each entry's `command` is the
absolute path to `cctally` followed by `hook-tick` (bare, no trailing `&`),
quoted via `shlex.quote` so paths with spaces survive `/bin/sh -c`.
`cctally hook-tick` reads its stdin payload synchronously, then forks
itself so CC's hook returns immediately while sync_cache + OAuth refresh
run in the background child.

## Identification of our entries

`setup` recognizes its own entries by the last two shell tokens of the
`command` field: a path whose basename is `cctally` followed by `hook-tick`.
Both bare and absolute paths match; quoted paths (with spaces) match too.

## `statusLine.refreshInterval`

When your `~/.claude/settings.json` already has a `statusLine` block that points at cctally (a `type: "command"` block whose command runs `cctally statusline` — directly, via `cctally claude statusline`, via the self-contained `cctally-statusline`, or through a legacy `~/.claude/statusline-command.sh`-style wrapper that invokes it), setup adds `"refreshInterval": 30` when the block lacks one. This makes Claude Code re-run the status line on a 30-second timer so usage keeps recording while a coordinator session waits on a long subagent (see [statusline.md](statusline.md#keeping-usage-fresh-during-subagent-waits-statuslinerefreshinterval)).

Ownership is **add-when-absent / never-mutate / never-remove**:

- **Add when absent.** The key is inserted only when a recognized cctally `statusLine` block has no `refreshInterval`. Setup never *creates* a `statusLine` block where none exists — your status-line choice is yours; setup only augments a cctally-pointing one.
- **Never mutate.** A `refreshInterval` you already set is left exactly as-is, whatever its value or JSON type (a number, or even a string). Setting your own value is the durable way to change or disable the cadence.
- **Never remove.** `cctally setup --uninstall` removes cctally's hook entries but leaves the `statusLine` block and any `refreshInterval` untouched.

The change rides the same atomic backup + write as the hook install. Install reports what it did (`Added …`, `unchanged (user value: N)`, or `skipped` for a non-cctally statusLine command); `--dry-run` previews `Would add statusLine.refreshInterval: 30` only when it would actually add it; `--status` reports the current state read-only. All three modes carry a `statusline_refresh` object in their `--json` envelope with `{ state, value, action }` — `state ∈ {unavailable, absent, foreign, present, missing}`, `value` the existing `refreshInterval` echoed verbatim (or `null`), and `action ∈ {added, would_add, none}`. `cctally doctor` WARNs when a recognized cctally statusLine command is missing the key (see [doctor.md](doctor.md)).

## Codex quota lifecycle hooks

When Codex homes are available, setup also manages native Codex quota hooks.
It resolves the comma-separated `$CODEX_HOME` value, or the default Codex home
only when that variable is unset or blank. An explicit override with no valid
home does not fall back to the default. Each detected home is handled
independently at `<codex-home>/hooks.json`.

On install, setup additively adds one owned command handler to each `Stop` and
`SubagentStop` event:

```text
<absolute-installed-cctally> hook-tick --foreground --source codex
```

The handler has a 30-second Codex timeout. It runs a bounded, local lifecycle
tick: at most one all-root Codex cache sync, quota reconciliation, due-root
quota alert evaluation, and existing Codex budget alert evaluation. It makes
no network request or provider-live refresh. A per-root 15-second throttle and
non-blocking locks make concurrent or unchanged hook fires successful no-ops;
an all-root sync can still update reporting state for throttled or contended
roots, but only due roots can claim quota alerts in that tick.

`setup` is the installation consent boundary, so this Codex hook integration is
enabled by default. It validates every existing `hooks.json` before changing
any setup-managed file, preserves unrelated keys, matcher groups, and
handlers, and performs the authoritative reread, validation, reconciliation,
backup decision, and atomic replacement while holding that root's writer lock.
The resulting home/file permissions are `0700`/`0600`. Re-running setup
collapses token-recognized owned duplicates or obsolete handler shapes into
exactly one current handler per event; status reports installed only for that
exact canonical shape. `--uninstall` removes every token-recognized
cctally-owned Codex handler and retains unrelated configuration.

### Hook state vocabulary

Codex records its hook trust decisions in `~/.codex/config.toml`, under
`[hooks.state."<hooks-file>:<event>:<group>:<handler>"]`. cctally reads that
table (read-only — it never writes `config.toml`) and classifies each root by
an **ordered** algorithm whose first matching rule wins:

1. `CCTALLY_DISABLE_CODEX_HOOKS` is set → `feature_disabled`.
2. `hooks.json` cannot be read or validated → `malformed`.
3. Either event carries no supported handler → `absent`.
4. Either event carries more than one supported handler → `absent`. A
   duplicate registration is the condition setup exists to reconcile, so it
   is never reported as installed.
5. A handler matches only the legacy two-token form → `absent`. That handler
   runs the Claude leg on a Codex event, so it is manageable but not
   functioning.
6. `config.toml` exists but cannot be read or parsed, or a relevant
   `enabled` value is present and not a boolean →
   `installed_trust_unobservable`.
7. A relevant entry sets `enabled = false` → `installed_disabled`.
8. A relevant entry is missing, carries no string `trusted_hash`, or
   `config.toml` does not exist → `installed_untrusted`.
9. `hooks.json` is newer than `config.toml` → `installed_unverified`. Trust
   was recorded, but the handler changed after the last recorded decision.
10. Otherwise → `installed_enabled`.

An absent `enabled` key reads as enabled: the handlers that fire in practice
carry no `enabled` key at all, so only a boolean `false` disables one.
cctally cannot reproduce Codex's `trusted_hash`, so it never compares a
recorded hash against current content — rule 9's modification-time test is
what covers the residual risk instead. `installed_review_required` has been
retired; it encoded "setup just changed the file", a fact about the running
process rather than about Codex's recorded state.

`cctally doctor` FAILs on `installed_disabled` and `installed_untrusted`,
WARNs on `installed_unverified`, `installed_trust_unobservable`, `absent`,
`malformed` and `feature_disabled`, and passes only on `installed_enabled`.
See [doctor.md](doctor.md).

### Slot preservation and the reconcile refusal

Codex keys its trust record by position, so removing a handler renumbers
every later group and can land a freshly installed handler on a key whose
`enabled = false` was decided about a different handler. Before mutating any
root, setup plans every configured root read-only and refuses the **whole
run** at exit 1 when any of three conditions holds:

- a surviving handler would acquire a different `(event, group, handler)`
  key than the one it holds now;
- a newly occupied key already appears in the state table for no current
  handler;
- the planned content at an existing key differs from the current content
  there and that key's entry does not set `enabled = false`.

The third condition's carve-out is deliberate and asymmetric. Inheriting a
*disable* is fail-safe — the hook stays off and doctor FAILs on it loudly.
Inheriting *trust* would run a command the operator never approved under an
approval given to a different one, so only that direction refuses.

An unreadable or unparseable `config.toml` also refuses a mutating
reconcile, because cctally then lacks the evidence that it is not landing on
a stale key. A missing `config.toml` never blocks: with no recorded decision
there is nothing to strand or inherit. Neither does a plan that changes
nothing — setup computes the plan first and refuses only when it would
actually rewrite `hooks.json`, so an already-correct handler under a
hand-broken `config.toml` still gets its symlinks and its Claude hooks.

Two refusals name a different remedy. When every finding is a recorded key
that no handler occupies, Codex `/hooks` has nothing to review, so the
message names the manual edit instead: remove that `[hooks.state]` entry
from `config.toml` by hand. And when the collision is detected after the
preflight passed — another process rewrote `config.toml` inside the window
between the read-only plan and the locked write — the message says that the
earlier steps of the run already completed, because on that path the
symlinks and `settings.json` are already written. With more than one Codex
root the write loop can also have replaced an earlier root's `hooks.json`
before it reached the racing one, so that message names every file it
rewrote and scopes the unchanged claim to the root it refused on.

The refusal names the hooks path, the event, the old and new slots or the
reused key, and lists any recorded keys pointing at handlers that no longer
exist. Before any mutation it also states that no configuration file was
changed; after one it states instead what the run had already written,
naming each `hooks.json` whose contents it rewrote and the dated backup
holding what that file held before. When the collision lands on the first
root the write loop reaches, there is no such file to name and the refusal
says only that cctally rewrote the contents of no Codex hooks file. The
claim is about rewritten contents specifically: a root whose document
already matched the plan is not listed even though the run still enforced
its file mode.
Those stale keys are reported, never cleaned up — `config.toml` stays
read-only. Under `--json` the refusal emits a `schema_version: 2` envelope
with `result: "err"` and `reason: "codex_hook_slot_collision"` before
exiting 1. That envelope carries `hooks_path`, `config_path`, `findings`,
`stale_state_keys`, the boolean `after_mutation`,
`changed_hooks_paths` — the Codex hooks files this run had already
rewritten, empty on the pre-mutation path — and `changed_hooks_backups`,
mapping each of those paths to its backup. A path is absent from that map
when the run created the document rather than replacing one. A
machine-readable caller reads that distinction off those fields rather than
by substring-matching the English in `error`. `--dry-run` reports the same refusal without creating
the hooks directory or a lock file.

### Wire contract

The snake-case setup JSON is at **`schema_version: 2`**. Version 2 retired
the `installed_review_required` state and changed
`codex_hooks.roots[].changes` from an integer to a nested per-event object;
both are breaking value-shape changes rather than additive key additions.

```text
codex_hooks = {
  roots: [{source_root_key, codex_home, hooks_path, state,
           stop_count, subagent_stop_count, feature_enabled,
           requires_review, remediation, error,
           changes?: {Stop: {added, removed, unchanged},
                      SubagentStop: {added, removed, unchanged}}}],
  installed_count,
  enabled_count,
  all_roots_enabled,
  error_count
}
```

A row carries `changes` only when setup planned that root: the install,
uninstall and `--dry-run` envelopes. `--status` plans nothing, so its rows
omit the key entirely, as do the rows of a root setup skipped because its
document is malformed or the feature switch is set.

`changes` counts managed handlers only. An exact canonical survivor is one
`unchanged`; a normalization is one `removed` plus one `added`; each
discarded duplicate is one further `removed`; a fresh append is one `added`.
`--dry-run` and the applied run report all three axes and agree with each
other for the same input. `installed_count` counts every `installed_*`
state, `enabled_count` counts `installed_enabled` alone, `all_roots_enabled`
is `null` with no roots and otherwise true only when every root is
`installed_enabled`, and `error_count` counts `malformed` rows.

`codex_home` and `hooks_path` are intentionally local setup diagnostics; they
are not share/export data. A malformed document fails before any mutation;
use the stated remediation, correct the JSON, and rerun setup.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Hard prerequisite failure |
| 2 | Partial: symlinks created but settings.json write failed |
| 3 | User declined a confirmation under non-`--yes` mode |

## Migrating from a prior install pattern

If your machine has hooks from an earlier install pattern — hand-installed
Python scripts under `~/.claude/hooks/` (`record-usage-stop.py`,
`usage-poller-start.py`, `usage-poller-stop.py`, `usage-poller.py`) plus
their `Stop` / `SubagentStart` / `SubagentStop` entries in
`~/.claude/settings.json` — `cctally setup` will detect them and offer to
migrate. The migration:

- Unwires the matching entries from `~/.claude/settings.json`.
- Moves the `.py` files to `~/.claude/cctally-legacy-hook-backup-<UTC ts>/`
  (reversible — files are moved, not deleted).
- Best-effort stops any currently-active background daemon spawned by
  those hooks (so you don't have to wait out its multi-hour timer or
  reboot for the new wiring to fully take effect).

By default `cctally setup` prompts on a TTY. Pass `--migrate-legacy-hooks`
to auto-accept (useful for non-interactive setups; also implied by
`--yes`), or `--no-migrate-legacy-hooks` to skip without prompting. Both
flags are install-mode only — they're rejected with exit code 2 if
combined with `--status` or `--uninstall`. Under `--json` or a
non-interactive stdin, the prompt is skipped silently and the migration
runs only when one of the two flags is set explicitly.

`cctally setup --status` reports the current legacy-hook state in both
text (under "Legacy bespoke hooks") and `--json` (under
`legacy.bespoke_hooks`). `cctally setup --dry-run --migrate-legacy-hooks`
previews the migration without touching disk.

## See also

- [`hook-tick`](hook-tick.md) — internal per-fire runtime invoked by hooks
- [`refresh-usage`](refresh-usage.md) — manual OAuth fetch (mostly for debugging)
- [`record-usage`](record-usage.md) — opt-in status-line integration (alternative to hooks)
- [`codex-quota`](codex-quota.md) — native quota reports run from retained
  local rollout data
