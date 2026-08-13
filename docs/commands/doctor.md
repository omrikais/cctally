# `cctally doctor`

Read-only diagnostic. Answers the question: "why is my cctally data
stale or broken?" by running every passive check across install,
hooks, OAuth, database, data freshness, pricing coverage, and safety
config, then emitting a severity-ranked report.

## Modes

| Mode | What it does |
|---|---|
| `cctally doctor` | Human-readable report |
| `cctally doctor --json` | Machine-readable JSON to stdout |
| `cctally doctor --quiet` / `-q` | Human mode; hide OK rows |
| `cctally doctor --verbose` / `-v` | Human mode; include per-check `details` blocks |

`--quiet` and `--verbose` are mutually exclusive.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All checks are OK or WARN |
| 2 | Any check is FAIL |

Loose mapping (WARN doesn't cause non-zero) makes `cctally doctor`
usable as a healthcheck without false-positive noise:
`cctally doctor || alert-me`.

## Severity model

| Level | Meaning |
|---|---|
| `OK` | Healthy. No user action needed. |
| `WARN` | Degraded but functional. Data still flowing; user may want to act. |
| `FAIL` | Broken. Data is wrong, or a critical workflow won't work. |

## Check inventory

Ten categories. Each check has a stable `id` (used as the
JSON key), a one-line summary, and a remediation hint shown when
severity != `OK`.

### Install
- `install.symlinks` — WARN when any cctally-* command is unavailable. Reports "N/M available", counting `available = ok + stale`. PATH-aware: a command is counted available when its `~/.local/bin/` symlink is present, **or** when the command is reachable on `$PATH` via another install channel (e.g. a Homebrew `<prefix>/bin/` install), so it no longer false-warns purely because `~/.local/bin/` lacks the link. A leftover link to an old Homebrew keg (`<prefix>/Cellar/cctally/`) or the npm shim, whose command is still reachable elsewhere, is reported as a cleanable **`stale`** state (counted available, listed in the new `--json` `details.stale` array) rather than a generic failure — the summary appends "N stale link(s) to clean" and the remediation is `Run cctally setup to clean stale links`. A wrong-target / dangling / non-symlink slot still counts as missing (`wrong`). One pinned-only-path case is special-cased: when cctally is reachable **only** through a legacy `~/.local/bin/` link to a keg (so `cctally setup` deliberately won't remove the only working copy), the remediation switches to a PATH-fix hint ("Put `<prefix>/bin` on your PATH (e.g. `eval "$(brew shellenv)"`), then run `cctally setup` to remove the legacy link"). The `--json` `details` keys `present` / `total` / `missing` are unchanged (`missing` spans `wrong + missing`); `details.stale` is additive.
- `install.path` — availability-aware: OK whenever cctally is reachable on `$PATH` via **any** channel (Homebrew `<prefix>/bin/`, an npm prefix, or source `~/.local/bin`), summary `cctally reachable on $PATH`. WARN (`cctally not reachable on $PATH`) only when no channel makes it reachable; the remediation is channel-aware — a Homebrew keg is pointed at `eval "$(brew shellenv)"` (it owns no `~/.local/bin` symlinks per the #119 policy), while source / npm installs get the `export PATH="$HOME/.local/bin:$PATH"` + `cctally setup` fix.
- `install.update_channel` — reports the configured update (release) channel `cctally update` tracks (`stable` | `beta`), distinct from the preview `channel` (prod|preview) in `install.mode`. OK for stable, or beta on npm/source. WARN on the beta+brew mismatch — Homebrew tracks the stable channel only (a beta opt-in silently resolves stable); the remediation points at npm/source or `cctally config set update.channel stable`. Never FAIL, so it doesn't affect the exit code.
- `install.legacy_snippet` — WARN when an old status-line snippet is detected.
- `install.legacy_bespoke_hooks` — WARN when the legacy hand-installed hooks are present.

### Hooks
- `hooks.installed` — WARN when any of `PostToolBatch`/`Stop`/`SubagentStop` entries are missing.
- `hooks.statusline_refresh_interval` — WARN only when a recognized cctally `statusLine` command is present but has no `refreshInterval` (state `missing`); the remediation is `Run cctally setup to add statusLine.refreshInterval, or set it manually`. Without it, statusline-fed usage persistence goes quiet while a coordinator waits on a long subagent (see [setup.md](setup.md#statuslinerefreshinterval) and [statusline.md](statusline.md#keeping-usage-fresh-during-subagent-waits-statuslinerefreshinterval)). Every other state is OK with its own summary — `present` (set), `absent` (no statusLine configured), `foreign` (a custom, non-cctally statusLine), and `unavailable` (settings.json unreadable — the `hooks.installed` / settings warnings already surface that, so this check does not double-WARN).
- `hooks.recent_activity_24h` — WARN when no hook has fired in 24h, or error/fire ratio ≥ 0.5.
- `hooks.last_fire_age` — WARN when the last fire was >1h ago or never.
- `hooks.codex_installed` — root-qualified Codex hook state. With no detected
  Codex root it is OK/not applicable. It is WARN when any detected root is
  missing, malformed, or feature-disabled; exact owned handlers are OK only
  when every root is installed. Its additive details include sorted
  `states: [{source_root_key, state}]`, root/install counts,
  `requires_review`, and `trust_state`. A status of
  `installed_trust_unobservable` means cctally can recognize the handler but
  cannot determine whether Codex has trusted it; verify it in Codex `/hooks`.
- `hooks.codex_recent_activity` — root-qualified success/error activity from
  the last 24 hours for installed Codex handlers. It is WARN when any installed
  root has never succeeded or was last successful more than 24 hours ago; it
  is OK/not applicable when no owned Codex handler is installed. Details carry
  a sorted `roots` array plus the worst-state representative, never a session
  path or conversation payload.

### Auth
- `oauth.token_present` — FAIL when the OAuth token file is missing.

### Database
- `db.stats.file` — WARN when stats.db is absent (fresh install); FAIL when present but cannot open. If an absent, empty, or partial destination has a matching interrupted-rebuild scratch family and journal evidence, Doctor reports either "rebuild in progress" (maintenance flock held) or "interrupted rebuild detected" (stale). The entire Doctor gather suppresses recovery, including nested stats probes, so it never replaces the destination or reclaims scratch; run any normal report command or restart the dashboard to trigger automatic recovery, and use `cctally db rebuild --db stats` only if that retry fails.
- `db.cache.file` — WARN when cache.db is absent; FAIL when present but cannot
  open. It also reports a live `cache.db.repairing` owner as "repair in
  progress" without destructive advice, and a dead/malformed/PID-reused owner
  as "stale repair owner" with the proven
  `cctally cache-sync --rebuild` remediation. This marker probe is read-only.
  Doctor holds the existing cache maintenance lock shared across every raw
  cache probe; when a repair or pending quarantine exists it skips those
  SQLite opens, so diagnostics cannot appear beneath the recovery drain check.
  Transcript rollup/page-count probes and the deep `conversations.db`
  quick-check likewise hold conversation maintenance shared and degrade without
  opening SQLite while transcript recovery owns or has marked the store. A
  durable incomplete transcript rebuild reports WARN rather than treating an
  absent or partially repopulated store as healthy.
- `db.integrity` — runs read-only `PRAGMA quick_check(1)` on `stats.db`, `cache.db`, and `conversations.db`. FAIL when **stats.db** reports corruption or cannot be opened for the check. When cctally has retained journal data for this installation, `stats.db` is a disposable index derived from it and the damage is normally healed by an automatic rebuild before you ever see this check fail; run `cctally db rebuild --db stats` if it has not been. On a pre-cutover installation with no retained journal data there is nothing to rebuild from and `stats.db` may be the only copy of your recorded history, so remediation points at `cctally db repair --db stats --yes`, which preserves the corrupt original before a verified atomic replacement. WARN when a re-derivable store is corrupt: **cache.db** or **conversations.db** both use the verified `cctally cache-sync --rebuild` recovery command, and the summary names the affected store. Stats retains precedence when more than one leg is unhealthy. OK when every present store reports `ok`. This check runs **only from the CLI** (`cctally doctor` gathers with a `deep=True` flag); the dashboard health modal, whose gather runs on every rebuild, skips all three checks because a large-store scan can cost seconds — there it shows "not checked (fast gather — run `cctally doctor`)".
  The dashboard does not infer transcript corruption from generic SQLite error
  text or run its own `quick_check`: conversation-route open failures return a
  transcript-specific, privacy-safe 500 and leave core panels/SSE fail-soft.
  A typed `conversations.db` background failure remains the generic
  `server_sync` notice rather than manufacturing a `cache.db` recovery action.
- `db.version_ahead` — classifies each DB's `user_version` versus what this binary expects. **stats.db** follows the EPOCH model (DB journal redesign §7.1): `user_version == STATS_INDEX_EPOCH` (a cut-over install) is HEALTHY, `user_version <= 13` (a pre-cutover legacy install) is HEALTHY (it cuts over on the next open), and `user_version > 13` but `!= epoch` is a §7.1 index **mismatch** → WARN. When the journal's latest segment has data, the warning says that the disposable index rebuilds from the journal and points at `cctally db rebuild --db stats` (NOT the retired `db recover --db stats`). When no journal data exists—even if an empty `journal/` directory exists—Doctor does not promise an impossible auto-heal: it says to restore the journal first, then rebuild. **cache.db** is unchanged (issue #145): a `user_version` past the cache registry head → WARN, auto-heals on the next open (remediation: it heals automatically, or run `cctally db recover --db cache`). OK ("none ahead") otherwise. `doctor` reads the raw `user_version` and journal high-water state without invoking the migration dispatcher, so it reports without healing, rebuilding, or bricking.
- `db.migrations.applied` — WARN on `skipped` rows; FAIL on `failed` rows.
- `db.migrations.pending` — WARN when any migration is pending.
- `db.lock_state` — informational (always OK). A non-blocking flock probe reports whether a core sync lock (`cache.db.lock` / `cache.db.codex.lock`) or transcript lock (`conversations.db.lock` / `conversations.db.codex.lock` / `conversations.db.maintenance.lock`) is currently held; a held lock usually just means an active sync, transcript maintenance, or dashboard is running, so it never WARNs. The summary notes that a hold persisting across repeated `doctor` runs may indicate a wedged process. Read-only — the probe never creates the data dir or the lock files (it opens existing files read-only).
- `db.wal_size` — WARN when `cache.db-wal` exceeds 256 MiB, indicating that the normal WAL cap/checkpoint defenses have not contained it; remediation is `cctally db checkpoint`.
- `db.reclaimable` — WARN when at least 25% of `cache.db` pages are on SQLite's freelist, meaning a substantial part of the file can be returned to the filesystem. Remediation is `cctally db vacuum --db cache`. The probe reads `PRAGMA page_count` and `PRAGMA freelist_count` only; it never vacuums or otherwise mutates the database. An absent or unreadable cache degrades to OK, and the raw counts plus ratio are available in the unstable `details` block.
- `db.retained_artifacts` — the corruption evidence cctally has retained, measured against `storage.artifact_retention` (#496). Reports retained, reclaimable and protected bytes, the free disk, and any pending reclamation that cannot finish. OK when the corpus is inside its policy. WARN when reclamation is due and would satisfy the policy (remediation `cctally db prune`), when the metadata walk stopped at its entry cap and the figures cover only part of the corpus, when the scan could not run at all — the reason is named, because a silent OK there reads exactly like a healthy install with nothing retained — or when a reclaim plan has carried a fail-closed entry for more than 24 hours; that last one names the plan id and the member to inspect, because no reclamation pass can decide it and the file must be removed by hand. FAIL when protected evidence holds the corpus over a bound, or when the policy block is malformed, in which case automatic reclamation is switched off until it is fixed. Both the FAIL and the WARN summary name the bound at issue by its own configured value — the age bound, the per-family count, the size budget or the free-disk floor — and every one of them when more than one applies. The last surviving example of each distinct damage shape is reported as information and never as a failure: it is retention the operator asked for through `max_shape_examples`, and treating it as a problem would produce a FAIL no action can clear. The scan is read-only, takes no lock, and is bounded to two directory levels and 5000 entries. It runs **only from the CLI** (`deep=True`), like `journal.integrity` and `journal.conflicts`; the dashboard's and TUI's per-rebuild gather shows "not scanned" and still reports a malformed policy and a stuck reclaim record, both of which cost one file read and one directory listing.
- `db.conversations_reclaimable` — applies the same read-only 25% freelist threshold to `conversations.db`, with remediation `cctally db vacuum --db conversations`. The transcript-store probe uses a zero-timeout read-only connection, so a large reingest or maintenance lock cannot stall `doctor`; a locked, absent, or unreadable transcript store degrades to OK with unavailable counts.

### Journal

The append-only journal is the durable truth for stats.db (DB journal redesign §9). All eight legs are read-only.
- `journal.presence` — reports the `journal/` directory. A pre-cutover (legacy) install has NO journal yet: that is INFO/OK ("no journal (pre-cutover install)"), never a FAIL. When present it is OK ("N segment(s), writable"), or WARN if the directory is not writable.
- `journal.integrity` — mid-file **malformed** lines are external damage → WARN (every other line stays independently parseable — the ingester skips + counts the bad ones); a **torn final line** is a known crash artifact healed by the next append → INFO. The scan reads whole segments, so it runs **only from the CLI** (`deep=True`); the dashboard's per-rebuild gather shows "not scanned".
- `journal.index_freshness` — the stats index **cursor** vs. the journal high-water, in bytes. WARN when the unconsumed gap exceeds 4 MiB (no ingest cycle has run for a long stretch; a monthly segment is MB-scale), remediation `cctally db rebuild --db stats` (or just run any cctally command — the ingester consumes the backlog). Caught-up / small-gap → OK with the gap shown. No journal/cursor yet → OK.
- `journal.auto_heal` — auto-heal incidents and whether they are recurring (#496). An **incident** is a quarantine directory under `quarantine/` and nothing else; a **detection** is an entry in the durable heal ring, keyed by its heal id; a `logs/<db>-corruption-forensics-*.json` bundle is linked evidence and is neither, so a directory and the bundle written moments before it count as one incident rather than two. OK when there is no incident and no detection. WARN for a single historical incident or for detections that are not recurring — the summary states the count and a relative age (`3 incidents, 4h ago; no recurrence in 7d`), so a sub-day incident no longer reads as `0d ago`. FAIL on at least three detections in seven days, or on the same damage shape appearing in two distinct incidents within seven days; the literal shape token `none` is not a shape and never triggers this. Remediation names the bundles and the heal-event log to report.
- `journal.writer_guard` — reports unauthorized stats.db write attempts captured by the runtime authorizer. The leg is read-only; a recorded violation is FAIL and names the guarded source rather than mutating or repairing the index. Installed builds throttle the log across processes, rotate one generation at 1 MiB, and Doctor reads at most the newest 256 lines / 64 KiB.
- `journal.conflicts` — **divergent same-revision event groups quarantined behind a provisional winner** (#374). WARN, never FAIL: the index is complete and usable, we simply refuse to assert that a guessed (first-written) winner is authoritative. Emitted **only when selection completes**; the remediation names `cctally db rederive --family claude-usage` only when at least one group belongs to a re-derived Claude family (it is the wrong remedy for a retained `qaa:` state stream or an unknown prefix). The scan uses full effective-selection semantics over account-normalized records — raw `(id, rev)` grouping would report superseded revisions and false account conflicts — so it runs **only from the CLI** (`deep=True`); the dashboard's per-rebuild gather shows "not scanned".
- `journal.protocol` — FAIL, taking `doctor` to exit 2, when recognized structural correction-batch violations remain unacknowledged. Selection and rebuild complete with each whole affected batch omitted; details name every batch, kind, bounded evidence set, and stable fingerprint, and remediation gives the exact preview plus fingerprint-selected `cctally db journal-repair ... --yes` command. Deep doctor, journal-repair, and rebuild all count every successfully decoded physical line when deriving sequence-bearing evidence, but retain decoded dictionaries only for selector decision records; irrelevant observations occupy lightweight positional placeholders. Their fingerprints therefore agree without making diagnostic or repair memory track decoded observation history. After every current violation is acknowledged, the leg becomes WARN—not OK—and retains the omitted batches, audit ids, reviewed high-waters, and prefix hashes in `acknowledgedViolations`. Rebuild and live preflight persist both states in the disposable index, so shallow Dashboard/TUI gathers remain truthful without rescanning the journal. An invalid marker/action shape or other out-of-scope selector failure remains a distinct FAIL where `journal.conflicts` is unavailable. `db rederive` is deliberately not offered as the structural remedy.
- `journal.quota_projection` — the durable **incomplete-quota-projection** flag carried inside the published stats generation (#496 S5b §4.7). A rebuild whose Codex quota cache recovery stopped short publishes a semantically partial projection and sets the flag, and every quota-projection read is then refused until something reconciles it. WARN, never FAIL: the index is valid and no data is lost. Remediation is `cctally cache-sync`, which is one of only two things that clear the flag — the other is a later rebuild whose coverage came back complete. **No ingest path clears it**, so without this leg the flag can stay set indefinitely while the Codex quota surfaces render as empty or stale with no cause stated anywhere. The leg reads the flag and never reconciles it, and it adds no write of its own. It reaches `stats.db` through the established #386 guarded read-only opener, which also resumes any quarantine already pending from an earlier failure. Current rollback-journal reads create no WAL/SHM sidecars. A pre-1009 index, an absent `stats.db`, or an unreadable one all report "not applicable".

### Data
- `data.latest_snapshot_age` — WARN at 5min-1h, FAIL >1h or never.
- `data.statusline_pipeline` — passive evidence for the statusline candidate
  pipeline: timer-transport age, selected-usage age, active candidate count,
  selected-control/database fingerprint agreement, and independent 5h/7d
  authoritative recovery state. It WARNs when an authoritative repair is
  needed, selected control no longer agrees with the database, or a recently
  active timer has not produced selected usage for five minutes. A stale or
  absent timer is informational — Claude may simply be closed — and doctor
  never creates, prunes, repairs, or otherwise changes pipeline files.
- `data.cache_sync_state` — WARN when the cache is empty despite JSONL files, or last entry > 24h old.
- `data.codex_cache` — same shape for `codex_session_entries`; OK with summary "none" when no Codex sessions exist.
- `data.codex_prune_safety` — WARN when a whole-tree Codex sync refused to
  treat a missing, empty, unrecognizable, or unreadable configured root as
  evidence that retained rollout files were deleted. The cache and transcript
  rows remain intact. Details contain only reason/count fields and affected
  store names, never configured paths or provider identifiers. Verify that
  every `$CODEX_HOME` root is mounted and contains rollout JSONL, then run
  `cctally cache-sync --source codex`; a recognized clean walk clears the
  warning.
- `data.codex_replay` — WARN when a byte-zero Codex transcript replay is
  *stalled* rather than merely pending. Codex transcript ingest defers on the
  cache-side replay marker (running ahead of the replayed thread rows would
  stamp a permanent `(unassigned)` project), so while that marker stands no
  Codex transcript is ingested at all. A marker that is only pending reports OK
  — it clears on the next **unbudgeted** Codex sync, which is `cctally
  cache-sync --source codex`, the dashboard, the TUI, or any `cctally codex`
  command. Not the hook: a byte-zero replay is not sliceable across a
  wall-clock budget, so a budgeted tick declines it outright rather than
  committing the wipe and only part of the re-read. There are therefore two
  stall shapes, and the leg reports both:
  - **Blocked** — a whole-tree sync *ran* and still could not consume the
    marker, which is what a persistently torn `auth.json` (or a repeated
    per-file DB error) produces; `cache-sync` itself still exits 0. WARN.
    Remedy: check or refresh the Codex login, then run `cctally cache-sync
    --source codex`.
  - **Deferred** — the hook declined the replay. It hands the unbudgeted drain
    to a background worker, so a *recent* deferral is the ordinary self-healing
    state and reports OK, naming that worker. It becomes a WARN once the
    deferral has stood for over an hour, which means the hand-off is not
    landing — on an install that only ever runs the hook, that is the one
    signal that all Codex ingest is frozen. Remedy: `cctally cache-sync
    --source codex`. A `blocked` record still outranks a `deferred` one.

  Details carry the pending flag, the blocked timestamp, the failed/deferred
  file counts, and `deferred_since` / `deferred_at` — never paths.
- `data.codex_ingest_backlog` — WARN when the Codex hook's *budgeted* ingest has been behind for over an hour. The hook's ingest leg has a wall-clock ceiling (`codex.hook.ingest_budget_seconds`, default 5), so it can legitimately leave rollouts unread for the next tick — that is the mechanism working, and a fresh backlog reports OK with a "draining" summary. The WARN fires only once the backlog has stayed non-zero *continuously* past an hour, which is the shape of a store whose per-tick growth outruns its budget. Remedy: run `cctally cache-sync --source codex`, which is unbudgeted and drains it. Details carry remaining files, remaining bytes, and the timestamp the backlog first appeared — never paths. A WARN alone does not change `doctor`'s exit code.
- `data.codex_project_metadata` — an all-history, root-qualified partition of
  retained Codex accounting rows. WARN when rows lack a conversation key or a
  same-root conversation-thread join; rebuild with `cctally cache-sync --source
  codex --rebuild`. FAIL when the read-only health query cannot run. Details
  contain counts only, never source paths or identifiers.
- `data.codex_quota` — physical local-rollout quota freshness per qualified
  Codex window. No Codex corpus is OK/not applicable; Codex files with no
  safely interpreted quota, or any applicable `future`, `stale`, or
  `unavailable` window, are WARN. Details include the sorted `windows` array,
  the latest local capture, aggregate worst freshness, and its responsible
  identity. This is not an OAuth or provider-live check; run a local
  `cctally cache-sync --source codex` (or trigger trusted Codex activity) to
  reread rollout data.
- `data.codex_quota_verification` — WARN when the detached Codex quota verification worker is not landing. Every whole-history projection pass now runs off the blocking hook path, so on an install driven only by the Codex hook all of it depends on the `_codex-quota-verify` worker — and on the routes where the hook does no projection work of its own (a rebuilt statistics index, a classification change, a reset change log), a worker that never succeeds leaves the Codex projection *missing* rather than merely stale. `data.codex_quota` cannot see that: it reports the freshness of the local rollout observations, which stay perfectly fresh while the projection derived from them is absent. The leg reads the worker's own outcomes from `hook-tick.log` (the worker's streams go nowhere else) and WARNs only on failures with no completed pass in 24 hours — one failed hand-off is ordinary and self-heals on the next throttle window. Silence is OK: an install with no Codex hooks never hands off, and every non-hook caller runs the pass inline. Remedy: `cctally cache-sync --source codex`. Details carry the 24-hour completed / errored / failed-spawn counts and the last completed pass — never paths.
- `data.parse_health` — WARN when the rolling ingest parse-health record (per vendor, kept in `cache_meta`) shows a malformed or drift-skipped JSONL line within the trailing 7 days — a signal that a Claude Code / Codex session-format change may be silently affecting your numbers; the summary carries the counts and the dominant skip reason. OK otherwise: absent record (pre-first-sync), all-zero counters, or a *stale* anomaly older than 7 days (surfaced as historical counts in the details so a one-off bad line doesn't nag forever). Remediation points at checking for a cctally update / filing an issue; `cctally cache-sync --rebuild` re-baselines the counters.
- `data.conversation_sessions_rollup` — WARN when the conversation-viewer browse-rail rollup (`conversation_sessions`) has drifted from its source — its row count differs from `COUNT(DISTINCT session_id)` over `conversation_messages` — **and only in a quiescent transcript store**. OK when the counts match, when either is unavailable (the table is absent on a pre-rollup store, or `conversations.db` cannot be read), or while a transcript sync/reingest/backfill is in progress. The in-progress signal is a non-blocking `conversations.db.lock` flock probe plus pending transcript `cache_meta` flags, so a transient mid-sync mismatch never WARNs. Informational only; the next conversation sync re-derives the rollup (`cctally cache-sync --rebuild` forces it). Read-only — the SQLite probe uses zero timeout and the lock probe never blocks.

### Accounts

- `accounts.identity` — WARN when the active Claude account identity cannot be read stably; cctally defers rather than guessing.
- `accounts.codex_identity` — WARN while a torn Codex `auth.json` is deferring Codex rollout ingest. The remedy checks or refreshes the login, then retries `cctally cache-sync --source codex`.
- `accounts.registry` — WARN when account registry rows are missing a provider; otherwise reports the real-account count by provider.
- `accounts.freshness` — informational account-attribution recency. It remains OK when no account has yet been observed.
- `accounts.attribution` — WARN when recent Claude usage is landing in `unattributed` despite a resolved active account, or while identity evidence is torn.
- `accounts.codex_reset_anchors` — WARN when any retained Codex quota observation lacks its canonical reset anchor. Raw-reset fallback keeps reads functional, but the row will not be healed by the already-completed migration; run `cctally cache-sync --source codex --rebuild`.

### Pricing
- `pricing.coverage` — WARN when your **recent (trailing 30-day)** session data contains a model cctally cannot price exactly: a Claude model that resolves to `$0` (`unpriced` — silent undercount) or a Codex model approximated via the `gpt-5` fallback (`fallback`). `details` lists each offending model ID + entry count + token volume; remediation points at [`pricing-check`](pricing-check.md) and the embedded pricing tables. OK when every observed model is priced, or when the cache is absent (no usage to assess). Read-only — the scan never creates the data dir on a fresh HOME. This is the offline counterpart to [`pricing-check`](pricing-check.md)'s coverage leg (which scans *all* history, not just the last 30 days), and it rolls into the dashboard health chip/modal for free.

### Safety
- `safety.dashboard_bind` — WARN when stored config is non-loopback OR (when invoked from inside the dashboard server) when the runtime bind is non-loopback.
- `safety.backup_sync` — WARN only when the resolved cctally data directory is confirmed inside an unexcluded file-level backup/sync root (Time Machine, iCloud Drive, or Dropbox). Remediation says to exclude the live data directory and use `cctally db backup --db stats` / `--db cache` for consistent SQLite snapshots. An absent destination, an explicit Time Machine exclusion, an unavailable tool, or a non-macOS platform is informational/OK; Doctor never guesses inclusion. The CLI uses bounded read-only `tmutil` probes. The dashboard's frequent shallow gather remains subprocess-free (static iCloud/Dropbox root detection still applies), preserving envelope liveness. Details contain only status/provider—never the local path.
- `safety.config_json_valid` — FAIL on `JSONDecodeError` (raw read; never `load_config()`).
- `safety.update_state` — FAIL on malformed JSON; WARN when absent or missing fields.
- `safety.update_suppress` — FAIL on malformed JSON.
- `safety.update_available` — WARN when latest > current.

### Telemetry

- `telemetry.state` — reports whether the anonymous install-count beat is enabled and why. It is informational and never creates an install id.

## JSON schema

Stable contract at `schema_version: 1`. Top-level fields:

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-13T14:22:31Z",
  "cctally_version": "1.6.3",
  "overall": { "severity": "warn", "counts": {"ok": 14, "warn": 1, "fail": 0} },
  "categories": [
    {
      "id": "install", "title": "Install", "severity": "ok",
      "checks": [
        { "id": "install.symlinks", "title": "Symlinks",
          "severity": "ok", "summary": "9/9 available",
          "details": { "present": 9, "total": 9, "missing": [] } }
      ]
    }
  ]
}
```

Stable: top-level shape, severity enum values, all check `id` strings,
`remediation` semantics (present iff severity != ok). Consumers MUST
tolerate unknown keys.

Unstable: `details` block per check — shape varies, keys may be added
or renamed across versions.

## Dashboard

The dashboard exposes the same diagnostic via:
- **Header chip** — aggregate-health pill (OK / WARN N / FAIL N) beside the existing freshness chip. Click to open the modal.
- **Modal** — full report with refresh button. Opened by clicking the chip or pressing `d`.
- **`GET /api/doctor`** — returns the same JSON the CLI emits.
- **SSE envelope** — every snapshot carries `doctor: { severity, counts, generated_at, fingerprint }` (aggregate only, ~120 bytes).

## See also

- [`setup`](setup.md) — install / hook management
- [`db status`](db.md) — migration inventory
- [`refresh-usage`](refresh-usage.md) — force-fetch OAuth usage
- [`cache-sync`](cache-sync.md) — rebuild the session-entry cache
- [`codex-quota`](codex-quota.md) — local-rollout quota semantics and recovery
- [`update`](update.md) — upgrade cctally
