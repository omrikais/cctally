# `cctally config`

Manage cctally user preferences in `~/.local/share/cctally/config.json`.

## Subcommands

```
cctally config get [<key>] [--json]
cctally config set <key> <value> [--json]
cctally config unset <key>
```

## Allowed keys

| Key | Values | Default |
|-----|--------|---------|
| `dashboard.bind` | `loopback` (= `127.0.0.1`, default), `lan` (= `0.0.0.0`), or any literal host string (IPv4, IPv6, hostname). Resolution order: `--host` flag > config > default. Applies only at server startup. | `loopback` |
| `dashboard.expose_transcripts` | Boolean (`true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off`). The LAN opt-in for the conversation-viewer transcript endpoints. When `false` (default) those routes are served **only** over loopback; set `true` to also serve them on a LAN bind. Even then an anti-DNS-rebinding `Host` allowlist applies — see [`dashboard.md`](dashboard.md#conversation-viewer-endpoints-plan-2). | `false` |
| `dashboard.cache_failure_markers` | Boolean (`true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off`). Opt-out for the conversation-viewer cache-rebuild markers (the amber `⚡` chip on a turn that re-created the bulk of its cached prefix). `true` (default) shows them; set `false` to hide every marker, the outline landmark/jump button, and the stats count. Absence is treated as ON. Also toggleable from the dashboard settings modal. | `true` |
| `display.tz` | `local`, `utc`, or any IANA name (e.g. `America/New_York`) | `local` |
| `alerts.notifier` | `auto`, `osascript`, `notify-send`, `command`, `none` — the OS-popup backend for threshold alerts. See [Alerts dispatch keys](#alerts-dispatch-keys). | `auto` |
| `alerts.command_template` | JSON: a non-empty list of argv strings (e.g. `["notify-send","{title}","{body}"]`) or `null` to clear. See [Alerts dispatch keys](#alerts-dispatch-keys). | `null` |

## Examples

```bash
cctally config get
# display.tz=local

cctally config set display.tz America/New_York
cctally config get display.tz
# display.tz=America/New_York

cctally config unset display.tz
```

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
cache-report, diff, percent-breakdown, project) reads `display.tz` to
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

The web dashboard's Settings overlay (`s` key) has a "Display timezone"
section that mirrors `display.tz`. Saving from the dashboard hits
`POST /api/settings` (gated by Origin-vs-Host parity CSRF; see
[`docs/commands/dashboard.md`](dashboard.md#threat-model)); the change
propagates to all open tabs via SSE within ~100ms. Disabled while pinned
by a startup `--tz` (see "Dashboard pin behavior" above).

## Display timezone behavior

Two canonical render paths produce every datetime visible to a user:

- **Python (`bin/cctally`)** — `format_display_dt(value, tz, *, fmt, suffix)`. Naive inputs are treated as UTC. `tz=None` means "host-local via bare `astimezone()`"; pass a `ZoneInfo` for any explicit zone. Suffix follows `display_tz_label`: alphanumeric `tzname()` (≤5 chars) wins, else numeric offset (`+05`, `+05:30`, etc.). Set `suffix=False` for date-only labels (`%b %d`, `%Y-%m-%d`) where a zone token would clash with the surrounding text.
- **TypeScript (`dashboard/web/src/lib/fmt.ts`)** — `fmt.datetimeShort`, `fmt.datetimeShortZ`, `fmt.dateShort`, `fmt.startedShort`, `fmt.timeHHmm`. Every consumer reads the `FmtCtx { tz, offsetLabel }` from `useDisplayTz()`, which sources its values from the snapshot envelope's `display` block (server-resolved IANA in `resolved_tz`, never browser-resolved).

Both paths share the same `display.tz` config setting (managed by `cctally config get|set|unset display.tz`). Per-call `--tz` flags on subcommands win over the persisted value for that call only. Adding a future locale dimension (12h vs 24h, day-name format) is a one-site change in each chokepoint.

For the parse-time tz rules on `--since`/`--until` and friends, see the per-subcommand pages and CLAUDE.md's `display.tz` gotcha.

## Errors

- `cctally config: invalid IANA zone '<X>'` (exit 2) — the value is
  neither `local` nor `utc` nor a recognized IANA name.
- `cctally config: unknown config key '<X>'` (exit 2) — the key is not
  in the allowlist (`display.tz`, `dashboard.bind`,
  `dashboard.expose_transcripts`, the `alerts.*` keys including
  `alerts.notifier` / `alerts.command_template`, the `statusline.*` keys,
  the `budget.*` keys, and the `update.check.*` keys).
- `cctally: alerts config error: <detail>` (exit 2) — an
  `alerts.notifier` / `alerts.command_template` value failed validation
  (bad enum, malformed template, or `notifier='command'` with no
  template). The stored config is left untouched.
