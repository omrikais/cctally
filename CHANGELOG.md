# Changelog

All notable changes to this project are documented in this file. Format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `cctally doctor` — read-only diagnostic subcommand consolidating install / hooks / OAuth / DB / freshness / safety state into one severity-ranked report (human + JSON; exit 0 unless any check FAILs, then 2); the dashboard exposes the same diagnostic via an aggregate-health header chip and a full-report modal opened by clicking the chip or pressing `d`, backed by `GET /api/doctor`.

### Changed
- `share`: Detail templates for `weekly` / `daily` / `monthly` / `blocks` now ship cross-tab data (per-week × per-model, per-day × per-project, per-month × per-model, per-block × per-project) in their MD and HTML exports — resolves the per-project narrowing landed in M2.1 ([#33](https://github.com/omrikais/cctally-dev/issues/33)). SVG output for these templates continues to omit the table body and is tracked separately at [#38](https://github.com/omrikais/cctally-dev/issues/38).

## [1.6.3] - 2026-05-12

### Fixed
- npm tarball for v1.6.2 was built by the public-repo GHA workflow from a pre-fix tree (the workflow fired on a tag push that briefly pointed at the wrong commit before the cut was redone). The published v1.6.2 npm package therefore lacks `bin/_lib_share_templates.py` even though the GH release page, brew formula, and public repo all carry the correct fixed commit. v1.6.3 republishes the same v1.6.2 content under a fresh version so `cctally update` and fresh `npm install -g cctally` resolve to a working build. npm `cctally@1.6.2` will be deprecated post-publish; brew users on v1.6.2 are unaffected (brew builds from the GH archive which points at the corrected commit).

## [1.6.2] - 2026-05-12

### Fixed
- v1.6.1's `package.json` `files[]` edit was a necessary but incomplete fix for the dashboard share GUI on npm installs: `bin/_lib_share_templates.py` also needed to be promoted to public in `.mirror-allowlist`, where it was lingering as `unmatched` from a stale "private kernel adjunct" classification dating to share-v2 implementation. The npm-publish GHA workflow runs from the public clone, so a file the mirror filters out never reaches the tarball regardless of `files[]`. v1.6.2 promotes the module and removes the stale comment block; v1.6.1's CLI `--format` fix is unchanged. The `tests/test_package_files.py` guard now ALSO asserts every `files[]` path classifies as `public` against `.mirror-allowlist`, so a future runtime-sibling promotion that updates only one of the two layers can't ship.

## [1.6.1] - 2026-05-12

### Fixed
- npm-installed `cctally` now ships the `bin/_lib_share.py` and `bin/_lib_share_templates.py` runtime sibling modules in the package tarball. They were latently absent from `package.json` `files[]` since v1.4.0; brew and source installs were unaffected (Homebrew copies the whole prefix). On npm installs the dashboard share GUI failed at "Couldn't load templates: Load failed" on every panel because the lazy-loader couldn't open the sibling file. A new `tests/test_package_files.py` guard asserts every `bin/_lib_*.py` runtime module is enumerated in `files[]` so a future sibling addition can't silently drop out of the npm distribution.

## [1.6.0] - 2026-05-12

### Added
- Dashboard share GUI: per-panel `↗` share icon opens a modal with 24 infographic templates (8 panels × 3 archetypes), live preview, themed export to MD/HTML/SVG, client-side PNG, and browser-native Print → PDF. Keyboard: `S` shares the focused panel, `B` opens the basket composer.
- Multi-section composer: collect template recipes from any panel into a `📋 basket` (localStorage-persisted, hard cap 20), then stitch them with `/api/share/compose` into one document under composite chrome (single title, single frontmatter, one footer). Sections show "Outdated" when underlying data or kernel version has shifted; per-section refresh re-renders without losing the basket order.
- Share presets + history: save the current template + knob recipe under a panel-scoped name (`/api/share/presets`); recall presets and the last 20 export recipes via the gallery's `presets ▾` dropdown.
- New endpoints: `GET /api/share/templates`, `POST /api/share/render`, `POST /api/share/compose`, full CRUD on `/api/share/presets` and `/api/share/history`. All write paths CSRF-gated; compose is recipe-only (client-supplied bodies are silently ignored — privacy chokepoint preserved).

### Changed
- Markdown exports now carry YAML frontmatter (title, generated_at, period, panel, anonymized, cctally_version). Same set of v1 share goldens churn once with this release. Stripped by `--no-branding`.

### Docs
- New user-facing reference: `docs/commands/share-v2.md`.

## [1.5.0] - 2026-05-11

### Added
- `cctally update` subcommand for self-updating npm and Homebrew installs, with auto-suggest banner in CLI and amber update badge in the dashboard, plus `--check`, `--skip`, `--remind-later`, `--version` (npm only), `--json`, `--dry-run` flags. Source/dev installs fall through to a manual-recipe message. Dashboard modal streams live subprocess output and survives subprocess `execvp` restart via SSE auto-reconnect.
- `update.check.enabled` and `update.check.ttl_hours` config keys for opting out of automatic version checks or extending the 24-hour default TTL up to 30 days.
- `cctally setup` auto-detects hooks from prior install patterns under `~/.claude/hooks/` (`record-usage-stop.py`, `usage-poller-{start,stop}.py`, `usage-poller.py`) and offers to migrate them: unwires the matching `settings.json` entries, moves the `.py` files to a timestamped `~/.claude/cctally-legacy-hook-backup-<UTC ts>/` directory (reversible — files moved, not deleted), and best-effort stops any currently-running background daemon those hooks spawned. Before sending SIGTERM, the migration verifies the PID at `/tmp/claude-usage-poller.pid` is actually the legacy `usage-poller.py` process (via `ps -p <pid> -o command=`) so a stale sentinel pointing at a recycled PID is treated as `stale-pid` rather than risking a kill against an unrelated user process. The backup directory is resolved before the settings write so a directory-creation failure (unwriteable parent, name collision, disk full) exits 1 with `~/.claude/settings.json` byte-identical, never leaving a half-applied state. New flags `--migrate-legacy-hooks` / `--no-migrate-legacy-hooks` for non-interactive control (install-mode only; rejected with exit 2 against `--status` / `--uninstall`). `setup --status` reports the migration state in both text and JSON (`legacy.bespoke_hooks`); `setup --dry-run --migrate-legacy-hooks` previews without touching disk and warns when `~/.claude/settings.json` is malformed.
- `npm install -g cctally` now prints a one-time hint pointing to `cctally setup` after install, mirroring what brew already shows via `Formula#caveats`. The postinstall hook (`bin/cctally-npm-postinstall.js`) is gated on `npm_config_global=true` so per-project node_modules pulls stay silent, never auto-executes `cctally setup` (which is interactive and writes outside the package surface), and honors `CCTALLY_NPM_POSTINSTALL_QUIET=1` as an escape hatch for CI / fixtures.

### Fixed
- `release` Phase 6 now refuses (exit 2) to write a brew formula whose URL pins a *lower* SemVer than the on-disk `Formula/cctally.rb` — the monotonic-version gate that closes the regression class behind issue #30, where the brew tap silently rolled back from v1.3.0 to v1.0.0 twice in one day. The gate compares with SemVer-aware ordering (stable > prerelease at the same MAJOR.MINOR.PATCH per §11.4) so prerelease promotions still flow through. New `--allow-formula-downgrade` flag overrides the gate for genuine yank/revert cases and prints a loud stderr warning when invoked.

## [1.4.0] - 2026-05-09

### Added
- Shareable reports — all 8 reporting subcommands (`report`, `daily`, `monthly`, `weekly`, `forecast`, `project`, `five-hour-blocks`, `session`) now accept `--format md|html|svg` to emit shareable artifacts to a filename like `cctally-<cmd>-<utcdate>.<ext>`. Flags: `--theme light|dark`, `--no-branding`, `--reveal-projects` (project labels are anonymized to `project-N` by default), `--output <path>` / `--output -` for stdout, `--copy` (markdown only), `--open` (html/svg only). `session --format` also accepts `--top-n N` to cap the chart's project breakdown. See `docs/commands/share.md` and the per-command "Shareable output" sections.

### Fixed
- Dashboard 5-hour row now shows the post-reset delta (`⚡ Δ +Xpp this block`) when a 5h block spans a weekly reset, instead of suppressing the number behind a `⚡ reset` line. The cross-reset flag now detects natural weekly boundaries from `weekly_usage_snapshots.week_start_at` in addition to Anthropic-shifted mid-week resets, and all interval comparisons normalize through `unixepoch()` so the flag flips correctly on non-UTC hosts (the prior lex-compare silently failed for `+03:00` and other non-zero offsets, leaving the panel showing a misleading `Δ −94pp this block`).
- `record-usage`: self-heal `percent_milestones` and `five_hour_blocks` rows that were silently dropped when an earlier invocation was killed between snapshot insert and milestone insert (e.g. Claude Code self-update kill window). On a dedup'd tick, re-runs the idempotent milestone helpers against the latest snapshot — recovering missed rows at the next status-line tick instead of waiting for the percent to advance.
- Root `.gitignore` now anchors `/node_modules` and `/package-lock.json`, preventing `npm install` next to the repo-root `package.json` (the npm-publish sentinel) from leaving the working tree dirty and blocking `cctally release`. `dashboard/web/node_modules` and the tracked `dashboard/web/package-lock.json` are unaffected by the anchored entries.

## [1.3.0] - 2026-05-08

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
