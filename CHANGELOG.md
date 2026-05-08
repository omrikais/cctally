# Changelog

All notable changes to this project are documented in this file. Format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- `release` Phase 5 now publishes to npm via a GitHub Actions OIDC trusted-publisher workflow in the public repo, with `npm publish --provenance` for supply-chain attestation. The release script no longer invokes `npm publish` locally — it polls `npm view` until the workflow lands the version. Eliminates the prior failure mode where passkey-based npm 2FA would block `npm publish` from a non-interactive subprocess.

## [1.2.0] - 2026-05-08

### Added
- npm distribution channel — `npm install -g cctally` lands the
  Python script and dashboard assets via a thin Node shim. `package.json`
  at the public-repo root, version stamped by Phase 1 alongside CHANGELOG.md.
- Homebrew distribution channel — `brew install omrikais/cctally/cctally`
  via a separate `omrikais/homebrew-cctally` tap. Formula
  `depends_on "python@3.13"` and pins cctally's shebang to that keg.
- `cctally release` Phase 5 (npm publish) and Phase 6 (brew formula bump),
  idempotent and resume-aware. `--skip-npm` / `--skip-brew` flags for
  outage workarounds. Pre-releases publish to npm under `--tag next`;
  brew skips pre-releases.

### Fixed
- `bin/cctally-mirror-public` now classifies each commit's paths under
  the `.mirror-allowlist` that lived in THAT commit's tree, matching the
  commit-msg hook's at-commit-time semantics. Previously the mirror tool
  read HEAD's allowlist and retroactively flagged historical commits
  that added a path before a later commit promoted it to public — even
  when the author followed the documented "add file → then add to
  allowlist" sequencing and the hook accepted both commits. Authors saw
  green at commit time and red at release time. The fix evaluates each
  commit against the allowlist that lived at that commit's tree,
  matching the commit-msg hook.
- `cctally release` Phase 6 done-check is now remote-authoritative
  AND verifies the tap default-branch tip. Done-check runs three
  predicates: the local formula contains `/v<version>.tar.gz`, the
  tap origin carries `refs/tags/v<version>`, and the local clone's
  HEAD SHA equals the remote default-branch SHA. Without the
  branch-tip leg, a half-failed push (tag landed, branch did not)
  could mark Phase 6 done while `brew install` — which reads the
  formula from the default branch, not from the tag — still served
  the prior version. Resume after any push failure detects the
  local-but-not-remote state and re-pushes without re-rendering or
  re-committing.
- `cctally release` Phase 6 push is now atomic: a single
  `git push --atomic origin HEAD refs/tags/v<version>:refs/tags/v<version>`
  replaces the previous separate branch + tag pushes. Both refs land
  or neither, eliminating the half-failed-push asymmetry at the
  source. Lightweight tag (no `-a`/`-m`) still works because the tag
  refspec is explicit.
- `cctally setup` hook commands now route through the same
  channel-aware resolver used for the `cctally` symlink, so npm
  installs get the Node shim path in `~/.claude/settings.json`
  instead of the Python script directly. Without this, npm users who
  set `CCTALLY_PYTHON` (because system `python3` is below 3.13) had
  working interactive `cctally` invocations but every Claude Code
  hook fire bypassed `CCTALLY_PYTHON` via the script's
  `/usr/bin/env python3` shebang and silently failed. brew installs
  are unaffected (formula pins `python@3.13`).
- `cctally setup` no longer symlinks `~/.local/bin/cctally` to the
  Node shim during a source-clone install. Resolver previously selected
  `bin/cctally-npm-shim.js` whenever the file was present; since the
  shim is checked into the source tree, source clones (documented as
  Python-only) on a host without Node would have a broken `cctally`
  on PATH. Selection now requires `repo_root` to live under a
  `node_modules/` directory — the canonical npm install layout.

## [1.1.0] - 2026-05-07

### Added
- `bin/cctally-mirror-public --accept-skip-mismatch` flag — overrides
  the refuse gate when accumulated public-skip diffs significantly
  exceed the current publish commit's diff (long-skip-chain plus
  fix/chore-typed publish subject). Default behavior gains an
  `⚠ ACCUMULATED-DIFF MISMATCH` block surfacing warn-severity findings
  (max-ratio greater than 3× plus non-feat subject) and a hard refuse
  on the chain-greater-than-15 plus max-ratio-greater-than-5× case;
  the flag is the documented escape hatch for refuse situations the
  operator has reviewed and accepted.
- SQLite migration framework for `stats.db` and `cache.db` — per-DB
  registry populated via `@stats_migration` / `@cache_migration`
  decorators with contiguous `NNN_descriptive_name` ordering enforced
  at script load. Dispatcher handles fresh-install detection, bootstrap
  rename of pre-framework markers, per-migration `BEGIN`/`COMMIT`
  ownership, first-failure halts, and `PRAGMA user_version` fast-path.
