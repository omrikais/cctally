# `dashboard`

Launch a live web dashboard rendering your current subscription usage,
forecast, $/1% trend, and recent sessions. Coexists with the `tui`
subcommand — use `tui` over SSH or when a browser isn't available.

## Usage

```
cctally dashboard [--host H] [--port N] [--no-browser]
                                      [--sync-interval SEC] [--no-sync]
                                      [--tz TZ]
```

Or via the wrapper:

```
cctally-dashboard [...same flags...]
```

## Flags

| Flag | Default | Purpose |
|---|---|---|
| `--host H` | `127.0.0.1` (loopback) | Interface to bind. `127.0.0.1` binds loopback only (this machine); `0.0.0.0` binds all interfaces (LAN-accessible). Resolution order: `--host` flag > `dashboard.bind` config > default. See [LAN access](#lan-access) and [Threat model](#threat-model) below. |
| `--port N` | `8789` | TCP port to bind. Change if 8789 is taken. |
| `--no-browser` | off | Skip auto-opening the browser (useful for SSH tunnels). |
| `--sync-interval SEC` | `5` | Background snapshot-rebuild cadence in seconds. |
| `--no-sync` | off | Freeze data to snapshot at startup (for debugging / fixtures). |
| `--tz TZ` | config | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). |

Passing `--tz` at startup **pins** the display tz for the server's
lifetime. While pinned, the Settings UI's "Display timezone" form is
disabled and `POST /api/settings` returns 409 if a client tries to
change `display.tz`. Without `--tz`, the dashboard reads (and the
Settings UI mirrors) the persisted `display.tz` config; restart without
`--tz` to re-enable Settings-driven changes.

## LAN access

By default, `cctally dashboard` binds to `127.0.0.1` (loopback only), so the
dashboard is reachable only from the machine that launched it. The startup
banner shows a single localhost URL:

    dashboard: serving http://localhost:8789/ — Ctrl-C to stop

To opt in to LAN access (reachable from your phone or another laptop on the
same Wi-Fi):

    cctally dashboard --host 0.0.0.0   # one-shot
    cctally config set dashboard.bind lan   # persist

Once LAN-bound, the banner shows both URLs:

    dashboard: serving on all interfaces:
      - http://localhost:8789/      (this machine)
      - http://192.168.1.42:8789/   (LAN)
    Ctrl-C to stop

