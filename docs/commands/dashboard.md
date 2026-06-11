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
| `5` | Open Projects modal |
| `0` | Open the panel at position 10 (the 10th-slot panel in your current order — `alerts` in default order; vim-style "10 wraps to 0") |
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

## Projects panel + modal

The Projects panel renders the current subscription week's top-5 projects
sorted by cost descending, with a horizontal-bar visualization, an
attributed `Used %` per project, and a magenta accent. A "+N more" tail
row collapses everything below the top 5. Cross-nav from the Sessions
panel: clicking a project name in a Sessions row opens the Projects
modal pre-expanded on that project's drill.

Clicking the panel header or any row opens the **Projects modal**. The
modal shows a `1w` / `4w` / `8w` / `12w` window-pill selector, a
stacked-area trend chart over the selected window, a 7-column per-project
table (cost desc by default), and an in-place per-project drill into
model breakdown + recent sessions. Clicking a session in the drill
opens the Session modal (replaces, not stacks). The modal's share
affordance routes through the same `_build_project_snapshot` kernel as
the panel and carries the active `windowWeeks` into the share flow.

The panel sits at index 4 of `DEFAULT_PANEL_ORDER` and is reachable via
keyboard shortcut `5` (or the position-N digit if you have reordered
panels). New users land it there automatically; upgraders are migrated
in-place by `reconcilePanelOrder` so any custom order is preserved.

