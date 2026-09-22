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
- `hooks.codex_installed` — root-qualified Codex hook state, read from
  Codex's own `[hooks.state]` table in `config.toml`. With no detected Codex
  root it is OK/not applicable. Otherwise it takes the **worst** severity
  across every root, so a healthy sibling never masks a bad one: **FAIL** on
  `installed_disabled` (the operator turned the handler off in Codex
  `/hooks`) and on `installed_untrusted` (no usable trust record exists);
  WARN on `installed_unverified` (the handler changed after the last
  recorded trust decision), `installed_trust_unobservable` (cctally cannot
  read the recorded state), `absent`, `malformed` and `feature_disabled`;
  OK only when every root is `installed_enabled`. Each state carries its own
  remediation. The summary line reads `<enabled>/<roots> root(s) enabled`,
  and appends `, <installed> installed` whenever more roots are installed
  than are enabled — an `installed_untrusted` or `installed_unverified`
  handler is installed and may still be firing, which the ratio alone would
  hide from a reader who does not open the details block. Details include
  sorted `states: [{source_root_key, state}]`,
  root / installed / enabled counts, `requires_review`, `trust_state`,
  `worst_state` and `responsible_root_key`. `trust_state` is one of
  `not-applicable`, `review-required`, `unobservable`, `enabled`, `partial`
  or `not-installed`. The full state vocabulary and its ordered
  classification live in [setup.md](setup.md#hook-state-vocabulary).
- `hooks.codex_recent_activity` — root-qualified success/error activity from
  the last 24 hours, for **enabled** Codex handlers only, read from the
  lifecycle log. States are `recent` (OK) and `stale` / `never` (**WARN**);
  the check reports the worst state across the enabled roots, so one silent
  root is never masked by a firing sibling. Details carry `activity_state`,
  `last_tick_at`, `age_seconds`, `success_count_24h`, `error_count_24h`,
  `responsible_root_key` and a sorted `roots` array — never a session path or
  conversation payload. With no enabled Codex root the check is OK and
  `activity_state` takes a fourth value, `not-applicable`, which the per-root
  `roots` array never carries.
- `hooks.codex_liveness_7d` — the same question over a seven-day window and
  **different evidence**: the per-root success markers, which survive the
  rotation of the lifecycle log the 24-hour check reads. For each enabled
  root it reduces over `<root-key>.last-success` and
  `<root-key>.<account-key>.last-success` and takes the newest readable
  timestamp, so a dormant historical account never fails a root that is
  firing under a current one — which also means it is not proof of
  per-account coverage. Account suffixes are never exposed. States are
  `recent` (OK), `unavailable` (WARN, the markers could not be read), and
  `stale` / `never` (**FAIL**). Details carry `liveness_state`,
  `window_seconds: 604800`, `last_success_at`, `age_seconds`,
  `marker_count`, `responsible_root_key` and a sorted `roots` array. With no
  enabled Codex root the check is OK and `liveness_state` takes a fifth value,
  `not-applicable`, which the per-root `roots` array never carries.

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
- `db.conversations_wal_size` — the same backstop for the transcript store: WARN when `conversations.db-wal` exceeds 256 MiB, with remediation `cctally db checkpoint --db conversations`. It shares the cache leg's threshold because `conversations.db` carries the same 128 MiB `journal_size_limit`, so the same "this only fires when the containment machinery has genuinely failed" reasoning applies. The exact byte count appears only in the unstable `details` block, so a below-threshold count that drifts between runs does not move the doctor fingerprint; only an OK↔WARN crossing does. The check reads the size of the `conversations.db-wal` sidecar independently of whether `conversations.db` itself is present, exactly as the `cache.db` leg does. An oversized sidecar with no store beside it therefore still WARNs, and the remediation cannot clear it: `cctally db checkpoint --db conversations` reports that no database file is present, exits 0, and leaves the orphaned sidecar in place. Delete that file by hand if you meet this state; a WAL without its database holds no recoverable data.
- `db.reclaimable` — WARN when at least 25% of `cache.db` pages are on SQLite's freelist, meaning a substantial part of the file can be returned to the filesystem. Remediation is `cctally db vacuum --db cache`. The probe reads `PRAGMA page_count` and `PRAGMA freelist_count` only; it never vacuums or otherwise mutates the database. An absent or unreadable cache degrades to OK, and the raw counts plus ratio are available in the unstable `details` block.
- `db.retained_artifacts` — the corruption evidence cctally has retained, measured against `storage.artifact_retention` (#496). Reports retained, reclaimable and protected bytes, the free disk, and any pending reclamation that cannot finish. OK when the corpus is inside its policy. WARN when reclamation is due and would satisfy the policy (remediation `cctally db prune`), when the metadata walk stopped at its entry cap and the figures cover only part of the corpus, when the scan could not run at all — the reason is named, because a silent OK there reads exactly like a healthy install with nothing retained — or when a reclaim plan has carried a fail-closed entry for more than 24 hours; that last one names the plan id and the member to inspect, because no reclamation pass can decide it and the file must be removed by hand. FAIL when protected evidence holds the corpus over a bound, or when the policy block is malformed, in which case automatic reclamation is switched off until it is fixed. Both the FAIL and the WARN summary name the bound at issue by its own configured value — the age bound, the per-family count, the size budget or the free-disk floor — and every one of them when more than one applies. The last surviving example of each distinct damage shape is reported as information and never as a failure: it is retention the operator asked for through `max_shape_examples`, and treating it as a problem would produce a FAIL no action can clear. The scan is read-only, takes no lock, and is bounded to two directory levels and 5000 entries. It runs **only from the CLI** (`deep=True`), like `journal.integrity` and `journal.conflicts`; the dashboard's and TUI's per-rebuild gather shows "not scanned" and still reports a malformed policy and a stuck reclaim record, both of which cost one file read and one directory listing.
- `db.conversations_reclaimable` — applies the same read-only 25% freelist threshold to `conversations.db`, with remediation `cctally db vacuum --db conversations`. The transcript-store probe uses a zero-timeout read-only connection, so a large reingest or maintenance lock cannot stall `doctor`; a locked, absent, or unreadable transcript store degrades to OK with unavailable counts.
  - The same check also carries the #780 reclaim backlog, and it contributes nothing on a store that has never fallen behind — no `details` key and no severity change — so an install in the ordinary state renders exactly what it rendered before. When `conversation_retention_reclaim_pending` exists in the transcript store's `cache_meta`, `details` gains `reclaim_pending`, `reclaim_backlog_bytes` and `reclaim_ceiling_bytes`. A backlog at or over `RECLAIM_CEILING_BYTES` is a **FAIL** and transcript rebuilds are refused with `deferred_reason="reclaim_backlog_over_ceiling"`, because a rebuild adds a staged generation of churn to a store that is already failing to return the space it freed; a backlog at or over `RECLAIM_ESCALATION_BYTES` is a WARN and reclaim continues on every cycle instead of waiting for the daily throttle. Both constants are defined in `bin/_lib_conversation_retention.py` and the check reports the ceiling it applied in `details.reclaim_ceiling_bytes`, so read the value there rather than from this page. Both states clear themselves once a pass measures the space returned — completion is physical, so a zero freelist alone does not clear the record while the bytes are still in the WAL.

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
  retained Codex accounting rows. WARN when rows lack a conversation key, lack a
  same-root conversation-thread join, or carry project metadata the store cannot
  decode; rebuild with `cctally cache-sync --source codex --rebuild`. FAIL when
  the read-only health query cannot run. Details contain counts only, never
  source paths or identifiers.

  The partition reports three reasons, and `incomplete_rows` is their sum:
  `missing_conversation_key_rows`, `missing_thread_join_rows` and
  `undecodable_metadata_rows` (#845). The third counts accounting rows whose
  project attribution would have to read a `codex_conversation_threads.cwd` or
  `.git_json` value whose stored bytes are not valid UTF-8. It is a new
  additive key in the `--json` output; a row is counted under exactly one
  reason, so the three never double-count the same row. The count is
  all-history here, deliberately unlike the dashboard's two bounded counts, and
  the rebuild is a real remedy because it re-derives both columns from the
  rollout JSON.
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
- `pricing.conversation_rollup_writer` — WARN when a process holding an older embedded pricing table was refused a write to the conversation-sessions rollup (#705). The guard protects **two stores**: `sync_cache` runs it on the `cache.db` connection and `sync_claude_conversations` runs it on the `conversations.db` connection, so each file carries its own `conversation_sessions_pricing_fp` and its own refusal record. The check reads both and reports when EITHER carries one — a refusal in either file has the same diagnosis and the same remedy, so it is not a false alarm where that store's own rollup holds no rows, and a refusal latched by a process that never opens the conversation store (`hook-tick`, `statusline`, `daily`, `report`) exists only in `cache.db`. `details` names the store in `store` (`conversations.db` or `cache.db`); both stores are classified for an unorderable fingerprint before either record is reported, and within each pass `conversations.db` is reported first. A record is reported only while its `active` field is true. The record is a durable tombstone rather than a latch deleted on recovery, so a store that diverged and then converged keeps the evidence with `active: false` and stops warning; a record written before that field existed carries no `active` key and is read as active, because that older record was written only on refusal and deleted on convergence. An active record proves that such a write was refused; it does not prove that such a process is running now, so the summary is past tense. The rollup is non-authoritative as a **consequence** of a refusal arming `conversation_sessions_backfill_pending` — which every refusal does except the `cache-sync --rebuild` pre-clear, whose rollup is intact — not as an inference from this record. `details` names `store`, `process_snapshot_date`, `store_snapshot_date` and `first_refused_at_utc`. A record that exists but cannot be parsed still WARNs, with a distinct summary saying the refusal record cannot be read and no dates in `details` — a record the guard wrote must never read as "no refusal" merely because its contents are unreadable. Remediation, verbatim as the command prints it: "Restart or upgrade the process that is writing to this store — most often a `cctally dashboard` left running across an upgrade. A process whose pricing table is not older than the store's clears this record the next time it recomputes the rollup: on its next tick when the refusal also armed the rollup backfill, and otherwise on the next sync that touches a session. While the backfill is armed the conversation list falls back to live aggregation, so every session stays visible, but cost sorting and project filtering are degraded, the browse filter's project list omits sessions the rollup has not indexed, and each row's cost is recomputed from the reading process's own pricing table. A refusal `cctally cache-sync --rebuild` takes before it clears anything arms no backfill, because the rollup it declined to touch is intact — that store keeps reading it." One state does NOT respond to that remedy and is reported separately, with its own summary ("the rollup's stored pricing fingerprint is not a date any version of cctally can compare") and `details` carrying `store` and `stored_fingerprint`, plus the date pair merged in when a refusal record has already been recorded and parses. cctally refuses a write it cannot order, and it cannot order a stored fingerprint that is not an ISO date — so on such a store every version refuses, `cache-sync --rebuild` included, and restarting or upgrading cctally reaches nothing. Only corruption or a fingerprint written in a format this version predates produces it, and its remediation names the step that does work, again verbatim: "The pricing fingerprint recorded in the rollup (`conversation_sessions_pricing_fp` in the `cache_meta` table of the store this check's `store` detail names — `conversations.db` or `cache.db`) is not an ISO date, so cctally cannot tell whether its own pricing table is older or newer and refuses the write. Every version refuses it, including `cctally cache-sync --rebuild`, so restarting or upgrading cctally does not clear this one. Delete that single `cache_meta` row with `sqlite3`, then run `cctally cache-sync --rebuild` to re-derive the rollup and record a fresh fingerprint. Until then the conversation list falls back to live aggregation, so every session stays visible, but cost sorting and project filtering are degraded." Two further states are reported separately, and both arrived after that pair. The first is a fingerprint READ that did not happen: `sqlite3` raised on the `cache_meta` SELECT, so the store was not classified at all. That is not the same as a store with nothing recorded — the read used to collapse to absence, and doctor then reported OK over a store it had no evidence about. A store whose `sqlite_master` probe positively shows no `cache_meta` table is still absent, because a store with nowhere to record a fingerprint determinately recorded none; every other failure, including a probe that fails too, is a failed read. Its summary is "the rollup's stored pricing fingerprint could not be read, so this store's materialized cost is unverified", its `details` carry `store` and `read_error_kind` (the coarse `operational_error`, never the driver's message), and it is reported above the recorded refusals because a store doctor could not read has no verdict at all, and below the unparseable value because that one is a diagnosed permanent state with a known step out of it. Its remediation is store-parameterised — the `cctally db checkpoint --db` value follows this check's `store` detail, so `--db cache` when the affected store is `cache.db` — and reads, verbatim for `conversations.db`: "cctally could not read the pricing fingerprint recorded in the rollup (`conversation_sessions_pricing_fp` in the `cache_meta` table of `conversations.db`), so it cannot say whether that store's materialized cost is protected. This is usually a store held by another process: check for a `cctally dashboard` or a long ingest running against this installation, and run `cctally db checkpoint --db conversations` if the WAL has grown. Run `cctally doctor` again once the store is quiet. While the read keeps failing every rollup writer refuses rather than assuming the store had nothing to protect, so cost sorting and project filtering may be degraded, but no session is hidden." The second is a refusal that a failed read latched, reported once the store reads cleanly again. A refusal records the store date its own read produced, and a failed read produces none, so a null `store_snapshot_date` identifies this case exactly: an absent fingerprint is authorized and latches no record, while every other refusal names a value. Its summary is "a rollup write was refused because this store's pricing fingerprint could not be read at the time", its `details` carry `store`, `process_snapshot_date`, a null `store_snapshot_date` and `first_refused_at_utc`, and its remediation is store-parameterised the same way. Verbatim for `conversations.db`: "A rollup write to `conversations.db` was refused because cctally could not read that store's recorded pricing fingerprint (`conversation_sessions_pricing_fp` in its `cache_meta` table) at the time, so it had no evidence about what the store's materialized cost needed protecting from. The store reads cleanly now, so there is no unparseable value to delete and no version difference to resolve — restarting or upgrading the writing process is not the step here. The next authorized sync that recomputes the rollup clears this record: on its next tick when the refusal also armed the rollup backfill, and otherwise on the next sync that touches a session. If it keeps coming back, the store is intermittently unreadable — check for another process holding it, and run `cctally db checkpoint --db conversations` if the WAL has grown. While the backfill is armed the conversation list falls back to live aggregation, so every session stays visible, but cost sorting and project filtering are degraded." Like `pricing.coverage`, this check can never FAIL and therefore never affects `doctor`'s exit code, on either branch: a refusal means the safety mechanism worked and ingestion continues, so the store is degraded but usable.

### Quota
Neither check can FAIL, so neither affects `doctor`'s exit code — the same posture `pricing.coverage` and `data.parse_health` take.

- `quota.meter_drift` — OK-with-detail, stating whether Anthropic's metering rate changed. The report is **account-blind**: it reads the merged calibration bucket and takes no account argument, exactly as the [status line](statusline.md)'s `Δrate` marker does, so run [`quota`](quota.md) when you need to know whose rate moved. The predicate is derived rather than stored: the active open regime has a confirmed predecessor, and the marker shows for the whole of that successor regime. `details` carries `active`, `assessed`, `calibration_status`, the effective instant, and the previous and new weighted units per meter point. `calibration_status` is the active successor regime's own status, and the remediation branches on it: an `ok` successor points you at the fitted budget, and any other status says that no fitted budget is available and names the status, because a successor that is not prediction-ready has none to report. A rate change is the provider's behaviour rather than a cctally malfunction, so this check reports it and never warns. An install with no fitted calibration reports `not assessed`, never `no change detected` — that covers an install which has never run [`quota`](quota.md), and equally one whose runs all withheld their fit for a reason that also refuses persistence, because absence of a fitted regime is absence of evidence rather than evidence that nothing changed, and [`quota`](quota.md) forbids conflating the two. A run withheld for a **forward-looking** reason does persist its regimes, so this check reports its transition rather than `not assessed`; [`quota`](quota.md) states which reasons those are. The negative finding is therefore gated on `assessed`, while an active transition is not, since a transition is itself proof that the regime pair was examined and gating it would risk suppressing a real change. The effective day in the summary is the **UTC** calendar day and says so, because this report carries no display-timezone plumbing and every other datetime it emits is UTC.
  - **Scope limitation, stated rather than left silent:** this check reads the MERGED calibration bucket and takes no account argument. On a decorated multi-account install it therefore reports the merged regime's transition, which may be `no change detected` while a per-account regime did change, or a stale merged transition while a per-account one did not. Run [`quota`](quota.md) with `--account` for the per-account answer. At a single real account the R8 gate means nothing decorates and the merged bucket is the account bucket, so this affects only genuinely multi-account installs.
  - **The same limitation applies to the other two surfaces that render this predicate**, and it is recorded here once for all three: the [status line](statusline.md)'s `Δrate` marker and the [dashboard](dashboard.md) Forecast panel's `Δ rate` chip both resolve the merged bucket too. All three read one glyph off a regime pair with no account argument to give it, which is why the limitation is shared rather than specific to `doctor`.
- `quota.calibration` — WARN when the stored calibration cannot be used because it is malformed, unreadable, from a newer cctally, or was fitted under other constants or by an earlier algorithm. **Stale here means a fingerprint or algorithm-revision mismatch, not calendar age**: a calibration fitted months ago under the current constants is healthy, and one fitted yesterday under other constants is not. An absent calibration, or one whose successor fit is not yet predictive, is OK-with-detail. Remediation is to run [`quota`](quota.md), which refits it.

Both checks read the calibration through the non-mutating reader rather than through the loader `quota` itself uses. That loader renames a malformed or version-ahead file aside, and `doctor` is documented read-only, so calling it here would make `doctor` a writer. An already-quarantined file is reported simply as a missing primary; the checks do not scan quarantine sidecars.

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

## Implementation

The report is a pure kernel, `bin/_lib_doctor.py`: it takes an already-gathered state object and returns the check list, so every check is decided without touching the filesystem, the network or the clock. All of the I/O sits in one layer, `doctor_gather_state`, which is also what lets the dashboard render the same checks from the same state without running the command.

## See also

- [`setup`](setup.md) — install / hook management
- [`db status`](db.md) — migration inventory
- [`refresh-usage`](refresh-usage.md) — force-fetch OAuth usage
- [`cache-sync`](cache-sync.md) — rebuild the session-entry cache
- [`codex-quota`](codex-quota.md) — local-rollout quota semantics and recovery
- [`update`](update.md) — upgrade cctally
