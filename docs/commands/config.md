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
| `display.tz` | `local`, `utc`, or any IANA name (e.g. `America/New_York`) | `local` |

## Examples

```bash
cctally config get
# display.tz=local

cctally config set display.tz America/New_York
cctally config get display.tz
# display.tz=America/New_York

cctally config unset display.tz
```

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
  in the allowlist (currently `display.tz`, `alerts.enabled`,
  `dashboard.bind`).
