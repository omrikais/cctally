# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Local CLI tooling to track Claude subscription usage percentage against weekly USD cost computed in-process from session JSONL data. Computes **$ per 1% weekly usage** for trend analysis. Several subcommands are drop-in offline replacements for `ccusage` / `ccusage-codex` flows. All code lives in `bin/` as standalone scripts (no package manager, no build step for the CLI; `dashboard/web/` is the one Vite/React island, with its built output committed to `dashboard/static/`).

## Repository Layout

- `bin/cctally` — Single-file Python 3 CLI (the main program). Contains all logic: SQLite storage, week boundary parsing, cost computation from JSONL session data, usage recording from Claude Code status line, report generation, and percent milestone tracking.
- `bin/cctally-<cmd>` — Bash wrappers per subcommand (`dollar-per-percent`, `sync-week`, `forecast`, `project`, `refresh-usage`, `tui`, `dashboard`, `five-hour-blocks`, `five-hour-breakdown`).
- `bin/cctally-<cmd>-test` — Fixture-based validation harnesses (one per subcommand with goldens; see Validation & Testing).
- `bin/build-<cmd>-fixtures.py` — Regenerate deterministic SQLite fixtures under `tests/fixtures/<cmd>/`.
- `bin/_lib-fixture-harness.sh`, `bin/_fixture_builders.py` — Shared harness wrapper + builder helpers.
- `docs/commands/*.md` — Per-subcommand user-facing reference pages. `tests/fixtures/<cmd>/` — golden test fixtures.

Scripts are symlinked into `~/.local/bin/` for shell usage. Runtime data (SQLite DB, config, logs) lives in `~/.local/share/cctally/` — never committed.

## CLI Subcommands

`cctally <command>` — full flag/exit-code reference in `docs/commands/<command>.md`. Distinguishing notes only:

**Recording / refresh:**
- `record-usage` — writes 7d/5h usage from CC status line `rate_limits` into `weekly_usage_snapshots`.
- `refresh-usage` — force-fetch 7d/5h percent from Anthropic OAuth API; busts `/tmp/claude-statusline-usage-cache.json`.
- `hook-tick` — internal CC hook (hidden via `argparse.SUPPRESS`); reads stdin JSON, runs `sync_cache`, throttled OAuth refresh (default 30s).
- `setup` — install/uninstall cctally + additive `PostToolBatch`/`Stop`/`SubagentStop` hooks in `~/.claude/settings.json`.
- `sync-week` — computes weekly cost from JSONL into `weekly_cost_snapshots`.
- `cache-sync` — prime/rebuild `cache.db`. `--source {claude,codex,all}` (default `all`).

**Reporting:**
- `report` — joins usage + cost snapshots; $/1% trend table.
- `percent-breakdown` — per-percent cost milestones for a week (with 5h correlation).
- `forecast` — projects current-week % + daily $/% budgets vs. 100%/90% ceilings; range output (week-avg vs recent-24h); `LOW CONF` when thin.
- `project` — aggregate usage by project (git-root resolved), per-project `Used %`.
- `daily` / `monthly` — by date / calendar month (replace `ccusage daily`/`monthly`).
- `weekly` — by **subscription week** (anchored to `--resets-at`; extrapolates 7d cadence for pre-snapshot history).
- `session` — by Claude `sessionId`; merges resumed sessions across JSONL files.
- `diff` — compare two windows; overall + per-model + per-project + cache drift; smart noise filter (`|Δ$| < $0.10 AND |Δ%| < 1.0`).
- `range-cost` — USD cost for a time range.
- `cache-report` — cache hit/cost behavior; ⚠ anomaly on Net $ < 0 or Cache % drop ≥15pp vs. 14-day median.

**5-hour windows:**
- `blocks` — usage by 5h session blocks (replaces `ccusage blocks`); `~` prefix when heuristic-anchored.
- `five-hour-blocks` — API-anchored analytics view with rollup totals + 7d-drift; `⚡ ` prefix on crossed-reset rows; cap 50 rows w/o date filter; JSON `schemaVersion: 1`.
- `five-hour-breakdown` — per-percent milestones inside one 5h block; selector `--block-start <iso>` (naive=UTC, date-only rejected) or `--ago N`.

