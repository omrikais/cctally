# `cctally config`

Manage cctally user preferences in `~/.local/share/cctally/config.json`.

## Subcommands

```
cctally config get [<key>] [--json]
cctally config set <key> <value> [--json]
cctally config unset <key>
```

## Allowed keys

Every key `cctally config set` accepts, in the order the CLI's own allowlist
declares them. `cctally config get` with no key prints the same set.

The **Dashboard writable** column says what `POST /api/settings` does with the
key, and it has three states rather than two:

- **Yes** — the dashboard Settings overlay writes it, and a direct POST
  persists it.
- **Ignored** — the endpoint accepts the key, does not persist it, and now
  reports it back in the response's `ignored_fields` array. Use
  `cctally config set` to change it for real.
- **No** — the endpoint rejects it with HTTP 400 and a `field` pointer. Some
  of these are CLI-only because they hold secrets or arm destructive
  behavior; others apply only at server startup.

| Key | Values | Default | Dashboard writable | Notes |
|-----|--------|---------|--------------------|-------|
| `display.tz` | `local`, `utc`, or any IANA name (e.g. `America/New_York`). | `local` | Yes | The render zone for every subcommand that prints a clock instant, and the parse zone for naive `--since`/`--until` on the date-bucketing commands. A per-call `--tz` flag wins for that one invocation. See [How `display.tz` interacts with subcommands](#how-displaytz-interacts-with-subcommands). |
| `alerts.enabled` | Boolean (`true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off`). | `false` | Yes | The master switch for threshold alerts. See [`alerts.md`](alerts.md). |
| `alerts.projected_enabled` | Boolean. | `false` | Yes | Opt-in for projected-pace alerts, so an upgrade fires no surprise notifications. See [`alerts.md`](alerts.md). |
| `alerts.notifier` | `auto`, `osascript`, `notify-send`, `command`, `none` — the OS-popup backend for threshold alerts. | `auto` | Yes | See [Alerts dispatch keys](#alerts-dispatch-keys). |
| `alerts.command_template` | JSON: a non-empty list of argv strings (e.g. `["notify-send","{title}","{body}"]`) or `null` to clear. | `null` | No | Trusted local command execution, and it routinely holds secrets, so the dashboard refuses it and redacts it from every echo as the boolean `command_configured`. See [Alerts dispatch keys](#alerts-dispatch-keys). |
| `alerts.quota` | JSON object with `enabled`, `actual_thresholds`, `projected_thresholds` and `rules`. | `{"enabled": false, "actual_thresholds": [90, 95], "projected_thresholds": [], "rules": []}` | No | The Codex quota alert axis. The block is absent from `config.json` until you write it, and disabled until `enabled` is true. Written as a whole object from the CLI. See [`codex-quota.md`](codex-quota.md). |
| `dashboard.bind` | `loopback` (= `127.0.0.1`), `lan` (= `0.0.0.0`), or any literal host string (IPv4, IPv6, hostname). Resolution order: `--host` flag > config > default. | `loopback` | No | Applies only at server startup, so the running server keeps its bind. See [`dashboard.md`](dashboard.md). |
| `dashboard.expose_transcripts` | Boolean (`true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off`). The LAN opt-in for the conversation-viewer transcript endpoints. When `false` those routes are served **only** over loopback. | `false` | No | A privacy gate, not live-mutable. Even when `true` an anti-DNS-rebinding `Host` allowlist applies — see [`dashboard.md`](dashboard.md#conversation-viewer-endpoints-plan-2). |
| `dashboard.cache_failure_markers` | Boolean. Opt-out for the conversation-viewer cache-rebuild markers (the amber `⚡` chip on a turn that re-created the bulk of its cached prefix). Absence is treated as ON. | `true` | Yes | `false` hides every marker, the outline landmark/jump button, and the stats count. Also toggleable from the dashboard settings modal. |
| `dashboard.live_tail` | Boolean. Opt-out for the conversation-viewer live-tail. Absence is treated as ON. | `true` | Yes | `true` lets an open reader follow an active session within ~1s via a per-conversation SSE stream; `false` falls back to the 5-second snapshot tick. See [`dashboard.md`](dashboard.md#live-tail). |
| `dashboard.lan_auth` | Boolean. Requires the per-run bearer token on every `/api/*` request when the dashboard binds to a non-loopback address. | `true` | Yes | `true` is fail-safe; set `false` only for a trusted LAN. The running server keeps its startup access mode, so a change applies **only after restarting the dashboard**. |
| `update.check.enabled` | Boolean (a JSON boolean over the API; a string is rejected). | `true` | Yes | Whether the background update check runs at all. See [`update.md`](update.md). |
| `update.check.ttl_hours` | Integer in `[1, 720]`. A JSON integer, not a string; a boolean is rejected because `bool` is an `int` subclass. | `24` | Yes | How long one update-check result is reused before the next check. See [`update.md`](update.md). |
| `update.channel` | `stable` or `beta` — the release channel `cctally update` tracks. `beta` receives every release as it ships; `stable` only the maintainer-promoted ones. | `stable` | Yes | Install-method-independent (Homebrew always tracks stable). Also toggleable from the dashboard settings modal. See [`update.md`](update.md#beta-channel). |
| `statusline.visual_burn_rate` | `off`, `emoji`, `text`, `emoji-text`. | `off` | No | The segment-3 visual indicator; the `-B`/`--visual-burn-rate` flag wins per call. See [`statusline.md`](statusline.md). |
| `statusline.cost_source` | `auto`, `cctally`, `cc`, `both`. The legacy `ccusage` value is rejected with a rename hint. | `auto` | No | Which session-cost source the status line renders. See [`statusline.md`](statusline.md). |
| `statusline.cctally_extensions` | Boolean (`true`/`false`/`yes`/`no`/`on`/`off`/`1`/`0`). | `true` | No | Appends or suppresses the cctally extension segment. See [`statusline.md`](statusline.md). |
| `statusline.usage_only` | Boolean. | `false` | No | Renders only the `5h X% · 7d Y%` subscription percentages. See [`statusline.md`](statusline.md). |
| `budget.weekly_usd` | A finite number greater than zero, or `null` for no budget. | `null` | Yes | The Claude equivalent-spend budget. See [`budget.md`](budget.md). |
| `budget.alerts_enabled` | Boolean. | `true` | Yes | On when a budget is set. See [`budget.md`](budget.md). |
| `budget.alert_thresholds` | Comma-separated base-10 integers in `[1,100]`; values are sorted and deduplicated, and an empty string means `[]` (silenced). | `90,100` | Yes | The percentages of the budget that fire an alert. See [`budget.md`](budget.md). |
| `budget.projected_enabled` | Boolean. | `false` | Yes | Opt-in for the projected-pace budget alert. See [`budget.md`](budget.md). |
| `budget.period` | `subscription-week`, `calendar-week`, or `calendar-month`. | `subscription-week` | Ignored | The dashboard accepts this leaf and runs the same forward-only reconcile the CLI does, but never stores it — the response discloses that in `ignored_fields`. Set it with `cctally config set`. See [`budget.md`](budget.md). |
| `budget.projects` | JSON object of `{canonical git-root path: usd}`; each value a finite number greater than zero. | `{}` | No | Per-project weekly budgets, CLI-only. See [`budget.md`](budget.md). |
| `budget.project_alerts_enabled` | Boolean. | `false` | Yes | Opt-in for per-project budget alerts. See [`budget.md`](budget.md). |
| `budget.accounts` | JSON object of `{account ref: usd}`; each value a finite number greater than zero. | `{}` | No | Per-account Claude budgets. Refs are resolved to immutable account keys at write time, which is why this is CLI-only. See [`account.md`](account.md). |
| `budget.codex` | Whole Codex budget object, or `null` for no Codex budget. | `null` | No | This compatibility key remains round-trippable; the leaf keys below are preferred for partial edits. See [Codex budget leaf writes](#codex-budget-leaf-writes). |
| `budget.codex.amount_usd` | Finite decimal strictly greater than zero. | `null` | Ignored | Writing it creates a missing Codex block with every default below; unsetting it removes the whole block. Amounts are never invented from a browser, so the endpoint accepts and drops this leaf. See [Codex budget leaf writes](#codex-budget-leaf-writes). |
| `budget.codex.period` | `calendar-week` or `calendar-month`; Codex never uses `subscription-week`. | `calendar-month` | Ignored | Accepted and dropped by the dashboard, like every other CLI-only Codex leaf. See [Codex budget leaf writes](#codex-budget-leaf-writes). |
| `budget.codex.alerts_enabled` | Boolean (`true`/`false`/`yes`/`no`/`on`/`off`/`1`/`0`). | `false` | Yes | Over the API this must be a JSON boolean; a string is rejected exactly as its Claude sibling is. See [Codex budget leaf writes](#codex-budget-leaf-writes). |
| `budget.codex.alert_thresholds` | Comma-separated base-10 integers in `[1,100]`; sorted and deduplicated, empty string means `[]`. | `90,100` | Ignored | Accepted and dropped by the dashboard. See [Codex budget leaf writes](#codex-budget-leaf-writes). |
| `budget.codex.projected_enabled` | Boolean controlling the Codex projected-budget alert. | `false` | Yes | See [Codex budget leaf writes](#codex-budget-leaf-writes). |
| `budget.codex.accounts` | JSON object of `{account ref: usd}`; each value a finite number greater than zero. | `{}` | No | Per-account Codex budgets. Refs are resolved to immutable account keys at write time. See [`account.md`](account.md). |
| `telemetry.enabled` | Boolean. Opt-out for the anonymous install-count telemetry; absence is treated as ON. | `true` | No | Also disabled by `CCTALLY_DISABLE_TELEMETRY=1`, `DO_NOT_TRACK=1`, and dev checkouts. `cctally telemetry off`/`on` is a thin wrapper over this key. See [`telemetry.md`](telemetry.md) and the [privacy page](../telemetry.md). |
| `conversation.retention_days` | Positive integer, or `off` / `0` to keep transcripts forever. | `90` | No | How many days of conversation transcripts are retained in `cache.db`. Cost and usage history is never affected, and transcripts are re-derivable from the JSONL. A malformed persisted value resolves to the default. See [`cache-sync.md`](cache-sync.md#--prune-conversations) and [`db.md`](db.md#db-vacuum). |
| `storage.artifact_retention` | JSON object bounding the on-disk evidence corruption recovery retains. Fields: `max_age_days`, `max_count_per_family`, `max_total_mib`, `min_free_mib` — each a positive integer or `null` to disable that rule — and `max_shape_examples`, a positive integer that is **never** nullable. Omitted fields inherit their default. At least one of `max_age_days`, `max_count_per_family` and `max_total_mib` must stay enabled. | `{"max_age_days": 30, "max_count_per_family": 20, "max_total_mib": 4096, "min_free_mib": 10240, "max_shape_examples": 8}` | No | A destructive-retention policy, managed through `cctally config` and `cctally db prune`, and never browser-writable. A malformed persisted block turns automatic reclamation OFF, FAILs `doctor`'s `db.retained_artifacts` leg, and makes `cctally db prune` exit 2 without deleting anything — cctally never falls back to a policy you did not write. Unclassified, active or referenced evidence is never deleted under any policy. See [`db.md`](db.md#db-prune). |
| `codex.hook.ingest_budget_seconds` | Positive number, strictly below 20 (Codex kills a hook at 30 seconds). A value at or above the cap is rejected (exit 2) rather than clamped. | `5` | No | The wall-clock ceiling on the native Codex hook's rollout-ingest leg: the hook stops at the deadline, records where it stopped, and resumes there next tick. Applies to the hook path ONLY — `cctally cache-sync --source codex` always runs to completion. A malformed persisted value resolves to the default. Surfaced by `doctor`'s `data.codex_ingest_backlog` leg. |

`cache_report.anomaly_threshold_pp` is deliberately **not** in this table. The
dashboard writes it and both the dashboard and the TUI read it, but it is not
a `cctally config set` key and the `cache-report` CLI does not read it — see
[`cache-report.md`](cache-report.md#anomaly-threshold-sources).

`tests/test_config_documentation.py` ties this table to the runtime: the keys
and their order come from the CLI's allowlist, the **Dashboard writable**
column from the endpoint's own disposition map, and every default the code can
hand over is compared against its cell. Adding a config key without adding a
row here fails that check.

## Examples

Bare `cctally config get` prints one `key=value` line for every key in the
table above, resolved to its effective value:

```bash
cctally config get
# display.tz=local
# alerts.enabled=false
# alerts.projected_enabled=false
# alerts.notifier=auto
# alerts.command_template=null
# ... one line per allowed key, in the same order as the table ...
# codex.hook.ingest_budget_seconds=5.0
```

Pass a key to print just that one:

```bash
cctally config set display.tz America/New_York
cctally config get display.tz
# display.tz=America/New_York

cctally config unset display.tz
```

## Codex budget leaf writes

The five `budget.codex.*` leaves share the whole-object validator. A first
`amount_usd` write creates a complete block with `calendar-month`, disabled
alerts, `90,100` thresholds, and disabled projected alerts; setting any other
leaf before an amount exists exits 2. Each successful leaf write is a locked,
validated nested merge and, while a budget remains configured, performs the
same one forward-only Codex budget reconciliation as the whole-object write.

```bash
cctally config set budget.codex.amount_usd 200
# budget.codex.amount_usd=200
cctally config set budget.codex.period calendar-week
cctally config set budget.codex.alert_thresholds 100,90,90
# budget.codex.alert_thresholds=90,100
cctally config get budget.codex.period --json
# {"budget":{"codex":{"period":"calendar-week"}}}
```

Human `get` prints the canonical `dotted.key=value`; JSON is the existing
unversioned nested CRUD echo, not a schema-stamped analytics envelope. Unknown
leaves, invalid values, or a malformed existing `budget.codex` object exit 2
without mutation. `unset` is idempotent and silent: unsetting `amount_usd`
removes the block; unsetting any optional leaf restores its default while
preserving the amount and other leaves.

Provider selection is intentionally **not** a config leaf. Use the per-command
`--source {claude,codex,all}` flag or the fixed `cctally claude|codex` subgroup
forms; `claude` remains the default.

## Alerts dispatch keys

Two keys select how threshold alerts fire their OS popup. Both live in
the `alerts` config block and are settable via `config set`; full alert
behavior is in [`alerts.md`](alerts.md).

### `alerts.notifier`

The dispatch backend. Resolved per host + config at fire time:

| Value | Effect |
|-------|--------|
| `auto` (default) | `command_template` (if set, on **any** OS) → `osascript` on macOS → `notify-send` on Linux → `none`. |
| `osascript` | macOS `display notification`; downgrades to `none` off macOS. |
| `notify-send` | Linux `notify-send`; downgrades to `none` if not Linux or the binary is missing. |
| `command` | Spawn `alerts.command_template` (which it then **requires** to be set). |
| `none` | No OS popup; the log line and dashboard surfaces still fire. |

```bash
cctally config set alerts.notifier notify-send
cctally config get alerts.notifier   # alerts.notifier=notify-send
```

**Precedence cue:** under `auto`, a set `command_template` overrides the
native backend on every platform — set it to take over dispatch
regardless of OS. An explicitly-selected native notifier that is
unavailable on this host downgrades to `none` (it is never
spawned-and-failed).

### `alerts.command_template`

A custom argv list spawned for the `command` backend (and for `auto`
when set). The value is JSON — a non-empty list of strings, or `null` to
clear it:

```bash
cctally config set alerts.command_template '["notify-send","-u","{urgency}","{title}","{body}"]'
cctally config unset alerts.command_template   # back to null
```

**Substitution tokens** (one-pass, left-to-right; substituted values are
NOT re-scanned; unmatched `{…}` and any non-token braces stay literal; a
missing/None key substitutes as `""`):

`{title}`, `{subtitle}`, `{body}`, `{severity}` (`info`/`warn`/`critical`),
`{urgency}` (`low`/`normal`/`critical`), `{axis}`, `{threshold}`,
`{metric}`.

**Safety / trust model.** `alerts.command_template` is **trusted local
command execution** — you own `config.json`, so the template's program is
whatever you put there. The spawn is `shell=False` with the arg-list form
(never a shell string), so alert text containing `$(...)`, `;`, or `&&`
is passed as one literal argument and cannot inject a shell command. The
native `notify-send` path additionally inserts a `--` end-of-options
delimiter so a title/body starting with `-` can't be parsed as a flag.

**Validation** (enforced before the value is persisted, so a written
config never fails a later read):

- `null`, or a non-empty list of strings (empty list rejected).
- Every element is a string; no NUL bytes.
- `command_template[0]` (the program) must not be empty/whitespace.
- `alerts.notifier='command'` requires `command_template` to be set.

An invalid value exits 2 with `cctally: alerts config error: …` and
leaves the stored config untouched.

## How `display.tz` interacts with subcommands

### Topology

Every subcommand that renders a clock instant (forecast, tui, dashboard,
report, weekly, daily, monthly, blocks, five-hour-blocks,
five-hour-breakdown, session, codex-{daily,monthly,weekly,session},
cache-report, diff, percent-breakdown, `cctally codex percent-breakdown`,
project) reads `display.tz` to
decide which zone to render labels in. A per-call `--tz <value>` flag
overrides the persisted value for that one invocation.

### Accepted values

`local`, `utc`, or any IANA zone name (e.g. `America/New_York`,
`Europe/Berlin`, `Asia/Jerusalem`). Same allowlist as `config set
display.tz`.

### `--json` UTC invariant

The `--json` output of every subcommand emits ISO timestamps in
`…Z`-suffixed UTC regardless of `display.tz` / `--tz` — `display.tz`
controls human-readable display only. (Tested invariant: TZ1 in
`bin/cctally-reconcile-test`.)

### Parsing scope

For `daily`, `monthly`, `session`, `cache-report`, and the codex-*
equivalents (`codex-daily`, `codex-monthly`, `codex-weekly`,
`codex-session`), naive `--since` / `--until` (date-only or naive ISO,
no offset) are parsed **in the resolved display tz**. So
`--tz utc --since 2026-05-01` lands at `2026-05-01T00:00Z`, not
`2026-04-30T21:00Z` on a Jerusalem host. Full-ISO `--since` / `--until`
values containing `T`/`+`/`Z` carry their own offset and are
tz-independent.

Two exceptions:

- **`five-hour-breakdown --block-start`** — naive values are parsed as
  **UTC**, since this is a canonical 5h-window key, not a calendar-day
  boundary. Pass an explicit offset or `Z` for non-UTC. Date-only
  inputs are rejected (exit 2).
- **`blocks`** — keeps host-local upstream-parity parsing. Documented
  divergence from upstream `ccusage`; preserved so JSON output matches
  upstream byte-for-byte.

### Dashboard pin behavior

Launching `cctally dashboard --tz <X>` **pins** the display tz for the
server's lifetime. While pinned:

- The Settings overlay's "Display timezone" form is disabled (the
  dashboard renders a read-only badge showing the pinned zone).
- `POST /api/settings` returns 409 if a client tries to change
  `display.tz` anyway.

Without `--tz`, the dashboard reads the persisted `display.tz` config
and the Settings UI mirrors it (see "Dashboard mirror" below). Restart
the dashboard without `--tz` to re-enable Settings-driven changes.

## Dashboard mirror

`POST /api/settings` accepts six blocks: `display`, `alerts`, `budget`,
`dashboard`, `update`, and `cache_report`. The **Dashboard writable** column
in [Allowed keys](#allowed-keys) is the per-key authority — the Settings
overlay itself sends only a subset of what the endpoint accepts, so the
column describes the endpoint, not the current overlay.

The overlay's **Require LAN access token** toggle controls
`dashboard.lan_auth`; saving it does not alter the running server, and the
new access mode begins only after the dashboard restarts. Saving hits
`POST /api/settings` (gated by Origin-vs-Host parity CSRF; see
[`docs/commands/dashboard.md`](dashboard.md#threat-model)); the change
propagates to all open tabs via SSE within ~100ms. The display form is
disabled while pinned by a startup `--tz` (see "Dashboard pin behavior"
above).

Three rules govern what the endpoint does with a key you send it:

- A key marked **Yes** is validated and persisted.
- A key marked **Ignored** answers `200` and appears in the response's
  `ignored_fields` array, a sorted list of the accepted-but-not-persisted
  paths in that request. The field is omitted entirely when the request
  contained none, so an ordinary save is unchanged.
- Any other path answers `400` with `{"error": ..., "field": "<dotted
  path>"}`. Whole-document failures — a body over 4 KB, an empty body,
  malformed JSON, a payload that is not an object, or an object naming no
  known block — use `"$"` as the field.

A named block carrying no leaves is a no-op that still echoes that block, so
a combined save may include `{"cache_report": {}}` for a tab the user never
opened without clobbering anything. `{"display": {}}` is the one exception:
`display.tz` is required, so it answers `400`.

## Display timezone behavior

Two canonical render paths produce every datetime visible to a user:

- **Python (`bin/cctally`)** — `format_display_dt(value, tz, *, fmt, suffix)`. Naive inputs are treated as UTC. `tz=None` means "host-local via bare `astimezone()`"; pass a `ZoneInfo` for any explicit zone. Suffix follows `display_tz_label`: alphanumeric `tzname()` (≤5 chars) wins, else numeric offset (`+05`, `+05:30`, etc.). Set `suffix=False` for date-only labels (`%b %d`, `%Y-%m-%d`) where a zone token would clash with the surrounding text.
- **TypeScript (`dashboard/web/src/lib/fmt.ts`)** — `fmt.datetimeShort`, `fmt.datetimeShortZ`, `fmt.dateShort`, `fmt.startedShort`, `fmt.timeHHmm`. Every consumer reads the `FmtCtx { tz, offsetLabel }` from `useDisplayTz()`, which sources its values from the snapshot envelope's `display` block (server-resolved IANA in `resolved_tz`, never browser-resolved).

Both paths share the same `display.tz` config setting (managed by `cctally config get|set|unset display.tz`). Per-call `--tz` flags on subcommands win over the persisted value for that call only. Adding a future locale dimension (12h vs 24h, day-name format) is a one-site change in each chokepoint.

For the parse-time tz rules on `--since`/`--until` and friends, see the per-subcommand pages and CLAUDE.md's `display.tz` gotcha.

## Errors

- `cctally config: invalid IANA zone '<X>'` (exit 2) — the value is
  neither `local` nor `utc` nor a recognized IANA name.
- `cctally config: unknown config key '<X>'` (exit 2) — the key is not one
  of the keys listed under [Allowed keys](#allowed-keys). That table is the
  allowlist; nothing outside it is settable.
- `cctally: alerts config error: <detail>` (exit 2) — an
  `alerts.notifier` / `alerts.command_template` value failed validation
  (bad enum, malformed template, or `notifier='command'` with no
  template). The stored config is left untouched.
