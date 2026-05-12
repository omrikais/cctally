# `cctally doctor`

Read-only diagnostic. Answers the question: "why is my cctally data
stale or broken?" by running every passive check across install,
hooks, OAuth, database, data freshness, and safety config, then
emitting a severity-ranked report.

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

Six categories, ~18 checks. Each check has a stable `id` (used as the
JSON key), a one-line summary, and a remediation hint shown when
severity != `OK`.

### Install
- `install.symlinks` — WARN when any cctally-* symlink is missing or wrong.
- `install.path` — WARN when `~/.local/bin` is not on `$PATH`.
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
- `db.migrations.applied` — WARN on `skipped` rows; FAIL on `failed` rows.
- `db.migrations.pending` — WARN when any migration is pending.

### Data
- `data.latest_snapshot_age` — WARN at 5min-1h, FAIL >1h or never.
- `data.cache_sync_state` — WARN when the cache is empty despite JSONL files, or last entry > 24h old.
- `data.codex_cache` — same shape for `codex_session_entries`; OK with summary "none" when no Codex sessions exist.

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
          "severity": "ok", "summary": "9/9 present",
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