**Live UIs:**
- `dashboard` — local web dashboard (stdlib HTTP + SSE) on `:8789`, default bind `127.0.0.1` (loopback; opt in to LAN exposure via `--host 0.0.0.0` or `dashboard.bind = lan` config); six panels + per-panel modals (`1`–`6`); sessions filter `f` / search `/` n/N; settings `s` (localStorage).
- `tui` — live terminal dashboard; lazy-imports `rich`; refuses <80 cols (80–99 narrow-warn); two variants (2×2 grid / expressive hero).

**Codex (OpenAI) parity:**
- `codex-daily` / `codex-monthly` / `codex-weekly` / `codex-session` — drop-in for `ccusage-codex` from `~/.codex/sessions/*.jsonl`. `codex-session` sorts ascending; `--offline` is a no-op.

**Config:**
- `config get|set|unset` — persisted prefs. Allowed keys: `display.tz` (`local`/`utc`/IANA). Dashboard mirrors via `POST /api/settings`.

## Validation & Testing

Golden-file harnesses follow the `bin/cctally-<cmd>-test` pattern. Coverage: `forecast`, `project`, `diff`, `tui`, `blocks`, `session`, `weekly`, `cache-report`, `codex-{daily,monthly,session,weekly}`, `dashboard`, `hook-tick`, `setup`, `five-hour-blocks`, `5h-canonical`, `reconcile`. `bin/cctally-test-all` runs every harness in sequence.
```bash
bin/cctally-forecast-test   # rebuilds fixtures, diffs 3 output modes vs goldens
bin/cctally-project-test    # same, for `project` — uses CCTALLY_AS_OF env hook
bin/cctally-diff-test       # same, for `diff` — 10 fixture scenarios × 2 modes
```
Fixtures live under `tests/fixtures/<cmd>/`; `bin/build-<cmd>-fixtures.py` regenerate seeded SQLite DBs (one builder per command, plus `bin/_fixture_builders.py` for shared helpers and `bin/_lib-fixture-harness.sh` for the wrapper pattern). For ad-hoc validation:
```bash
python3 -m py_compile bin/cctally
cctally --help
cctally-dollar-per-percent --json
cctally cache-sync --rebuild
```

## Architecture Notes

**Data flow:** Claude Code status line → `record-usage` subcommand → SQLite `weekly_usage_snapshots` table (with 5-hour data). Separately, `sync-week` computes cost from JSONL session files → SQLite `weekly_cost_snapshots` table. `report` joins both tables per week window. When usage crosses a new integer percent, `record-usage` writes to `percent_milestones` (cumulative/marginal cost + 5-hour percent at crossing). `percent-breakdown` displays these milestones.

**Week boundary handling** is the most complex part. The `--resets-at` Unix epoch timestamp from the status line rate_limits is converted to a UTC datetime and normalized to the nearest hour boundary to handle Anthropic jitter. When no reset timestamp is available, falls back to config-based week-start day. `weekly` reuses this boundary data to rollup subscription weeks: it reads `weekly_usage_snapshots` rows directly for weeks where data exists, and extrapolates by 7-day multiples from the earliest known anchor only to fill the pre-snapshot history tail.

**Week matching** uses `WeekRef` keys: prefers exact `week_start_at` (ISO timestamp) match, falls back to `week_start_date` (date-only) for backward compatibility.

**Cost computation** is built-in: the script reads JSONL session files from `~/.claude/projects/` and applies embedded model pricing to compute USD costs. No external tools are needed.

