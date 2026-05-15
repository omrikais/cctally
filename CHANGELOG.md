# Changelog

All notable changes to this project are documented in this file. Format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed
- `cctally record-usage`: detect Anthropic-issued in-place weekly credits (utilization drops while `resets_at` stays unchanged) and emit a `week_reset_events` row + force-write `hwm-7d` + seed a post-credit snapshot so dashboard / forecast / report / percent-breakdown / TUI stop freezing at the pre-credit high-water mark. Fires on a `>=25.0pp` drop, the same threshold as the existing boundary-shift path that catches Anthropic-shifted `resets_at` advances mid-week. Deduped via belt-and-suspenders: a pre-check `SELECT 1 FROM week_reset_events WHERE new_week_end_at = ?` short-circuits before any INSERT attempt, the `UNIQUE(old_week_end_at, new_week_end_at)` DDL constraint absorbs any race that slips past, and the post-credit seed snapshot brings prior_pct down to ~current_pct so the next tick's drop predicate is naturally below threshold (single trigger per credit). HWM file `hwm-7d` is force-written via the credit-only escape hatch since the normal monotonic-up write path at `bin/_cctally_record.py:1511-1525` would refuse to decrease it; force-write lands AFTER `conn.commit()` of the event row so a concurrent reader doesn't see the new HWM before the durable signal of the credit.
- `cctally record-usage`: the monotonic 7d DB clamp now joins against `week_reset_events`, so post-credit `MAX(weekly_percent)` filters to samples captured at-or-after `effective_reset_at_utc`. Fresh OAuth values land naturally instead of being held back by pre-credit history. No-op when no event row exists for the week (`COALESCE` defaults the filter to epoch-zero, so the regression-guard `test_reset_aware_clamp_without_event_preserves_legacy_behavior` confirms byte-identical legacy clamp behavior on un-credited weeks).
- `cctally`: `_backfill_week_reset_events` extended to detect historical in-place weekly credits in existing DBs via the same predicate as the live path — parallel `elif prior_end == cur_end` branch inside the existing scan loop, same `prior_end_dt > captured_dt` + `>=25pp` drop gate, same `_floor_to_hour` for the effective moment. Idempotent via `UNIQUE(old_week_end_at, new_week_end_at)` + `INSERT OR IGNORE`; the existing boundary-shift branch is byte-identical to v1.7.1 so existing user DBs synthesize event rows for past credits without affecting prior backfill output.
- `cctally percent_milestones`: schema migration 005 adds a `reset_event_id` column (default 0 = pre-credit segment / no-event sentinel) and reshapes the UNIQUE constraint from `(week_start_date, percent_threshold)` to `(week_start_date, percent_threshold, reset_event_id)` so post-credit threshold crossings land as separate rows from any pre-credit ones at the same threshold. SQLite can't ALTER a UNIQUE constraint in place — the handler uses the rename-recreate-copy idiom inside its own `BEGIN/COMMIT`; fast-path probe stamps the marker without re-doing the rename when the column is already present (covers fresh-install + partial-failure-retry cases). Existing rows backfill to `reset_event_id = 0` via the column DEFAULT; the migration's per-migration goldens at `tests/fixtures/migrations/per-migration/005_percent_milestones_reset_event_id/{pre,post}.sqlite` are the first lazy-adopted entries under that directory pattern.
- `cctally percent-breakdown` + dashboard milestone panel + TUI percent-milestones panel: now filter milestone rows by the active `week_reset_events` segment for the queried week (the latest event keyed on the canonical hour-floored `week_end_at`). A credited week's header (which already reflects the post-credit window via the canon-boundary rewrite) is now coherent with its body — pre- and post-credit crossings read as independent ledgers. An empty post-credit segment renders a distinct "(post-credit segment, no milestones crossed yet)" hint so the user can distinguish a freshly-credited week from a genuinely silent one; without this, a fresh-credited week would render "No percent milestones recorded for this week" while the pre-credit ledger is still intact in the DB.
- `cctally` milestone writer (`maybe_record_milestone` + helpers): now stamps the active `week_reset_events.id` into `percent_milestones.reset_event_id` so post-credit threshold crossings land as separate rows; `get_max_milestone_for_week`, `get_milestone_cost_for_week`, the `alerted_at` UPDATE inside the writer, and the post-INSERT cumulative-cost SELECT for the alert payload all gained a `reset_event_id` filter. Active-segment resolution uses `unixepoch()` on both sides of the `<=` comparison to absorb the `+00:00` vs `Z` offset mix between `week_reset_events.effective_reset_at_utc` (stored as `+00:00`) and a snapshot's `captured_at_utc` (stored as `Z`). Combined with the new UNIQUE shape this means a credited week sees post-credit 1% / 2% / 3% alerts fire fresh even if the pre-credit ledger already crossed those thresholds; the self-heal probe in the dedup-no-insert bail-out path is now also segment-scoped so the post-credit ledger doesn't get silently suppressed by a high pre-credit MAX.
- `cctally doctor`: new `data.post_credit_milestones` check warns when a credited week (`week_reset_events` row with effective < now) has `latest_weekly_percent >= 1.0` AND zero post-credit milestone rows. Informational WARN (no remediation), since the next `record-usage` tick at >=1% will self-heal via the segment-aware probe — surfaces the upgrade-window gap between when the credit lands and when the user accumulates enough usage to cross the post-credit 1% threshold. The `weekly_percent < 1.0` short-circuit prevents false-positive warns immediately after a credit when the user simply hasn't started using the new segment yet.
- `cctally record-usage`: round-3 user-test follow-up — defensive cleanup in the in-place credit detection branch. Between the moment Anthropic credits the user and `cctally record-usage` firing, the external `claude-statusline` tool can replay stale pre-credit `--percent` values (its in-memory HWM cache hasn't refreshed yet) — those replays land `captured_at_utc >= effective_reset_at_utc` and poison the reset-aware clamp's MAX over the post-credit segment, blocking legitimate fresh OAuth values from landing. The credit branch now runs a narrow DELETE pass scoped to `(week_start_date = ?, captured_at_utc >= effective_iso, round(weekly_percent, 1) = round(prior_pct, 1))` after writing the event row + force-writing `hwm-7d`. Strict-equality predicate avoids deleting legitimate post-credit climbs. Reported by user on the v1.7.2 dev branch with manual recovery already applied to the production DB; fix prevents recurrence.
- `cctally report` / `weekly`: round-3 user-test follow-up — credited weeks now render as TWO trend rows (pre-credit segment closed at `effective_reset_at_utc` AND post-credit segment opening at `effective_reset_at_utc`). Previously only the post-credit segment surfaced and the pre-credit segment's usage + cost (the bulk of the week's spend in the originating incident: 67% / $1484 across 6 days) was silently dropped from the trend table. `_apply_reset_events_to_weekrefs` synthesizes the pre-credit ref alongside the post-credit one for events whose row shape is `old_week_end_at == effective_reset_at_utc` (the in-place credit marker — boundary-shift events stay single-ref). `cmd_report`'s per-trend-row usage lookup now passes `as_of_utc = ref.week_end_at` for credited weeks so each segment renders its own latest snapshot (the shared `week_start_date` lookup key would otherwise return the post-credit value for both rows); non-credit weeks still use the unfiltered lookup so existing test fixtures that seed snapshots outside the API-derived week window keep finding their rows.
- `cctally blocks`: round-3 user-test follow-up — `_load_recorded_five_hour_windows` now overlays canonical anchors from `five_hour_blocks.five_hour_resets_at` (heavy-weight = 1000 per row) on top of the existing `weekly_usage_snapshots.five_hour_resets_at` source. The canonical rollup table holds the API-anchored 5h reset moment after `_canonical_5h_window_key` has absorbed Anthropic's seconds-level jitter; the heavy weight ensures that whenever the rollup table sees a window, the rollup's anchor always wins over any jittered raw snapshot value at the same 10-minute-floored key. Symptom this fixes: after an in-place credit, `cctally blocks` showed the ACTIVE row with the heuristic `~HH:MM` prefix while `cctally five-hour-blocks` correctly showed `⚡ HH:MM` (API-anchored). Both views now agree on the API anchor whenever the rollup table has it.
- `cctally report`: round-4 user-test follow-up — the "current week" summary box no longer renders the PRE-credit row (the closed segment) for credited weeks. Round-3's pre-credit ref synthesis in `_apply_reset_events_to_weekrefs` left both refs sharing `WeekRef.key`, so the match predicate `week_ref.key == current_ref.key` matched BOTH refs in `cmd_report`'s `current_row` loop and last-write-wins picked whichever was processed last. `current_ref` is now routed through `_apply_reset_events_to_weekrefs` itself so its `week_start_at` reflects the post-credit segment's effective start, and the row-match tightens to require BOTH `key` AND `week_start_at` equality — the pre-credit ref (original `week_start_at`) no longer overwrites the post-credit row's selection. Non-credited weeks are unaffected (`_apply_reset_events_to_weekrefs` is a no-op without an event row). On the user's live DB: summary box now correctly shows post-credit `4%` instead of pre-credit `67%`.

