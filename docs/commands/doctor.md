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

Seven categories. Each check has a stable `id` (used as the
JSON key), a one-line summary, and a remediation hint shown when
severity != `OK`.

### Install
- `install.symlinks` — WARN when any cctally-* command is unavailable. Reports "N/M available", counting `available = ok + stale`. PATH-aware: a command is counted available when its `~/.local/bin/` symlink is present, **or** when the command is reachable on `$PATH` via another install channel (e.g. a Homebrew `<prefix>/bin/` install), so it no longer false-warns purely because `~/.local/bin/` lacks the link. A leftover link to an old Homebrew keg (`<prefix>/Cellar/cctally/`) or the npm shim, whose command is still reachable elsewhere, is reported as a cleanable **`stale`** state (counted available, listed in the new `--json` `details.stale` array) rather than a generic failure — the summary appends "N stale link(s) to clean" and the remediation is `Run cctally setup to clean stale links`. A wrong-target / dangling / non-symlink slot still counts as missing (`wrong`). One pinned-only-path case is special-cased: when cctally is reachable **only** through a legacy `~/.local/bin/` link to a keg (so `cctally setup` deliberately won't remove the only working copy), the remediation switches to a PATH-fix hint ("Put `<prefix>/bin` on your PATH (e.g. `eval "$(brew shellenv)"`), then run `cctally setup` to remove the legacy link"). The `--json` `details` keys `present` / `total` / `missing` are unchanged (`missing` spans `wrong + missing`); `details.stale` is additive.
- `install.path` — availability-aware: OK whenever cctally is reachable on `$PATH` via **any** channel (Homebrew `<prefix>/bin/`, an npm prefix, or source `~/.local/bin`), summary `cctally reachable on $PATH`. WARN (`cctally not reachable on $PATH`) only when no channel makes it reachable; the remediation is channel-aware — a Homebrew keg is pointed at `eval "$(brew shellenv)"` (it owns no `~/.local/bin` symlinks per the #119 policy), while source / npm installs get the `export PATH="$HOME/.local/bin:$PATH"` + `cctally setup` fix.
- `install.legacy_snippet` — WARN when an old status-line snippet is detected.
- `install.legacy_bespoke_hooks` — WARN when the legacy hand-installed hooks are present.

### Hooks
- `hooks.installed` — WARN when any of `PostToolBatch`/`Stop`/`SubagentStop` entries are missing.
- `hooks.recent_activity_24h` — WARN when no hook has fired in 24h, or error/fire ratio ≥ 0.5.
- `hooks.last_fire_age` — WARN when the last fire was >1h ago or never.

### Auth
- `oauth.token_present` — FAIL when the OAuth token file is missing.

### Database
- `db.stats.file` — WARN when stats.db is absent (fresh install); FAIL when present but cannot open.
- `db.cache.file` — WARN when cache.db is absent; FAIL when present but cannot open.
- `db.integrity` — runs `PRAGMA quick_check(1)` on each database. FAIL when **stats.db** (the non-re-derivable DB) reports corruption; remediation backs up the file first and points at `sqlite3 <path> ".recover"` and filing a bug — it never suggests deleting the file. WARN when only **cache.db** is corrupt (re-derivable — `cctally cache-sync --rebuild`). OK when both report `ok`. This check runs **only from the CLI** (`cctally doctor` gathers with a `deep=True` flag); the dashboard health modal, whose gather runs on every rebuild, skips it because `quick_check` on a large cache.db costs seconds — there it shows "not checked (fast gather — run `cctally doctor`)".
- `db.version_ahead` — flags a DB whose `user_version` exceeds this binary's migration-registry head (a newer/unreleased cctally touched the data dir; issue #145). FAIL when **stats.db** is ahead — it bricks every stats-opening command and is not re-derivable; remediation: `cctally db recover --db stats --yes` (or restore from backup). WARN when only **cache.db** is ahead — it auto-heals on the next open (cache is re-derivable); remediation: it heals automatically, or run `cctally db recover --db cache`. OK ("none ahead") otherwise. `doctor` reads the raw `user_version` (no migration dispatcher), so it can report version-ahead without itself healing or bricking.
- `db.migrations.applied` — WARN on `skipped` rows; FAIL on `failed` rows.
- `db.migrations.pending` — WARN when any migration is pending.
- `db.lock_state` — informational (always OK). A non-blocking flock probe reports whether either sync lock file (`cache.db.lock` / `cache.db.codex.lock`) is currently held; a held lock usually just means an active sync or dashboard is running, so it never WARNs. The summary notes that a hold persisting across repeated `doctor` runs may indicate a wedged process. Read-only — the probe never creates the data dir or the lock files (it opens existing files read-only).

### Data
- `data.latest_snapshot_age` — WARN at 5min-1h, FAIL >1h or never.
- `data.cache_sync_state` — WARN when the cache is empty despite JSONL files, or last entry > 24h old.
- `data.codex_cache` — same shape for `codex_session_entries`; OK with summary "none" when no Codex sessions exist.
- `data.parse_health` — WARN when the rolling ingest parse-health record (per vendor, kept in `cache_meta`) shows a malformed or drift-skipped JSONL line within the trailing 7 days — a signal that a Claude Code / Codex session-format change may be silently affecting your numbers; the summary carries the counts and the dominant skip reason. OK otherwise: absent record (pre-first-sync), all-zero counters, or a *stale* anomaly older than 7 days (surfaced as historical counts in the details so a one-off bad line doesn't nag forever). Remediation points at checking for a cctally update / filing an issue; `cctally cache-sync --rebuild` re-baselines the counters.
- `data.conversation_sessions_rollup` — WARN when the conversation-viewer browse-rail rollup (`conversation_sessions`) has drifted from its source — its row count differs from `COUNT(DISTINCT session_id)` over `conversation_messages` — **and only in a quiescent cache**. OK when the counts match, when either is unavailable (the table is absent on a pre-rollup cache, or cache.db can't be read), or while a sync/reingest/backfill is in progress. The in-progress signal is a non-blocking `cache.db.lock` flock probe (a writer mid-walk holds it) plus the presence of any pending reingest/split/backfill `cache_meta` flag — so a transient mid-sync mismatch (the rollup is recomputed *after* `conversation_messages` commits per file) never WARNs. Informational only; the next full sync re-derives the rollup (`cctally cache-sync --rebuild` forces it). Read-only — the probe never blocks on the lock.

### Pricing
- `pricing.coverage` — WARN when your **recent (trailing 30-day)** session data contains a model cctally cannot price exactly: a Claude model that resolves to `$0` (`unpriced` — silent undercount) or a Codex model approximated via the `gpt-5` fallback (`fallback`). `details` lists each offending model ID + entry count + token volume; remediation points at [`pricing-check`](pricing-check.md) and the embedded pricing tables. OK when every observed model is priced, or when the cache is absent (no usage to assess). Read-only — the scan never creates the data dir on a fresh HOME. This is the offline counterpart to [`pricing-check`](pricing-check.md)'s coverage leg (which scans *all* history, not just the last 30 days), and it rolls into the dashboard health chip/modal for free.

### Safety
- `safety.dashboard_bind` — WARN when stored config is non-loopback OR (when invoked from inside the dashboard server) when the runtime bind is non-loopback.
- `safety.config_json_valid` — FAIL on `JSONDecodeError` (raw read; never `load_config()`).
- `safety.update_state` — FAIL on malformed JSON; WARN when absent or missing fields.
- `safety.update_suppress` — FAIL on malformed JSON.
- `safety.update_available` — WARN when latest > current.

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
- [`update`](update.md) — upgrade cctally