**Session-entry cache:** JSONL-reading commands (`daily`, `monthly`, `blocks`, `range-cost`, `cache-report`, `sync-week`, `session`) go through a read-through delta cache at `~/.local/share/cctally/cache.db`. `sync_cache()` tail-ingests new bytes from each `*.jsonl` under `~/.claude/projects/` (keyed by file size + last byte offset) into `session_entries`; queries run via `iter_entries()`. Cost is computed at query time from `CLAUDE_MODEL_PRICING` — no cache invalidation needed for pricing edits. `fcntl.flock` on `cache.db.lock` serializes writers; losers read the existing cache. Cache is fully re-derivable (`rm cache.db` or `cache-sync --rebuild` always safe); `get_entries()` falls back to direct JSONL parse if the cache DB can't be opened. The `session_files` table (shared with delta-resume state; `session_id`/`project_path` nullable ALTER-added columns) is populated lazily by `sync_cache()` from `sessionId` + `cwd` (or decoded dirname), and powers `session`'s resume-merging via join on `source_path`.

**Codex session cache:** The `codex-daily`/`codex-monthly`/`codex-session` commands use parallel tables (`codex_session_entries`, `codex_session_files`) in the same `cache.db`. `sync_codex_cache()` walks `~/.codex/sessions/**/*.jsonl`, emits one row per `event_msg.token_count` event (using `last_token_usage` fields), and tracks per-file `(session_id, model)` in `codex_session_files.last_session_id` / `last_model` so delta resumes don't replay from byte 0. A separate `cache.db.codex.lock` file serializes Codex ingests independently of Claude ingests. Codex cost is computed at query time from `CODEX_MODEL_PRICING` (LiteLLM-sourced snapshot); pricing updates take effect on the next read with no invalidation. `cache-sync --source {claude,codex,all}` (default `all`) primes or rebuilds whichever half is asked for.

## Gotchas

### Pricing & schema
- **Model pricing is embedded.** `CLAUDE_MODEL_PRICING` near the top must be updated when Anthropic ships new models. Unrecognized models log a warning and contribute zero cost.
- **Schema migrations are inline.** New columns added in `open_db()` via check-column-then-ALTER. Follow the existing pattern.
- **`schema_migrations` meta-table tracks durable migration completion.** Gate is `SELECT 1 FROM schema_migrations WHERE name = ?`. Empty-row backfills (parent block with no `session_entries`) MUST still INSERT the marker; table-emptiness as sentinel is unsafe.
- **Always snap up by 1e-9 before `math.floor()` on a percent-like float.** `0.57 * 100` is `56.99999999999999`. Write `math.floor(pct + 1e-9)` everywhere an integer percent comes from fraction-times-100.
- **Reconcile invariants use 1e-9 USD tolerance**, not exact equality (float ULP drift). See `bin/cctally-reconcile-test`.

### Codex (OpenAI) parity
- **Codex pricing is embedded** (`CODEX_MODEL_PRICING`, LiteLLM-sourced). Unknown models fall back to `CODEX_LEGACY_FALLBACK_MODEL = "gpt-5"` with `isFallback: true`; one-shot stderr warning per unknown name.
- **Codex token semantics (LiteLLM convention):** `last_token_usage.input_tokens` INCLUDES `cached_input_tokens`; `output_tokens` INCLUDES `reasoning_output_tokens`. Cost = `(input - cached) * input_rate + cached * cache_read_rate + output * output_rate`. Reasoning is NOT added separately. Upstream's JSON `inputTokens` is inclusive of cached; rendered table shows non-cached (`input - cached`) — our renderers mirror this.
- **Intentional divergence from upstream on duplicate `token_count` events.** Older Codex rollouts re-emit identical `last_token_usage` (~2x overcount upstream). We dedup by tracking `info.total_token_usage.total_tokens` and only yielding when cumulative strictly advances (`_iter_codex_jsonl_entries_with_offsets`). ~50% lower than upstream on historical data; fresh sessions match byte-exactly. **Do not "fix" back to parity.**