- `cctally db status` — per-DB list of applied / pending / failed /
  skipped migrations with `--json` output. Glyphs: `✓` applied,
  `✗` failed, `·` pending, `~` skipped.
- `cctally db skip <name> [--reason …]` — manual escape for
  migrations that genuinely cannot succeed on a particular machine
  (e.g., poison pills). Skipped migrations are bypassed by the
  dispatcher; they do not run.
- `cctally db unskip <name>` — removes the skip mark and invalidates
  the `user_version` fast-path so the migration retries on next open.
- Uniform migration error sentinel: `migration-errors.log` shared by
  both DBs (cache.db entries prefixed `cache.db:<name>`); banner
  renders on the next interactive command and auto-clears when the
  same migration succeeds again.
- `bin/_sqlite-diff.py` — stdlib `sqldiff` fallback for goldens
  harnesses; includes `PRAGMA user_version` so framework correctness
  conditions surface in the diff.
- `bin/cctally-migrations-test` — 12 framework-mechanics scenarios
  spanning fresh install, partial-marker upgrade, failure → banner →
  clear cycle, downgrade detection, skip / unskip semantics, both-DB
  end-to-end, legacy-marker recognition by `db status`, post-backfill
  5h-dedup re-run, and skip-honored post-backfill semantics. Includes
  a lazy-adopted per-migration goldens loop under
  `tests/fixtures/migrations/per-migration/<NNN_name>/{pre,post}.sqlite`.
- `cctally setup` — one-command install: symlinks user-facing binaries into
  `~/.local/bin/` and adds additive hook entries (`PostToolBatch`, `Stop`,
  `SubagentStop`) to `~/.claude/settings.json`. Includes `--dry-run`,
  `--status`, `--uninstall`, `--uninstall --purge` modes.
- `cctally hook-tick` — internal per-fire runtime invoked by Claude Code
  hooks. Reads CC hook payload from stdin, runs `sync_cache`, conditionally
  refreshes OAuth usage (default 30s throttle).
- `~/.local/share/cctally/logs/hook-tick.log` — rotating per-fire log
  (1 MB cap, single-generation rotation).
- `~/.local/share/cctally/hook-tick.last-fetch` — OAuth throttle marker
  (sentinel file owned by hook-tick).
- Fixture harnesses: `bin/cctally-setup-test` (13 scenarios) and
  `bin/cctally-hook-tick-test` (7 scenarios), both wired into
  `bin/cctally-test-all`.
- Spec: `docs/superpowers/specs/2026-05-06-migration-framework-design.md`.
  Reference page: `docs/commands/db.md`.

### Changed
- The three pre-framework data-shape migrations
  (`001_five_hour_block_models_backfill_v1`,
  `002_five_hour_block_projects_backfill_v1`,
  `003_merge_5h_block_duplicates_v1`) are now framework-managed.
  Existing DBs auto-rename their legacy unprefixed marker rows on the
  next open via the dispatcher's bootstrap path; both `cctally db
  status` and `cctally db skip` recognize legacy names as applied
  even before the bootstrap has run.
- Column additions still go through the existing
  `add_column_if_missing(conn, table, column, decl)` idempotent
  guard — that sibling pattern is unchanged. The migration framework
  is for data-shape changes (backfill, dedup, rename, FK rewrite)
  only.
- Default integration is now hook-based. The legacy status-line snippet
  (`cctally record-usage` from `~/.claude/statusline-command.sh`) is no
  longer the recommended path but **remains fully supported** as an opt-in
  alternative documented in `docs/commands/record-usage.md`.
- `docs/installation.md` rewritten around `cctally setup`.

### Fixed
- Skip-chain metrics now preserve the chain across clean `--no-ff`
  merges, matching the mirror's auto-skip-clean-merges behavior. A
  `--- public ---` block on a clean merge previously flushed the
  accumulated chain in metrics (while the mirror kept accumulating),
  letting a later `fix:` publish bypass the warn/refuse guard.
- `cctally release --resume` now detects an existing Phase-1 stamp
  even when the resume runs on a different UTC date than the original
  stamp. Previously the done-signal compared `## [version] - <today>`
  against the CHANGELOG's recorded date, so a next-day resume would
  miss the stamp and re-attempt Phase 1 on an empty `[Unreleased]`,
  blocking the documented idempotent-resume contract.
- `cctally release` preflight git probes (branch, clean-tree, fetch,
  ahead/behind, tag-clobber) and Phase-2 tag-existence probe now run
  with `cwd=` anchored to the cctally repo. Invocations from outside
  the checkout (e.g., the `cctally-release` symlink in `~/.local/bin/`
  with the operator's shell in another git repo) previously read the
  caller's CWD for these checks while later phases wrote to the cctally
  repo, allowing a clean `main` elsewhere to satisfy preflight against
  the wrong upstream.

## [1.0.0] - 2026-05-06

### Added
- Initial public release of cctally (mirror bootstrap).