## [1.7.1] - 2026-05-15

### Fixed
- `cctally record-usage` / `sync-week`: `week_start_date` bucket key is now anchored on the canonical UTC calendar day of `week_start_at`, not the host-local-TZ `.date()` of the parsed datetime. When the cctally process briefly inherits a non-UTC `TZ` (e.g., `TZ=America/Los_Angeles` for a `+03:00` host process during refactor work), the same physical subscription week silently forks across two `week_start_date` values, leaving `cctally report` Trend with two rows per current window — one frozen at the moment of the TZ flip, one still updating. The writer fix at `_derive_week_from_payload` / `pick_week_selection` prevents new ghosts; a companion self-heal migration `004_heal_forked_week_start_date_buckets` merges any pre-existing forked rows on the next `open_db()` (usage/cost UPDATE the date columns to `substr(at, 1, 10)`; milestones DELETE on `UNIQUE(week_start_date, percent_threshold)` collision against the canonical row, else UPDATE). A new `data.forked_buckets` doctor check (visible in `cctally doctor` and the dashboard) surfaces the invariant as `fail` with per-table counts so the next regression is visible immediately. `_bootstrap_rename_legacy_markers` is now idempotent against the duplicate-marker case — both the legacy unprefixed and the new prefixed marker rows present from a back-and-forth across cctally versions — by DELETEing the legacy row when its prefixed counterpart already exists and preserving the prefixed row's authoritative `applied_at_utc`; previously the plain UPDATE collided on `schema_migrations.PRIMARY KEY` and permanently blocked the dispatcher from running any subsequent migration (including the heal).
- `brew` installs: `cctally --help`, `cctally doctor`, the dashboard share GUI, and the CLI `--format md|html|svg` flag no longer crash with `FileNotFoundError` looking for runtime sibling modules. The Homebrew formula template's install block enumerated only `USER_FACING_BINS` since v1.4.0, so the lazy-loaded `_lib_doctor.py` / `_lib_share.py` / `_lib_share_templates.py` siblings never reached `libexec/bin` on brew layouts — `doctor`, the share modal, and the `--format` flag have been latently broken on every brew install since they landed. The v1.6.1 CHANGELOG note that "Homebrew copies the whole prefix; brew unaffected" was incorrect — the formula has always copied a per-name list, not the whole prefix. The bin/cctally split refactor on this branch promoted `_lib_semver` to an EAGER import at `bin/cctally:213`, which would have turned the latent crash into an immediate one (`cctally --help` itself stops resolving on a brew install missing the sibling). `homebrew/cctally.rb.template` now installs every `bin/_lib_*.py` and `bin/_cctally_*.py` runtime sibling via `Dir.glob` alongside `USER_FACING_BINS`, and `tests/test_package_files.py` gains a parity guard so future sibling additions can't silently drop out of the brew install layout. The next release cut after this branch merges ships a working brew formula for the first time since v1.4.0.
- `update` (self-heal): `_self_heal_current_version` no longer corrupts the global `update-state.json` when `cctally` is invoked from a development clone. The post-command hook reads `CHANGELOG_PATH` via `__file__` (resolved against the dev tree's `CHANGELOG.md`, not the installed binary's), so any `./bin/cctally` invocation from the source tree — including the six phases of `cctally release` itself — stamped `current_version` to whatever the dev tree's CHANGELOG claimed, masking the actually-installed version on the user's machine until the next `rm ~/.local/share/cctally/update-state.json`. The self-heal now early-returns when a `.git/` directory sits next to `CHANGELOG_PATH`, since production tarballs (npm tar, brew archive) never ship `.git/`; legitimate out-of-band upgrades on installed npm/brew binaries still self-heal as before. Symmetric twin of the v1.7.0 brew fix (CHANGELOG-via-`__file__` ≠ installed-binary's CHANGELOG); same root cause, different trigger. Resolves [#42](https://github.com/omrikais/cctally-dev/issues/42).

## [1.7.0] - 2026-05-13

### Added
- `cctally doctor` — read-only diagnostic subcommand consolidating install / hooks / OAuth / DB / freshness / safety state into one severity-ranked report (human + JSON; exit 0 unless any check FAILs, then 2); the dashboard exposes the same diagnostic via an aggregate-health header chip and a full-report modal opened by clicking the chip or pressing `d`, backed by `GET /api/doctor`.

### Changed
- `share`: Detail templates for `weekly` / `daily` / `monthly` / `blocks` now ship cross-tab data (per-week × per-model, per-day × per-project, per-month × per-model, per-block × per-project) in their MD and HTML exports — resolves the per-project narrowing landed in M2.1 ([#33](https://github.com/omrikais/cctally-dev/issues/33)). SVG output for these templates continues to omit the table body and is tracked separately at [#38](https://github.com/omrikais/cctally-dev/issues/38).

### Fixed
- `update`: Dashboard version label and CLI banner no longer stay frozen on the pre-upgrade version after an out-of-band install (`npm install -g cctally@X` outside `cctally update`) — a new self-heal compares the running binary's CHANGELOG against `update-state.json` on every CLI command and every dashboard tick, re-stamping `current_version` when they disagree. Also fixed the underlying bug where `cctally update` (without `--version`) stamped the cached `latest_version` as the just-installed version: a stale probe from before the registry advanced caused `current_version` to land on the wrong value even though npm had actually fetched a newer release. `_stamp_install_success_to_state` now prefers the freshly-installed CHANGELOG, falling back to `latest_version` only when CHANGELOG is unreadable.
- `update` (brew): `cctally update` on a brew install no longer stamps the pre-upgrade version into `update-state.json` when no `--version` is supplied. The running Python process has `CHANGELOG_PATH` bound to the OLD Cellar, so the CHANGELOG read returned the pre-upgrade version and `current_version` landed on the wrong value until the next dashboard self-heal (up to 30 min on the worker thread). `_stamp_install_success_to_state` now takes the resolved `InstallMethod` and short-circuits to `state.latest_version` (the freshly-probed value that drove the install) on the brew + no-explicit-version path; npm and explicit-`--version` paths are unchanged so the prior stale-probe regression (1.6.0-after-installing-1.6.3) stays fixed.
- `doctor`: `safety.update_suppress` no longer warns "bad types: remind_after" against the canonical producer shape. `cctally update --remind-later` writes `remind_after` as a dict `{"version", "until_utc"}` and the banner predicate consumes that shape — but the doctor validator only accepted `None`/str/numeric and flagged every legitimate deferral as a corrupt file, recommending the user delete `update-suppress.json`. The validator now accepts the dict shape alongside the legacy scalar form.
- `dashboard` (keymap): Doctor-modal global-key guard. `q` (quit), `r` (sync), `1`-`9` (panel modals), and `n`/`N` (search step) now skip when the Doctor modal is open — previously they fired underneath the modal, popping panel modals into ModalRoot or quitting the dashboard behind a still-visible Doctor card. Update modal's symmetric guard was already in place; this folds `doctorModalOpen` into the same predicate.
- `doctor`: Symlink check no longer reports `0/N present; missing …` when the running cctally invocation belongs to a different install than the one that owns `~/.local/bin/cctally-*` (e.g., source-tree dev iteration with a parallel npm/brew install). The strict equality check in `_setup_compute_symlink_state` compared each symlink's target to `<repo_root>/bin/<name>` derived from `__file__`, so launching the dashboard via `python3 <source>/bin/cctally dashboard` against an npm-installed user's symlinks would classify all 13 entries as `wrong`, render them under the "missing" label, and falsely prompt the user to re-run `cctally setup`. The diagnostic now asks the right question — "is `cctally-X` invokable from PATH?" — by accepting any symlink whose target is reachable; `_setup_create_symlinks` keeps its own strict equality for install-management decisions. Also flips dangling symlinks from the previously-conflated "missing" classification to the correct "wrong" state.

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