### Cost / weekly / session
- **`weekly` ignores `weekly_cost_snapshots` for cost.** Cost is recomputed from `session_entries` / `CLAUDE_MODEL_PRICING` so pricing edits take effect immediately. For "cost as snapshotted," use `report`.
- **`session` merges resumed sessions.** A `sessionId` across multiple JSONL files (`--resume`) collapses into one row. `Directory` shows the most-recent project; `--json` `sourcePaths` preserves the file set.
- **`session_files` is populated lazily.** First run after deploy: entries may briefly lack `session_id`/`project_path`; aggregator falls back to filename UUID with one-shot stderr warning. Backfilled by subsequent `sync_cache()`.
- **`cache-report` anomaly baseline silent-skips when samples thin.** `cache_drop` needs ≥5 daily or ≥10 session rows in the trailing window. Below that, silently skipped — widen `--days` or check `--json | jq '.days[].anomaly.reasons'`.
- **`project` has hidden `CCTALLY_AS_OF` env hook** for fixture testing. Not in `--help` (deliberate). `bin/cctally-project-test` depends on it.

### TUI
- **`tui` lazy-imports `rich`** inside `cmd_tui` so other subcommands stay zero-dep. Missing `rich` → exit 1 with `TUI_RICH_MISSING_MSG`.
- **`_TUI_VALID_STYLE_NAMES` must stay in sync with the theme.** Every `{name}…{/}` style tag must appear in `_TUI_VALID_STYLE_NAMES` and `_tui_build_theme()`. Startup assertion catches drift.
- **TUI fixture tests use a hidden `--render-once --snapshot-module` dev path** (`argparse.SUPPRESS`ed). Snapshot modules under `tests/fixtures/tui/` set `last_sync_at=None` for deterministic "synced —" header.
- **TUI v2 modal/input lifecycle.** Modals re-render each tick from latest DataSnapshot (don't freeze). Sync thread runs while open. Modal & help overlay are mutually exclusive. In filter (`f`) or search (`/`) input mode, only `Esc`/`Enter`/`Ctrl-C` escape — even `q` is literal.
- **TUI v2 fixture loader uses `RUNTIME_OVERRIDES`.** Snapshot modules may export `RUNTIME_OVERRIDES: dict` applied to `RuntimeState` after construction; allow-list hard-coded in `_tui_render_once`. Dev-only.

### Fixtures & harnesses
- **Fixture SQLite DBs must `PRAGMA journal_mode=WAL` to match production.** All `bin/build-*-fixtures.py` set WAL; each `tests/fixtures/<cmd>/.gitignore` covers `*.db-wal`/`*.db-shm`.
- **Harness scratch-dir pattern.** Every `cctally-*-test` redirects builder output + SQLite writes to a per-run `mktemp -d` scratch dir (`HARNESS_FAKE_HOME_BASE`), keeping in-tree fixtures byte-stable. Codex wrappers point one level deeper (`<scratch>/codex-<sub>`) since the builder writes 4 sub-trees.
- **Use `TZ=Etc/UTC`, never `TZ=UTC`,** in tests/fixtures/harnesses. `_local_tz_name()` gates on IANA "/" — bare `TZ=UTC` falls back to host local zone, leaking non-determinism into goldens.

### Dashboard server
- **`dashboard` envelope emits lowercase verdict strings** (`"ok"`/`"cap"`/`"capped"`); JS maps to display labels via `dashboard/web/src/lib/verdict.ts`. Don't emit uppercase from Python.
- **Python 3.14 `http.client.HTTPResponse.read(n)` blocks until EOF on Content-Length-less SSE streams.** Tests against `/api/events` must read via `r.fp.read1(n)` with a deadline loop. See `tests/test_dashboard_api_events.py`.
- **`ThreadingHTTPServer` sets `daemon_threads = True`** so Ctrl-C is responsive. SSE handlers block up to 15s on `queue.Queue.get(timeout=15)`. Shutdown order: sync-thread, then `srv.shutdown()`, then `http_thread.join(timeout=2)`.
- **CSRF + LAN exposure.** `/api/sync`, `/api/settings`, `/api/alerts/test` use `_check_origin_csrf` (Origin host:port vs Host header, case-insensitive). Loopback aliasing (`localhost`/`127.0.0.1`/`::1`) and LAN bind (`0.0.0.0`) work without server self-knowledge. Default bind is `127.0.0.1`; LAN exposure is opt-in via `--host 0.0.0.0` or `dashboard.bind = lan`. **CSRF blocks browser cross-origin attacks but NOT direct `curl` from LAN peers — there's no auth on `/api/*`.** Reads leak usage; writes can burn OAuth (`/api/sync`), mutate config (`/api/settings`), or fire osascript popups (`/api/alerts/test`). Use LAN bind only on trusted networks.
- **`/api/sync` runs `_refresh_usage_inproc` THEN snapshot rebuild on chip click / `r` key.** This is the force-fetch path (busts statusline cache, bypasses throttle) — NOT `_hook_tick_oauth_refresh`. Periodic sync stays snapshot-only. 204 on clean, 200 + `{warnings:[{code}]}` otherwise (status enum: `ok | rate_limited | no_oauth_token | fetch_failed | parse_failed | record_failed`). Frontend silently ignores `rate_limited`.
- **`_run_sync_now` is split into `_run_sync_now_locked` + public wrapper to avoid self-deadlock** (`threading.Lock` is non-reentrant; `/api/sync` already holds `sync_lock`). Periodic thread uses the public wrapper.
- **`_discover_lan_ip` uses UDP-no-send to TEST-NET-1** (192.0.2.1:1) so `getsockname()` returns the kernel's chosen source IPv4 without sending a packet. Returns `None` on `OSError`. Honors `CCTALLY_TEST_LAN_IP` (`__SUPPRESS__` forces None) for fixture-stable banner goldens.
- **All dashboard URLs go through `_format_url(host, port)` which IPv6-brackets** (RFC 3986: `http://[::1]:8789/`). Already-bracketed hosts pass through.

### Dashboard client (React)
- **Dashboard v2 client is React + TypeScript.** Source `dashboard/web/`; built output committed to `dashboard/static/`. State via `useSyncExternalStore`-backed singleton in `store/store.ts`; SSE singleton boots in `main.tsx` (module-scoped — StrictMode can't double-boot). Keyboard precedence: `modal > sessions > global`. Vite 8 (Rolldown); dev `:5173` proxies `/api/*` + `/static/*` to Python `:8789`. The `/api` proxy uses `changeOrigin: true` AND a manual `proxyReq` Origin rewrite — both must match upstream for CSRF parity (`changeOrigin` only handles Host). After any source change, `cd dashboard/web && npm run build` and commit `dashboard/static/` in the same commit. See `dashboard/web/README.md`.
- **Dashboard build is byte-stable only on Node 24.11.x + lockfile-synced `node_modules`.** `.nvmrc` pins 24.11.1; `engines.node` strict `>=24.11.0 <25`. Always `nvm use` then `npm ci` before `npm run build`. If hashes differ with no source change, check `node_modules/.bin/vite --version` against the lockfile.
- **Dashboard `trend.weeks[]` is 8 rows; `trend.history[]` is 12 rows.** Panel sparkline reads `weeks[]`; modal reads `history[]`. Don't merge.
- **Dashboard `current_week.five_hour_block` binds to the latest snapshot's `five_hour_window_key`,** NOT highest `block_start_at`. Returns `null` when no matching `five_hour_blocks` row; React panel falls back to legacy single-big-number layout.
- **Session detail modal updates live on SSE ticks.** Subscribes to `snapshot.generated_at` and refetches `/api/session/:id` (stale-while-revalidate; do NOT re-introduce `setData(null)`). Refetch classification: `isInitialFetch = lastResolvedIdRef.current !== id || data == null` (the `|| data == null` clause handles tick-aborts). 404-grace: one keeps stale, two evict to "Session not found"; success clears the arm. Bound id captured into `resolvedIdRef`, only re-binds on `openSessionId` change or close→reopen.
- **`blocks` tilde marker means heuristic anchor.** No recorded `five_hour_resets_at` for that window; start was floored from the first CC entry's hour. `record-usage` populates the authoritative anchor.

### 5-hour windows
- **5h window key MUST go through `_canonical_5h_window_key`.** `rate_limits.5h.resets_at` arrives with seconds-level capture jitter; raw use as a key treats one physical window as multiple. Datetime callers: `_floor_to_ten_minutes`. Epoch-int callers: `_canonical_5h_window_key`. Both share `_FIVE_HOUR_JITTER_FLOOR_SECONDS = 600`. Don't derive a third key shape. Regression: `bin/cctally-5h-canonical-test`.
- **`TuiCurrentWeek.week_start_at` is NOT a valid `week_start_date` lookup key after a mid-week reset.** `_apply_midweek_reset_override` shifts `cw.week_start_at` to the reset instant, but `weekly_usage_snapshots`/`percent_milestones` stay keyed on the original subscription-week date. Re-resolve via `SELECT week_start_date FROM weekly_usage_snapshots ORDER BY captured_at_utc DESC, id DESC LIMIT 1` — NOT `cw.week_start_at.date().isoformat()`. Regression: `tests/fixtures/dashboard/reset-week/`.
- **`five_hour_blocks` is API-anchored only.** Per spec §3.2, no rollup row for heuristic 5h windows; `blocks` surfaces those via `~`. The `record-usage` bail-out gate enforces this.
- **`five_hour_milestones` is forward-only.** No backfill on first-ship. Mid-block upgraders see milestones from current floor onward; cost-at-moment-of-crossing isn't recoverable from snapshots. Symmetric with existing `percent_milestones`.
- **Close-older predicate uses `<`, not `!=`.** `maybe_update_five_hour_block` closes prior open blocks via `WHERE five_hour_window_key < ?`. Under `!=`, a late-completing older invocation could close the now-current block. `<` is safe under reordering (key is a 10-min-floored monotonic epoch).
- **Cross-reset flag is interval-based on both write paths** (live + backfill): `block_start_at <= effective_reset_at_utc <= last_observed_at_utc`, NOT keyed equality. `cmd_record_usage` passes `mid_week_reset_at` only when `INSERT OR IGNORE INTO week_reset_events` returns `cur.rowcount > 0` (filters duplicates).
- **`five_hour_blocks` totals are recomputed every tick from `session_entries`.** The four `total_*_tokens` and `total_cost_usd` columns are NOT incremental — recomputed by summing cache.db's entries over `[block_start_at, captured_at_utc]` on every `record-usage` (shared `_compute_block_totals`). Pricing edits take effect next tick.
- **FK on `five_hour_milestones.block_id` is documentation-only** (`PRAGMA foreign_keys` not enabled). Referential integrity from live-insert ordering + `UNIQUE(five_hour_window_key, percent_threshold)`.
- **`five_hour_block_models` / `five_hour_block_projects` are recompute-every-tick rollup-children.** UNIQUE on `(five_hour_window_key, model | project_path)`. Live writes use replace-all (`DELETE WHERE five_hour_window_key = ?` + bulk INSERT) inside the parent's transaction. Reconcile invariants: `SUM(child.cost_usd) ≈ parent.total_cost_usd ± 1e-9`; `SUM(child.<token_col>) == parent.total_<token_col>` exactly. NULL `session_files.project_path` collapses to `'(unknown)'`.
- **`_compute_block_totals` reads via `get_claude_session_entries`, not `get_entries`** — breakdown buckets need `project_path` from the `session_files` LEFT JOIN. Same fallback chain (cache → lock-contention → direct-JSONL parse).
- **`five-hour-blocks --breakdown` is a value-flag (`{model,project}`),** not boolean — diverges from `weekly --breakdown`. JSON consumers probe via `if "modelBreakdowns" in row:`; unselected axis is omitted (not empty array).
- **Naive `--block-start` is UTC, not `--tz`.** `--tz` controls *display only*. Pass explicit offset (`...T19:30:00+03:00`) or `Z` for non-UTC. Date-only forms rejected with exit 2.

### Diff
- **`diff` JSON schema is `schema_version: 1`.** Stable: `windows.{a,b}.{label,kind,start_at,end_at,used_pct_mode}`, `sections[].rows[].{key,label,status}`, row `{a,b,delta}` shape (consumers MUST tolerate unknown keys), `options.*`. NOT stable: `columns[]`, `sort_key` semantics. Bump version on breaking change; adding optional keys does not bump.
- **`diff` cache section's `cost_usd` is full cost of cache-active entries**, NOT cache-attribution share (JSON `scope: "cache-active-entries"`). Sum across the three cache scope rows ≠ overall — independent slices.
- **`diff` errors emit plain text on stderr**, even with `--json` — `cmd_diff` error paths print `diff: <message>` and exit 1 or 2. Check exit code, not stdout-only.
- **`diff` re-aggregation eliminated.** `_build_diff_result` retains pre-filter aggregate maps on `DiffResult.raw_totals`; `cmd_diff` passes them to the renderer. Don't reintroduce the prior 8-walk double-aggregation.

### Hooks & config
- **`hook-tick` MUST read stdin before the detach.** CC hooks deliver payload as JSON on stdin; detach closes stdin. Without reading first, `event=unknown` for every fire and `--status` activity attribution breaks.
- **`hook-tick` does NOT bust `/tmp/claude-statusline-usage-cache.json`** — `cmd_refresh_usage` does. Hook path uses its own throttle marker at `~/.local/share/cctally/hook-tick.last-fetch` (busting would delete the file we read mtime on).
- **`settings.json` hook command is `shlex.quote`d** so a checkout path with spaces survives `/bin/sh -c`. `_is_cctally_hook_command` uses `shlex.split` to recognize quoted-or-unquoted forms uniformly.
- **`config.json` writes go through `config_writer_lock` + atomic `os.replace`.** Three writer sites (`_cmd_config_set`, `_cmd_config_unset`, `POST /api/settings`) acquire exclusive `fcntl.flock` on `config.json.lock`; `save_config` writes a PID-suffixed sibling tmp before rename. `load_config` does NOT acquire the lock; on `JSONDecodeError` warns once and returns in-memory defaults WITHOUT silently re-saving. **Do not call `load_config` from inside the writer lock — `fcntl.flock` is per-fd, would self-deadlock**; use `_load_config_unlocked`. Regression: `bin/cctally-config-test` steps 10/11/12.
- **`setup` recognizes its own hook entries by command-tail tokens, not custom marker keys.** Anything whose command, after stripping trailing `&` and `shlex.split`-parsing, has last-two tokens `<path-with-basename-cctally> hook-tick` is ours. Hand-edits → `--status` shows "1 expected entry not found".

### OAuth / refresh
- **`/api/oauth/usage` requires `claude-code/*` UA.** Anthropic rate-limits per User-Agent; `Python-urllib`, `cctally/*`, etc. get 429 during active sessions. Default UA via `_resolve_oauth_usage_user_agent`; opt-out via `oauth_usage.user_agent`.
- **`oauth_usage` config validation exits 2 on invalid blocks.** Cross-field constraint `fresh_threshold_seconds < stale_after_seconds`; throttle clamped `[5, 600]`. Defaults: `{user_agent: null, throttle: 15, fresh: 30, stale: 90}`.
- **`cmd_refresh_usage` returns exit 0 on HTTP 429.** Not user-actionable (only UA changes fix it); treating as error trains users to ignore real failures. Non-429 network failures still exit 3. JSON-mode 429 emits `{status: "rate_limited", fallback, freshness, reason}`.
- **TUI / dashboard freshness chip relies on `latest_snapshot_at`.** When absent, chip is hidden. When present, `_freshness_label` maps age to `fresh|aging|stale`.

### Display TZ
- **`display.tz` controls render; date-bucketing commands also parse `--since`/`--until` in display tz.** `daily`/`monthly`/`session`/`cache-report` (and `codex-*`) parse naive date-only `--since`/`--until` in display tz; full-ISO forms (`T`/`+`/`Z`) carry their own offset and are tz-independent. EXCEPTIONS: `--block-start` for `five-hour-breakdown` is UTC; `blocks` keeps host-local upstream-parity. JSON output ignores `display.tz` — every `--json` emits `…Z`. Reconcile invariants TZ1–TZ5 in `bin/cctally-reconcile-test`.
- **Datetime render chokepoints: `format_display_dt` + `lib/fmt.ts`.** All human-displayed datetimes route through these two files. **Non-targets:** (a) bucket keys — aggregators use raw `astimezone(tz).strftime(...)` for dict keys (routing through `format_display_dt` would append a tz suffix and break key equality); (b) internal-fallback bare `astimezone()` annotated `# internal fallback: host-local intentional` — used for "today" calendar-day calculations and grounding naive date strings for week-anchor discovery. NOT for user-facing `--since`/`--until`.

### Alerts
- **Set-then-dispatch invariant on alert dispatch.** `alerted_at` is written to `percent_milestones` / `five_hour_milestones` BEFORE the non-blocking `osascript` `Popen`. `alerted_at IS NOT NULL` means "we recorded that an alert was due and queued the popup," NOT "the user definitely saw it." Dispatch failures MUST NOT roll back the milestone — contract is one queue attempt per crossing, deduped on the column. Inverting this opens a re-fire loop. Regression: `bin/cctally-alerts-test` scenario `osascript-missing`.
- **Test alerts deliberately diverge from real alerts.** `cctally alerts test` and `POST /api/alerts/test` build synthetic payloads via the same `_build_alert_payload_*` helpers but with NO DB writes, NO envelope mutation; the dashboard endpoint returns the payload directly so a toast renders even when osascript fails. Only shared surface is `alerts.log` (`mode=test` vs `mode=real` in 5th tab field). If test fires but real don't, bug is upstream. Don't "unify" the paths.
- **Severity color hardcoded for v1: `< 95` → amber, `>= 95` → red,** regardless of which thresholds the user picked. Multi-tier severity is deferred to v2.
- **`insert_percent_milestone` returns `cur.rowcount`, not `cur.lastrowid`.** Callers detect "genuinely new crossing" (`rowcount == 1`) vs. "INSERT OR IGNORE no-op" (`rowcount == 0`) without follow-up SELECT. Race-safe alert-fire predicate depends on it. Callers needing the row ID must follow up with `SELECT id`. The 5h equivalent (inline `INSERT OR IGNORE INTO five_hour_milestones`) follows the same contract.
- **Dashboard envelope's `alerts_settings` block rides the same SSE channel as the rest of the snapshot.** No separate auth surface; loopback bind + Origin/Host parity is the entire protection. Mutations flow via `POST /api/settings`, gated by `_check_origin_csrf`.

## Public Mirror Trailer System

The public repo (`omrikais/cctally`) is mirrored from this private repo via `bin/cctally-mirror-public`. Private commit messages are detailed/process-rich; public messages should describe what shipped in a public-release voice. The trailer system decouples them.

When constructing a commit, classify staged files:
```bash
python3 .githooks/_public_trailer.py classify --staged
```
Non-empty output means the commit message must include either:
- A **`--- public ---`** block at the end with a public-voice subject (and optional body), OR
- A **`Public-Skip: true`** trailer to drop the commit from the mirror (use for fix-typo / regenerate-fixture follow-ups whose content accumulates into the next published commit's diff).

The two surfaces are mutually exclusive. The commit-msg hook enforces this; the mirror tool validates as backstop. Full grammar, error codes, and edge cases in `docs/superpowers/specs/2026-05-05-public-trailer-system-design.md`.

## Key Conventions

- The main script is a single-file Python program with no external dependencies (stdlib only).
- All wrappers in `bin/` must stay executable (`chmod +x`).
- Config lives in `~/.local/share/cctally/config.json` (week-start day, etc.) — never committed.