The sync chip / `r` shortcut works from any of these devices. Read
[Threat model](#threat-model) before opting in — LAN bind exposes every
`/api/*` surface to anyone on the same network.

Valid `dashboard.bind` values: `loopback` (= `127.0.0.1`, default), `lan`
(= `0.0.0.0`), or any literal host string (an IPv4, IPv6, hostname, or e.g.
a Tailscale tun IP).

## Threat model

The dashboard has **no authentication**. Origin/Host parity blocks
browser-driven cross-origin attacks, but anyone on the same LAN with `curl`
can:

- Read your usage data (GET /api/data, /api/events)
- Trigger an OAuth refresh from Anthropic (POST /api/sync) — capped only by
  Anthropic's per-User-Agent 429 rate limit. A hostile LAN peer can burn
  your OAuth quota for ~15 minutes by hammering the chip.
- Mutate your persisted config (POST /api/settings) — disable alerts,
  change display tz, etc. There is no rate cap on this surface.
- Trigger an osascript popup (POST /api/alerts/test).

Use `cctally dashboard` only on networks you trust — home Wi-Fi, a
Tailscale tailnet, a VPN. NOT on public Wi-Fi or shared/untrusted LANs.

Token-based auth for off-LAN exposure is a deferred design concern.

## Keybindings (v2)

Dashboard v2 ships a full keyboard-driven flow plus mouse-equivalent buttons
for every action — every keybind has a clickable counterpart, so you can drive
the UI with either input.

| Key | Action |
|---|---|
| `1`/`2`/`3`/`4` | Open Current Week / Forecast / Trend / Sessions modal |
| `Tab` + `Enter` | Focus a panel and open its modal |
| `Esc` | Close modal / settings / collapse filter-search |
| `r` | Force immediate sync |
| `f` | Focus the Sessions filter input |
| `/` | Focus the Sessions search input |
| `n` / `N` | Next / previous search match |
| `s` | Open settings overlay |
| `q` | Close the tab (best-effort) |
| `?` | Toggle help overlay |

Every keybind has a clickable equivalent: footer pills are real buttons, the
sync chip posts to `/api/sync`, panel bodies and session rows open their
modals on click.

## Sync chip / 'r' shortcut

Clicking the sync chip in the upper-right (or pressing `r`) triggers an
explicit OAuth refresh from Anthropic followed by a snapshot rebuild —
the same path as `cctally refresh-usage`. The chip spins for ~1 second
on a typical click while the OAuth fetch completes.

Rate limit (HTTP 429 from Anthropic): silent fallback. The dashboard keeps
showing the last-known data without flashing an error.

Other refresh failures (no OAuth token, network error, parse error,
record-usage error): the chip flashes red for 3 seconds. The console
shows the warning code; check `cctally refresh-usage` from the CLI for
details.

The periodic background sync runs every 5 seconds (configurable via
`--sync-interval`) and only does a snapshot rebuild — it never calls
the OAuth API, so Anthropic's rate limit is not affected by background
ticks. Only chip clicks / `r` presses trigger OAuth fetches.

## Manual verification (post-v2)

Before merging or releasing v2 changes, run through:

1. Open dashboard → press `1`/`2`/`3`/`4` → all four modals open/close.
2. Click each panel body → same modals open.
3. Tab through panels → focus ring visible; Enter opens the focused panel's modal.
4. Click filter icon → input appears; type `opus` → rows filter; blur → chip shown; reload page → filter persists.
5. Click magnifier → type `claude` → `n`/`N` cycle matches; count shown.
6. Click a model chip in a row → filter prepopulates with that model.
7. Click a session row → Session modal fetches and renders; close restores focus.
8. Click sync chip → `POST /api/sync` fires; chip pulses "syncing…"; next SSE tick clears.
9. `s` → settings overlay; change sort default + sessions-per-page; save; reload → persisted.
10. `q` → `window.close()` attempted (likely blocked by browser; fallback toast shows).

## Endpoints

| URL | Purpose |
|---|---|
| `GET /` | The dashboard HTML |
| `GET /static/*` | CSS / JS / SVG sprite |
| `GET /api/data` | One-shot JSON snapshot (curl-friendly) |
| `GET /api/events` | SSE stream — full snapshot on every sync tick |
| `GET /api/session/:id` | Per-session detail (v2) — powers the Sessions modal |
| `POST /api/sync` | OAuth refresh + snapshot rebuild (chip / `r`). 204 on clean success; 200 + `{warnings:[{code: ...}]}` when refresh-usage returned a non-`ok` status (`rate_limited`, `no_oauth_token`, `fetch_failed`, `parse_failed`, `record_failed`); 503 if another sync is in flight. Origin-vs-Host parity CSRF (see [Threat model](#threat-model)). |

Sort-pill click (top-right of Sessions panel) cycles the session sort;
the choice persists in `localStorage`. The Settings overlay (`s`) stores
the default sort + remembered filter term in the same `localStorage` slot.

## Shutdown

`Ctrl-C` in the terminal where you launched the dashboard. The server
cleans up within ~1 s. The browser tab will show a "disconnected"
state in the sync chip.

## Deferred

Token-based authentication for off-LAN exposure (binding beyond the local
network) is a deferred design concern. The default loopback bind
(`127.0.0.1`) plus Origin-vs-Host parity CSRF is the current contract; LAN
exposure is opt-in (see [LAN access](#lan-access)). Remote use across the
internet (or untrusted shared networks) will need a stronger auth story
first. See [Threat model](#threat-model) for the LAN-bind caveat.