Lazy detail endpoint: `GET /api/project/<key>?weeks=N` returns the
window-scoped trend points + drill payload (top models, recent
sessions). Mirrors the SessionModal stale-while-revalidate pattern —
the modal renders from the envelope's top-N summary first, then
hydrates the full drill on demand.

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
| `GET /api/conversations` | Conversation-viewer browse rail — all-history per-session rows with per-session cost. Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). |
| `GET /api/conversation/<id>` | Conversation-viewer reader — one session's deduped, turn-grouped messages with cost-once. Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). |
| `GET /api/conversation/search` | Conversation-viewer cross-session FTS search (`?q=…`; LIKE fallback when FTS5 is unavailable). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). |
| `POST /api/sync` | OAuth refresh + snapshot rebuild (chip / `r`). 204 on clean success; 200 + `{warnings:[{code: ...}]}` when refresh-usage returned a non-`ok` status (`rate_limited`, `no_oauth_token`, `fetch_failed`, `parse_failed`, `record_failed`); 503 if another sync is in flight. Origin-vs-Host parity CSRF (see [Threat model](#threat-model)). |

> `/api/conversation/search` is matched **before** the `/api/conversation/<id>`
> reader, so `search` is never treated as a session id. `/api/data` carries a
> per-request `transcriptsEnabled` boolean (the same gate value) so the client
> only offers the conversation UI when the routes would actually serve.

Sort-pill click (top-right of Sessions panel) cycles the session sort;
the choice persists in `localStorage`. The Settings overlay (`s`) stores
the default sort + remembered filter term in the same `localStorage` slot.

## Conversation viewer endpoints (Plan 2)

The three `/api/conversation*` routes serve **raw transcript prose** read from
the local `cache.db`. Because that prose is far more sensitive than the
aggregate usage numbers the rest of `/api/*` exposes, those routes sit behind a
fail-closed privacy gate that is independent of the general LAN bind.

**Loopback-default gate.** By default the conversation routes are served **only
over loopback** — even when the dashboard itself is LAN-bound (`--host 0.0.0.0`
/ `dashboard.bind lan`). A request whose `Host` header is not a loopback
address gets `403`. To serve transcripts on the LAN you must additionally opt
in:

    cctally config set dashboard.expose_transcripts true

(boolean key — see [`config.md`](config.md#allowed-keys); default `false`).

**Host anti-DNS-rebinding allowlist.** The gate composes two checks: the bind
must permit transcripts at all (loopback always; LAN only under the opt-in),
**and** the request's `Host` header must pass an anti-DNS-rebinding allowlist:

| Bind | `expose_transcripts` | Request `Host` | Result |
|---|---|---|---|
| loopback | (any) | loopback (`127.0.0.1` / `[::1]` / `localhost`) | served |
| loopback | (any) | a hostname / LAN IP | `403` |
| LAN | `false` (default) | anything | `403` |
| LAN | `true` | an **IP literal** (`192.168.0.9`, `[fe80::1]`) | served |
| LAN | `true` | a **hostname** (`box.local`, `evil.example.com`) | `403` |

Under the opt-in only an **IP-literal** `Host` is accepted: a LAN device
reaches the dashboard at its IP literal (which can't be DNS-rebound), while a
rebinding *domain* (any hostname) is rejected. A missing/empty `Host` fails
closed. `/api/data`'s `transcriptsEnabled` is computed with the **same**
per-request predicate, so a request the conversation routes would `403` always
reports `transcriptsEnabled=false` — the client never offers a button that
would 403.

**At-rest hardening.** Since `cache.db` now holds plaintext conversation prose,
`open_cache_db` best-effort `chmod`s the data dir to `0700` and `cache.db` to
`0600`; the `-wal` / `-shm` sidecars (materialized only on the first write) are
hardened to `0600` at the end of the `sync_cache` write transaction. The chmod
is best-effort: a failure (e.g. an exotic filesystem) logs and continues rather
than aborting.

**Enriched data contract (#177).** The reader payload carries a richer, additive contract for downstream tool-rendering work. Each assistant `tool_call` block now exposes the full **structured** tool input as `input` (the original argument object, size-bounded so a pathological input can't bloat the payload) alongside an `input_truncated` flag set when any bound clipped a value; the legacy `input_summary`/`preview` fields stay for back-compat. A tool result carries `full_length` — the pre-clip character count of the underlying output — beside the existing capped `text`/`truncated`, so a "showing X of Y" affordance can be built without the full body (the cap was also raised to 16 000 characters). Each assistant turn item carries a `tokens` breakdown (`input`/`output`/`cache_creation`/`cache_read`) drawn from the **same** deduped `session_entries` row its `cost_usd` comes from — note these are the same *source row*, not an arithmetic identity: when a vendor supplies a raw cost it overrides the token-derived math, so never assume `cost == f(tokens)`. Assistant items also surface `stop_reason` and `attribution_skill` / `attribution_plugin` (the skill/plugin that drove the turn) when present, omitted when absent. A parser-populated `search_aux` column plus a parallel `conversation_fts_aux` index quietly denormalize tool input, tool results, and thinking text for future tool-content search — this session builds and maintains the index but adds no search query over it yet. Every one of these fields is additive: the existing client keeps reading the prior shape unchanged, and the new fields land on existing history the next time the cache syncs (a one-time, lossless re-ingest of the re-derivable conversation cache, gated by the distinct `conversation_reingest_enrichment_pending` flag that migration `007` sets).

**Subagent thread cards (#166).** On modern transcripts the reader surfaces a subagent's *kind* in the thread-card eyebrow (`SUBAGENT · <kind>`, e.g. `SUBAGENT · Explore`) and a dim second line with its result meta — tokens, wall-clock duration, tool-use count, and status (a bare `✓` on success; `✕ error` or `⚠ <status>` spelled out on failure). The kind and meta come from the spawn `Task`/`Agent` `subagent_type` joined to the record-level `toolUseResult` in the query kernel. Older transcripts that predate the capture lack the linkage, so their cards gracefully fall back to the title-only rendering.

**Decision & planning tool cards (#177 S2).** Three decision/planning tools now render as dedicated semantic cards instead of the generic JSON tool chip. `AskUserQuestion` becomes a Q&A card: each question with its header tag and single/multi-select mode, every option laid out, and the option(s) you actually chose highlighted in green (a free-text answer that matches no option renders in its own "your answer" block). `TodoWrite` becomes a checklist card, collapsed by default to a one-line progress preview (`done / total` with a mini progress bar and the current in-progress item), expanding to the full list with completed items struck through, the in-progress item flagged in amber, and pending items dimmed. `ExitPlanMode` becomes a plan card that renders the proposed plan as Markdown (clamped with a "Show full plan ↓" reveal) and badges the outcome as Approved, Rejected, or a neutral Responded — never defaulting to Approved on an ambiguous result. Every other tool keeps the generic chip. The chosen-answer highlight prefers the structured answers captured at ingest and falls back to parsing the harness result string on older transcripts; each card is a `<details>`, so the reader's collapse-all keystrokes still reach it.

**Injected (`isMeta`) content.** Lines Claude Code injects into a transcript that you did not type — a skill's body (when an assistant turn invokes the `Skill` tool, or a `SessionStart` skill), git-context blocks, "Continue from where you left off.", pasted-image placeholders, slash-command plumbing — are never rendered as a "You" prompt. They collapse into quiet, collapsed-by-default disclosures: slash-command plumbing keeps the `System marker` pill, and everything else becomes a neutral `Injected context` pill. A skill body invoked via the `Skill` tool now **folds into its Skill tool chip** — the chip itself expands to the rich-Markdown body (the redundant "Launching skill: <name>" result is dropped), so the skill reads as one nested unit inside the turn rather than a detached pill below it. A `SessionStart` skill (no `Skill` tool call) keeps the standalone `Skill content · <name>` pill (the name is the skill's directory basename; its body still renders as full Markdown when expanded). Injected bodies are excluded from derived titles and full-text search. The classification — and the skill-body fold — land on existing history the next time the cache syncs (a one-time, lossless re-ingest of the re-derivable conversation cache).

**Reader UX (#175, #176).** While a conversation's first page loads, the reader shows an animated spinner (suppressed under `prefers-reduced-motion`). When you scroll deep into a tall turn so its start is off the top of the reading column, an unobtrusive floating "↑ Top of turn" button appears at the bottom-right of the reader; clicking it scrolls that turn back to its start (reduced-motion aware). Nothing floats over the reading column itself — the button is anchored clear of the bottom-center "↓ N new" pill and is hidden again on a session switch. The assistant model renders as a colored `.chip` (matching the rest of the dashboard) rather than plain text, with no chip shown when the model is unknown. Finally, the open conversation **live-tails**: once you've paged to the end, new turns from an active session appear on each refresh tick with no manual reload — the reader sticks to the newest turn if you're already at the bottom, or surfaces a floating "↓ N new" pill (click to jump to the latest) if you've scrolled up, and the cost/model totals update along with the new turns. Live-tail engages only after the conversation is fully paged; already-loaded turns are not re-fetched.

### Deep-linking & per-turn permalinks

The conversation reader reflects its state into the URL hash: `#/conversations/<sessionId>` for an open conversation and `#/conversations/<sessionId>/<turnUuid>` for a specific turn. Reloading or using the browser Back/Forward buttons restores the conversation and re-lands the turn jump. Hovering any turn — prose, tool-result, or system-marker — reveals a link button that copies a permalink straight to that turn and points the address bar at it; on prose turns it sits beside the copy button, and on the collapsible tool-result and system-marker chips it sits in the summary row. These links are local-first: a permalink is relative to your dashboard's origin and only resolves for someone who can already reach it (loopback, or your LAN when started with `--host 0.0.0.0`) — it is not a public, shareable-off-host URL.

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
