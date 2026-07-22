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
| `f` | Dashboard: focus the Sessions filter input. Conversations view: focus the conversation-list search input (works even with a reader open, so you can search the list without pressing `Esc` first) |
| `/` or `⌘F` / `Ctrl+F` | Open the conversation find bar when a reader is open; otherwise focus the rail / Sessions search input. `⌘F` / `Ctrl+F` suppresses the browser's native find bar only inside the Conversations workspace (and only when no modal/overlay/input is up) |
| `n` / `N` | Next / previous search match — and, with the find bar open and its input blurred, step to the next / previous in-conversation match |
| `s` | Open settings overlay |
| `q` | Close the tab (best-effort) |
| `?` | Toggle help overlay |
| `v` (dashboard) | Cycle the source selector Claude → Codex → All. View-scoped — in the Conversations reader `v` cycles the focus mode instead (below) |
| `o` | Toggle the conversation outline sidebar (reader) |
| `v` (reader) | Cycle the reader focus mode through the four primary modes (All → Chat → Prompts → Errors); from a "▾ More" mode (Edits/Bash/Subagent) it returns to All |
| `e` / `E` | Reader: jump to next / previous error turn |
| `u` / `U` | Reader: jump to next / previous prompt |
| `b` / `B` | Reader: jump to next / previous subagent thread |
| `p` / `P` | Reader: jump to next / previous plan/question turn |
| `m` / `M` | Reader: jump to next / previous **compaction** landmark |
| `i` / `I` | Reader: jump to next / previous **bookmark** |
| `t` | Reader: toggle a bookmark on the current turn (the explicit-selection pin, or the topmost-visible turn) |
| `j` / `k` | Reader: move the focused-turn cursor down / up |
| `a` | Reader: jump directly to the **last** (most-recent) prompt |
| `L` | Reader: jump directly to the **last** (most-recent) error turn |
| `End` | Reader: jump to the conversation's latest turn (resets to the tail page in one request, then lands and flashes it) — suppressed while a filter input or modal is focused |

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
ticks. A usage observation selected by the statusline reducer appears in the
next normal rebuild and is sent over the existing SSE stream; no dashboard
action or dashboard-owned OAuth request is involved. The separate 30-second
Claude status-line timer may perform one account-wide, detached OAuth
confirmation when selected usage is stale; it shares the hook throttle and
`429` backoff. Within the dashboard itself, only chip clicks / `r` presses
trigger OAuth fetches.

## Startup sync

The dashboard binds its HTTP port immediately and serves the current cached snapshot; the first full sync (and any pending one-time conversation-enrichment reingest) runs in the background and is pushed to the page over SSE when it completes. On a large transcript history the background reingest is resumable — interrupting the dashboard mid-sync and relaunching resumes where it left off rather than restarting. To start without any sync, use `--no-sync`; to consume a pending reingest in one foreground pass, run `cctally cache-sync` (or `cache-sync --rebuild`).

## Transcript retention

The dashboard's independent conversation sync thread also runs an automatic, throttled transcript-retention prune — at most once every 24 hours — that removes conversation transcripts older than `conversation.retention_days` (default 90) from `conversations.db`. This bounds transcript growth without delaying the compact accounting/quota sync; only transcript rows are pruned, and everything pruned is re-derivable from JSONL. Set `cctally config set conversation.retention_days off` to keep transcripts forever, or a positive integer to change the window. The prune is gated off under `--no-sync`. Current conversation stores use SQLite `INCREMENTAL` auto-vacuum; compact a legacy store explicitly with `cctally db vacuum --db conversations` after stopping the dashboard. You can also prune on demand with [`cctally cache-sync --prune-conversations`](cache-sync.md#--prune-conversations).

## Hero modal — history navigation

The Current Week / Current Cycle modal (opened with `1`, or by clicking the hero strip) navigates history in place. The week/cycle chip is flanked by `‹`/`›` buttons that step through provider billing cycles bounded by actual reset or re-anchor events—not calendar weeks or nominal seven-day buckets. A reset starts a new navigable cycle even when it arrives before the nominal deadline. It applies to both providers, and in the side-by-side **All** view each provider section navigates independently (click-only).

Within a selected cycle, the block navigator contains every retained five-hour block whose interval overlaps that cycle's exact half-open interval, ordered oldest to newest. The current cycle defaults to its active block; a historic cycle defaults to its last retained block. `ArrowLeft`/`ArrowRight` or the block buttons move one block per action, including when the first action must fetch detail before applying its direction. Boundary-straddling and milestone-empty blocks remain navigable. A cycle with no retained five-hour data hides the navigator as honest data absence; Codex automatically gains the same navigator whenever retained native 300-minute observations exist.

In the single-provider variants, `ArrowUp`/`ArrowDown` step to the newer/older cycle. Each selected cycle renders one chronological per-percent ledger from one provider-owned quota identity; reset-defined ledgers are never concatenated. Cycles with no recorded milestones show an explicit empty state. Historic cycles are read-only, so the Share action is hidden while one is displayed (it always shares the current cycle otherwise).

A compact per-provider navigation index rides the live SSE envelope (`current_week.week_index` for Claude, `sources.codex.data.quota.cycle_index` for Codex), built only on non-idle snapshot rebuilds. The complete payload for one selected cycle—its single opaque milestone segment and exact overlapping five-hour block list—is fetched on demand from:

```
GET /api/milestones/<source>/week/<key>
```

`<source>` ∈ `claude | codex`. For both providers, `<key>` is an opaque server-issued `milestone_cycle:*` key obtained from the envelope index; never hand-construct it or expose provider identity fields. The response is snake_case JSON with `Cache-Control: no-cache`; the client reuses it client-side keyed by `(source, key, detail_stamp)`. A malformed key or source returns `400`; a key that no longer resolves returns `404` with a machine-readable `{ code: "unknown_key", reason }` body (`reason` ∈ `pruned | rebuild_pending | projection_incoherent | unknown`).

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
| `GET /api/data` | One-shot JSON snapshot (curl-friendly). It retains the legacy Claude fields and appends source-aware backend fields (`source_schema_version`, `default_source`, `source_order`, `sources`). |
| `GET /api/events` | SSE stream — full snapshot on every sync tick |
| `GET /api/session/:id` | Per-session detail (v2) — powers the Sessions modal |
| `GET /api/source/<source>/<resource>/<opaque-key>` | Bounded provider-owned detail for `source ∈ {claude,codex}` and `resource ∈ {session,project,block}`. Codex reads are relational and `sync=False`; they expose native model/token, project/session, and quota observation/milestone/forecast data without walking rollout files. `all` has no physical details. Invalid/unavailable pairs return `400 source_capability_unavailable`; a valid missing key returns `404 source_resource_not_found`; neither response exposes raw provider identities or exception text. |
| `POST /api/share/render`, `POST /api/share/compose` | Server-rendered share artifact/composition. Optional backend `source ∈ {claude,codex,all}` is source-bearing in the digest and response; omission remains the legacy Claude contract. Codex report panels use the native share vocabulary and configured calendar-week boundary. `all` composes labelled provider sections rather than blending quota. |
| `GET`/`POST` `/api/share/presets`, `GET`/`POST`/`DELETE` `/api/share/history` | Saved share recipes persist the backend source; legacy source-less records resolve to Claude on read without being rewritten. |
| `GET /api/debug/backend` | Loopback-only diagnostic with safe per-source table counts, availability, and opaque data versions. It never returns paths, roots, logical limits, conversation keys, or raw exceptions. |
| `GET /api/conversations` | Conversation-viewer browse rail — all-history per-session rows with per-session cost. `?sort=` (#217 S4) ∈ `{recent (default), oldest, cost, messages, project}` orders the list; an unknown value falls back to `recent` (the endpoint stays lenient). Optional server-side filter params (`date_from`/`date_to`/`projects`/`cost_min`/`cost_max`/`rebuild_min`); a malformed value is `400`. The `page` object carries an additive `sort_degraded: true` when a `cost`/`project` sort fell back to `recent` order during the brief non-authoritative indexing window (beside `filter_degraded`). See [Rail sort](#rail-sort) and [Browse filters](#browse-filters). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). |
| `GET /api/conversations/facets` | Conversation-viewer filter facets — sorted distinct project labels with per-label conversation counts, plus per-model-family session counts (fold-then-count), for the Filters popover's project + model multi-selects. Project counts are a rollup GROUP BY; the model-family counts are an index-only scan of `conversation_messages(model, session_id)` (#301). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). See [Browse filters](#browse-filters). |
| `GET /api/conversation/<id>` | Conversation-viewer reader — one session's deduped, turn-grouped messages with cost-once. Pages with `?after=<id>` (forward), `?before=<id>` (backward), or `?tail=1` (open at the bottom — the last page in one request), plus `?limit=N` (default `500`, clamped `1`–`1000`); the three cursors are mutually exclusive (supplying more than one is `400`). The `page` object carries `next_after`/`has_more` (more newer turns) and the additive `prev_before`/`has_prev` (more older turns), so a client can page both directions; a stale cursor yields an empty page (never a head/tail re-serve). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). See [Reader pagination](#reader-pagination-217-s2). |
| `GET /api/conversation/search` | Conversation-viewer cross-session FTS search (`?q=…&kind=…&limit=…&offset=…`; LIKE fallback when FTS5 is unavailable). `kind ∈ {all, prompts, assistant, tools, thinking, title, files}` (default `all`; an unknown value is `400`). `kind=title` searches the AI-generated session titles and returns one session-level hit per match, anchored to the session's first turn. `kind=files` searches the write-class file paths each session edited/created and returns one hit per distinct `(session, file path)`, anchored to that path's most-recent touch. Also accepts the same server-side filter params as the browse rail (`date_from`/`date_to`/`projects`/`cost_min`/`cost_max`/`rebuild_min`), applied as a session-scope restriction across every kind; a malformed filter value is `400`. Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). See [Search depth](#search-depth-177-s6) and [Browse filters](#browse-filters). |
| `GET /api/conversation/<id>/find` | Conversation-viewer in-conversation find (#177 S6) — document-ordered rendered-turn anchors for one open session (`?q=…&kind=…`). `kind ∈ {all, prompts, assistant, tools, thinking}` — the cross-session-only `title` and `files` kinds are **not** valid here and return `400`. `?regex=1` / `?case=1` (#217 S4) switch to a physical-row scan (`mode: "regex"` / `"like"`, always `search_depth: "full"`); an invalid regex is pre-validated → `400`, never `500`. Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). See [Search depth](#search-depth-177-s6). |
| `GET /api/conversation/<id>/payload` | On-demand "load full tool payload" (#178) — re-reads the source JSONL line to serve the un-capped tool `result` or `input` for one `tool_use_id` (`?tool_use_id=…&which=result\|input`). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). |
| `GET /api/conversation/<id>/media` | On-demand media bytes (#177 S4) — re-reads the source JSONL line to decode and serve one inline image or PDF (`?tool_use_id=…&index=N` for tool-result media, or `?uuid=…&index=N` for user-content media). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2) **plus** a cross-origin Fetch-Metadata check; see [Conversation viewer endpoints](#conversation-viewer-endpoints-plan-2). |
| `GET /api/conversation/<id>/events` | Conversation-viewer live-tail SSE stream — watches only the open session's JSONL file(s) and emits `event: tail` within ~1s of growth (with `: keep-alive` comments while idle). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). See [Live-tail](#live-tail). |
| `GET /api/conversation/<id>/export` | Conversation-viewer whole-session Markdown export (#217 S5, F1/F5) — runs the full server-side assembly and serializes it to Markdown for one `?scope=` ∈ `{all, prompts, chat, recipe}` (default `all`; an unknown value is `400`, validated in the handler before the kernel — never a `500`). **#281 S4:** an optional `?anonymize=1` scrubs the body (project paths/labels, home, username, and documented secret patterns); it is strict-parsed (at most once, literal `0`/`1` — a blank/duplicate/other value is a `400` before the kernel), and its absence is byte-identical to the pre-#281 raw export. Serves `text/markdown; charset=utf-8`; unknown session → `404`. Behind the [transcript gate](#conversation-viewer-endpoints-plan-2) — the **same** `_require_transcripts_allowed()` fail-closed gate as the sibling reader routes, with **no** extra CSRF check (parity with `/payload`/`/outline`/`/find`). |
| `GET /api/conversation/<id>/anon-map` | Conversation-viewer client scrub plan (#281 S4) — returns the JSON wire form of the anonymization plan (`{tokens, patterns}`) so the reader can anonymize per-card copies with the same rules the server export uses. The plan is global, but the route probes existence and `404`s on an unknown session (sibling envelope discipline); it exposes only tokens the same gated client already sees raw. Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). |
| `GET /api/conversation/<id>/prompts` | Conversation-viewer session-comparison prompt bodies (#217 S7, F10) — returns `{session_id, prompts: [{uuid, text}]}`, the ordered **main-thread** human prompts (the same `subagent_key == null && !is_sidechain` + non-empty-text predicate the `recipe`/`prompts` export reuses) each with its **full** text and turn uuid, in document order. Used by the [Session comparison](#session-comparison-217-s7) view for lazy inline full-prompt expansion. Unknown session → `404`. Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). |
| `POST /api/sync` | OAuth refresh + snapshot rebuild (chip / `r`). 204 on clean success; 200 + `{warnings:[{code: ...}]}` when refresh-usage returned a non-`ok` status (`rate_limited`, `no_oauth_token`, `fetch_failed`, `parse_failed`, `record_failed`); 503 if another sync is in flight. Origin-vs-Host parity CSRF (see [Threat model](#threat-model)). |

> `/api/conversation/search`, `/api/conversation/<id>/payload`,
> `/api/conversation/<id>/media`, `/api/conversation/<id>/find`,
> `/api/conversation/<id>/events`, `/api/conversation/<id>/export`, and
> `/api/conversation/<id>/prompts` are all
> matched **before** the `/api/conversation/<id>` reader, so neither `search`
> nor a `…/payload` / `…/media` / `…/find` / `…/events` / `…/export` / `…/prompts`
> suffix is
> ever treated as a session
> id. `/api/data` carries a
> per-request `transcriptsEnabled` boolean (the same gate value) so the client
> only offers the conversation UI when the routes would actually serve.

Sort-pill click (top-right of Sessions panel) cycles the session sort;
the choice persists in `localStorage`. The Settings overlay (`s`) stores
the default sort + remembered filter term in the same `localStorage` slot.

The Settings overlay also carries an **Update channel** toggle (Stable /
Beta) — the release channel `cctally update` tracks (see
[`update.md`](update.md#beta-channel)). It seeds from the SSE envelope's
`update.configured_channel` (derived directly from config, not cached state)
and persists via `POST /api/settings` like the other mirrored keys. On the
beta channel the update badge/modal show a `(beta)` marker and the exact
resolved install command (`cctally@X.Y.Z`); Homebrew always tracks stable.

### Dual-form conversation routes (#294 S7)

The conversation routes are source-qualified **in place** — no new namespace. On the entity routes (`/api/conversation/<id>` and its `…/outline`, `…/prompts`, `…/find`, `…/payload`, `…/export`, `…/anon-map`, `…/media`, `…/events` suffixes), an id beginning `v1.` opts into the provider-neutral dispatch and returns the neutral envelope: `ok` → `200` JSON, `normalization_pending` → `200` JSON (a Codex cache that predates migration `025`), `not_found` (including a malformed `v1.*`) → `404` JSON, payload `gone` → `410`, `…/export` `ok` → `200 text/markdown`, and Codex `…/media` → `404 {"status":"capability_unsupported","source":"codex"}` (Codex media is capability-gated until real media data is shown to exist). Any other id — a bare Claude `sessionId`, UUID-shaped or not — takes the legacy handler unchanged and never touches the resolver, so existing behavior and bytes are byte-identical. The three collection routes (`/api/conversations`, `/api/conversations/facets`, `/api/conversation/search`) gain a strict `?source={claude,codex}`: exactly one literal value, a per-route parameter whitelist (blank/duplicate/`all`/unknown, a legacy-only axis with `source` present, or an out-of-range `limit` is a `400`); absent `?source=`, the legacy lenient parsing is unchanged. Browse cursors are raw conversation keys (echoed verbatim); the search cursor is unpadded base64url. The transcript privacy gate stays the first act of every handler, including the `capability_unsupported` and `normalization_pending` answers.

The `/api/conversation/<v1key>/events` live-tail preflights (privacy gate → resolve → Codex normalization authority → existence) and answers a non-`ok` result as plain JSON **before** any SSE bytes; only on `ok` does it commit SSE headers and stream `conversationKey`-framed `ready`/`tail`/keep-alive frames (a bare Claude stream keeps its `sessionId` frames byte-identical). The Codex stream uses targeted ingest and a budgeted directory-frontier child discovery, so a child thread spawned mid-watch joins the stream via a `tail` refetch. `--no-sync` passivity and the `dashboard.live_tail` opt-out apply to both providers.

### Source-aware backend (S4)

S4 makes the dashboard server publish Claude, Codex, and presentation-only
`all` states atomically. It does not add a visible source selector: S5 owns the
React control and browser interaction. The source-aware endpoints above are the
backend contract the S5 client reads.

When retained Codex accounting has incomplete project metadata, the Codex state
is `partial` but still `fresh`: its accounting, quota, budget, sessions, and
forensics remain current while **Projects alone** is unavailable. The public
`codex_metadata_incomplete` warning reports the safe row count and directs you
to run `cctally cache-sync --source codex --rebuild`; replay repairs historical
metadata where the retained rollout permits it. A fresh partial provider still
contributes compatible USD and total-token tiles to `All`. By contrast, a
generic `source_build_failed` remains a generic public warning; its traceback
is recorded only in the private `cctally.dashboard` log.

### Source selector and source-aware UX (S5)

The Header hosts a three-state `Claude | Codex | All` selector (a radiogroup — Arrow keys move focus and selection, Home/End jump to the ends; the `v` shortcut cycles Claude → Codex → All). The choice persists in `localStorage` under `cctally:dashboard:source` (default `claude`) and is a pure client re-selection over the already-delivered `sources` bundle — the store never waits for or reconciles it against an envelope, and panel subtrees re-key on switch so no mixed-source frame is ever shown. This control is visible in the dashboard workspace; Conversations exposes the persisted selection through its own rail control.

Claude and Codex use one canonical hero composition and metric order. Under Codex, the active native **seven-day (10,080-minute) reset cycle** supplies Week Usage, the reset countdown, cycle spend, `$ / 1%`, forecast-at-reset, the `$ / 1%` delta against the previous retained native cycle, and snapshot age; the independent five-hour (300-minute) limit fills the same optional 5-Hour slot. The values are derived only from retained Codex quota and cycle-bounded accounting. If the cache has accounting but no single coherent active seven-day boundary, those accounting slots render unavailable rather than showing a misleading zero. `All` remains a separate composition: it shows the bundle's combined USD and total-token tiles only when the server publishes a coherent `combined` object, with quota always rendered side by side and never as a blended gauge.

Capability gating (per the S4 manifest) hides — rather than zero-fills — any panel a source does not publish. The Help overlay (`?`) lists intentional omissions and points to the provider-native equivalent; Codex reset forecasts also feed the canonical hero, while its computed token-reuse detail lives in Cache Report. A runtime-degraded or stale source renders an explicit warning chip in place (never hidden), and a source with no successful snapshot yet renders "no successful snapshot yet". A fresh partial source degrades only the warning's affected domain (for example, incomplete project metadata leaves Daily and quota usable); a source-wide ingest/read-model warning degrades every applicable domain. The source-status chip uses concise visible copy while preserving its full diagnostic in its title and accessible label.

The visible-panel order (digit shortcuts, drag-reorder, Help's panel list) is derived from the persisted full order filtered through the active source's gating; the persisted order is never rewritten by a source switch, and a reorder in a filtered view maps back into the full order preserving hidden panels' positions.

Alerts are source-aware. The Recent-alerts panel and modal show the active source's own rows (Claude axes; Codex `codex_budget` / projected / quota rows with native labels; `All` a source-labelled union — never merged). Toasts fire for alerts of every source regardless of the active selection (an alert is a notification), each carrying a source chip; they are read only from the per-source projections, so a Codex budget alert can never double-toast. The Settings overlay (`s`) groups the persistable toggles into a global Notifications group (the notifier backend, shared across vendors), a Claude group (threshold + projected-weekly + a labelled Claude-budget subgroup), and a Codex group (the mirrored `budget.codex.*` toggles) — with a CLI pointer noting Codex quota-threshold rules are not configurable here. Regrouping is presentation-only; the `POST /api/settings` body is unchanged.

### Conversation source selector and mixed-source reader (S8)

Conversations has its own `Claude | Codex | All` control in the browse rail over
the same persisted source selection used by the dashboard header. Claude and
Codex rows use opaque qualified conversation keys and the same reader surface.
The reader preserves Codex-native reasoning, cached-input, tool-event, thread,
payload, find, export, and live-tail semantics instead of relabelling them as
Claude fields. Permalinks and browser-local reading positions/bookmarks include
the source-qualified identity, so equal native UUIDs from different providers
or Codex roots remain isolated.

Card-ready Codex plan updates, native web searches, MCP calls, and agent-control
operations render through the same conversation-card language as their Claude
counterparts while keeping the provider's operation names and states. Expand a
card to inspect retained request/result details or load its raw payload. A spawn
offers `Open child` only when the retained record contains a uniquely proven
opaque child conversation; unproven or ambiguous spawns remain unlinked. Local
filesystem and placeholder image targets embedded in Codex Markdown display a
`local screenshot unavailable` badge and are never requested by the browser.

`All` is a client composition: it fetches the strict Claude and Codex
collections separately, merges them by activity with qualified-key tie-breaking,
and source-labels every row. It never sends `source=all`. Provider-local filters
and sorting are disabled in this view with an explanatory note; switch to a
single source to use them.

A comparison may cross providers. Its header labels each run's source; cost and
prompt count are compared normally, while tokens, errors, and files remain
side-by-side provider-specific values and duration is marked unavailable when
the providers differ. The comparison copy action fetches each whole export and
emits separate `Run A · <source>` and `Run B · <source>` sections, never a
combined transcript body.

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

**At-rest hardening.** Since `conversations.db` holds plaintext conversation prose,
`open_conversations_db` best-effort `chmod`s the data dir to `0700` and `conversations.db` to
`0600`; the `-wal` / `-shm` sidecars (materialized only on the first write) are
hardened to `0600` at the end of the conversation sync transaction. The chmod
is best-effort: a failure (e.g. an exotic filesystem) logs and continues rather
than aborting.

**Enriched data contract (#177).** The reader payload carries a richer, additive contract for downstream tool-rendering work. Each assistant `tool_call` block now exposes the full **structured** tool input as `input` (the original argument object, size-bounded so a pathological input can't bloat the payload) alongside an `input_truncated` flag set when any bound clipped a value; the legacy `input_summary`/`preview` fields stay for back-compat. A tool result carries `full_length` — the pre-clip character count of the underlying output — beside the existing capped `text`/`truncated`, so a "showing X of Y" affordance can be built without the full body (the cap was also raised to 16 000 characters). Each assistant turn item carries a `tokens` breakdown (`input`/`output`/`cache_creation`/`cache_read`) drawn from the **same** deduped `session_entries` row its `cost_usd` comes from — note these are the same *source row*, not an arithmetic identity: when a vendor supplies a raw cost it overrides the token-derived math, so never assume `cost == f(tokens)`. Assistant items also surface `stop_reason` and `attribution_skill` / `attribution_plugin` (the skill/plugin that drove the turn) when present, omitted when absent. Earlier revisions denormalized tool input, tool results, and thinking text into a parser-populated `search_aux` column with a parallel `conversation_fts_aux` index; #177 S6 replaced that groundwork with the split `(text, search_tool, search_thinking)` index the search/find surfaces query directly — see [Search depth](#search-depth-177-s6). Every one of these fields is additive: the existing client keeps reading the prior shape unchanged, and the new fields land on existing history the next time the cache syncs (a one-time, lossless re-ingest of the re-derivable conversation cache, gated by the distinct `conversation_reingest_enrichment_pending` flag that migration `007` sets).

**Subagent thread cards (#166).** On modern transcripts the reader surfaces a subagent's *kind* in the thread-card eyebrow (`SUBAGENT · <kind>`, e.g. `SUBAGENT · Explore`) and a dim second line with its result meta — tokens, wall-clock duration, tool-use count, and status (a bare `✓` on success; `✕ error` or `⚠ <status>` spelled out on failure). The kind and meta come from the spawn `Task`/`Agent` joined to the record-level `toolUseResult` in the query kernel: the spawn's `subagent_type` carries the kind when present, and a subagent dispatched without an explicit `subagent_type` (the default general-purpose case) is still recognized by its tool name and labelled `general-purpose`, with its description and token/duration/tool usage shown the same way (#225). Older transcripts that predate the capture lack the linkage, so their cards gracefully fall back to the title-only rendering.

**AI titles & tool/subagent descriptions (#193).** The viewer titles each conversation by the **AI-generated title** Claude Code writes for the session (falling back, in order, to the first prompt, the project label, then the session id) — both in the rail and as the reader header, and a title that gets rewritten mid-session updates an already-open reader via the live tail. A **subagent thread** is titled by the spawning `Task`'s `description` when present (both in the thread-card header and the matching outline landmark), falling back to the first prompt of the subagent. A **Bash** tool call shows its own `description` on the dimmed chip line, falling back to the command; the command itself always remains in the expanded `$ …` body. Each label is fallback-guarded, so older transcripts and tool calls without a stored description keep their prior rendering.

**Decision & planning tool cards (#177 S2).** Three decision/planning tools now render as dedicated semantic cards instead of the generic JSON tool chip. `AskUserQuestion` becomes a Q&A card: each question with its header tag and single/multi-select mode, every option laid out, and the option(s) you actually chose highlighted in green (a free-text answer that matches no option renders in its own "your answer" block). `TodoWrite` becomes a checklist card, collapsed by default to a one-line progress preview (`done / total` with a mini progress bar and the current in-progress item), expanding to the full list with completed items struck through, the in-progress item flagged in amber, and pending items dimmed. `ExitPlanMode` becomes a plan card that renders the proposed plan as Markdown (clamped with a "Show full plan ↓" reveal) and badges the outcome as Approved, Rejected, or a neutral Responded — never defaulting to Approved on an ambiguous result. Every other tool keeps the generic chip. The chosen-answer highlight prefers the structured answers captured at ingest and falls back to parsing the harness result string on older transcripts; each card is a `<details>`, so the reader's collapse-all keystrokes still reach it.

**Code & shell tool cards (#177 S3, #178).** The Edit / MultiEdit / Write family and Bash now render as purpose-built cards instead of the generic JSON chip. `Edit`, `MultiEdit`, and `Write` render as a **unified diff card**: a header with the file's basename (bold) and dim parent dir, a `+N −M` added/removed stat, a `replace all` tag when the edit replaces every match, and an `N edits` tag for MultiEdit. The body is a unified red/green diff with relative line numbers in the gutter and intra-line word-level highlighting — the exact words that changed are brightened on the removal and addition rows. Unchanged context lines get full per-language syntax highlighting (inferred from the file extension); the changed lines carry the red/green tint and word emphasis as plain text. MultiEdit shows one diff hunk per edit under an `edit k of n` divider, and Write shows the new contents as an all-added `wrote N lines` block (create-vs-overwrite isn't knowable from the input, so it's never labeled "new file"). Beneath each diff a collapsed `result · N lines` sub-panel carries the file's **real** line numbers (the diff's own gutter is relative, since absolute offsets can't be derived from the old/new strings alone); the disclosure label names the snippet's actual line count rather than a fixed string. `Bash` renders as a **terminal**: a `$ <command>` prompt (bash-highlighted) over the output, with any `stderr` split into a red block and an `● error` or `■ interrupted` status badge; the small fraction of outputs carrying ANSI color codes are honored (foreground SGR colors), and everything else is plain monospace. A Bash card whose output runs **long (more than 20 rendered lines)** opens **collapsed by default** with a `show N lines` hint so it doesn't bury the next turn — short output stays open, and the `[` / `]` collapse-all and a per-card click still override either way. Legacy transcripts captured before the Bash stream split degrade to a single merged terminal block, and a request-only Bash (no recorded result) shows the command alone. When an edit's input or a result was clipped at ingest, a **load-full** affordance ("load full input" / "load full output") fetches the un-capped payload on demand from the `/api/conversation/<id>/payload` route (#178), which re-reads the original JSONL line behind the same transcript gate — the diff is recomputed or the terminal re-rendered from the full text, and a rotated/deleted source surfaces a "source no longer available" note. Every card is a `<details>`, so the reader's collapse-all (`[` / `]`) and `j` / `k` navigation still reach it; the load-full spinner honors `prefers-reduced-motion`.

**Codex terminal and patch cards (#331).** Supported Codex `exec` calls now use that same terminal vocabulary without showing the JavaScript/JSON harness: the provider name stays `exec`, each decoded command keeps its workdir, output retains stdout/stderr/error/raw distinctions, request-only and blank results remain visible, and long output follows the same collapsed-by-default rule. Supported `apply_patch`, patch-over-`exec`, and standalone patch-completion records render the exact retained per-file diff rows with native file status and move labels. A provider record without retained diff text says **No diff retained** and shows its status/stdout/stderr instead of inventing a patch. The visible cards are bounded; `raw request`, `raw output`, and `raw event` fetch the authoritative stored payload on demand. Unknown or future wrapper shapes remain generic tool/event disclosures, and Codex exports keep their canonical raw semantics.

**MCP, web & inline media (#177 S4).** MCP tool calls, web tools, and images now render with purpose-built chrome instead of falling through to the generic JSON chip. An MCP call chip leads with the **action** (`browser_take_screenshot`) and a quiet pill carrying the friendly server name, with a per-server icon — `playwright`, `chrome` (claude-in-chrome), `computer` (computer-use), and `codex` each get a dedicated glyph, and any other MCP server gets a generic plug icon plus its raw server name; the full original namespaced tool name (`mcp__plugin_playwright_playwright__browser_take_screenshot`) stays in the chip's title tooltip and the expanded request panel. A malformed MCP name with no action segment degrades to a server-only chip without breaking, and every non-MCP tool that has no dedicated card renders exactly as before. `WebFetch` becomes a semantic source card: a header with the fetched **domain** and an HTTP status chip (green for `2xx`/`3xx`, red for `4xx`/`5xx`; absent on older transcripts captured before the status was recorded), labeled `url` (an external link) and `prompt` fields, and the fetch summary rendered as Markdown — clamped with a "Show full summary" reveal, and a "load full summary" affordance through the `/payload` route when the result was clipped at ingest. `WebSearch` becomes a card with the quoted query, a result-count chip, and a clickable list of the `{title, url}` results (the first ten, then a "+ N more results" expander) with each result's domain shown dim beneath its title; an older transcript with no captured link list falls back to the plain result-text panel. Both web cards only ever render a clickable link for `http:`/`https:` URLs — any other scheme (including `javascript:`) renders as inert text — and neither card makes any outbound request (no favicon fetches); the only network traffic is a link you click yourself. The MCP server/action split and the web captures land on existing history the next time the cache syncs (the one-time reingest described below); older rows simply keep today's rendering until then.

**Inline media & the media route (#177 S4).** Images that previously showed only as a byte-count badge — or, for screenshots returned inside an MCP `tool_result`, were dropped entirely — now render inline. A figure shows the image clamped to a readable height with a quiet caption row (`media type · ~size · open full size ↗`), where the open link loads the full-resolution image in a new browser tab; images load lazily (`loading="lazy"`), so a screenshot-heavy session only fetches the images you scroll near. PDF documents are not embedded inline — they keep an upgraded badge with an `open ↗` link that opens the file in a new tab. The pixel data is never written to `cache.db`: the figure's `src` points at the new `GET /api/conversation/<id>/media` route, which **re-reads the original session JSONL line on demand** (the same mechanism as `/payload`), decodes the base64, and streams the raw bytes — so the cache does not grow and there is no new at-rest copy of your screenshots. Media is addressed by exactly one of `?tool_use_id=<id>&index=N` (an image/PDF inside that tool's result) or `?uuid=<uuid>&index=N` (an image/PDF you attached to a message), where `index` is the ordinal among the media items in that content list; supplying both keys, neither, or a non-integer/negative index is a `400`. The route sits behind the **same fail-closed transcript gate** as the other conversation routes (loopback by default, LAN only under `dashboard.expose_transcripts`, IP-literal `Host` only — a spoofed/rebinding `Host` is `403`) and, because an image can be embedded cross-origin in a way the JSON routes cannot, additionally rejects any request whose browser-set `Sec-Fetch-Site` cross-origin marker is present and not one of `same-origin`/`same-site`/`none` with a `403` (an absent header — `curl`, older browsers — is allowed; this is defense-in-depth layered on the primary gate). Only the allowlisted media types `image/png`, `image/jpeg`, `image/gif`, `image/webp`, and `application/pdf` are served, always with the canonical `Content-Type` for the matched type (never an echoed transcript string) and `X-Content-Type-Options: nosniff`; images additionally get `Content-Security-Policy: default-src 'none'`, while PDFs instead get `Content-Disposition: inline; filename="attachment-N.pdf"` (a CSP sandbox would break native PDF viewers). A non-allowlisted media type is a `404` (no existence oracle), a source line whose file has been rotated/deleted or whose payload is no longer decodable is a `410`, and a pathologically large payload is rejected at a `413` before any decode. The reader degrades gracefully on every error path: a figure that fails to load (`404`/`410`/`413`) falls back to the byte-count badge with a "source no longer available" hint, and a row that predates the reingest — and so carries no media ordinal to address — shows the badge until the reingest backfills it.

**One-time media reingest (#177 S4).** Like the earlier conversation-enrichment passes, the media placeholders and web captures are backfilled onto your existing transcript history by a one-time, lossless re-ingest of the re-derivable conversation cache, gated by a distinct `conversation_media_reingest_pending` flag that cache migration `009` sets on first open after upgrade. It runs on the next `cache-sync` (foreground) or in the background on the next dashboard sync, resumable per the usual sorted-path cursor, and clears its flag when complete; until then, MCP/web/image rows render in their pre-S4 form. `cache-sync --rebuild` is the clean one-shot way to force it through.

**Injected (`isMeta`) content.** Lines Claude Code injects into a transcript that you did not type — a skill's body (when an assistant turn invokes the `Skill` tool, or a `SessionStart` skill), git-context blocks, "Continue from where you left off.", pasted-image placeholders, slash-command plumbing — are never rendered as a "You" prompt. They collapse into quiet, collapsed-by-default disclosures: slash-command plumbing keeps the `System marker` pill, and everything else becomes a neutral `Injected context` pill. A skill body invoked via the `Skill` tool now **folds into its Skill tool chip** — the chip itself expands to the rich-Markdown body (the redundant "Launching skill: <name>" result is dropped), so the skill reads as one nested unit inside the turn rather than a detached pill below it. A `SessionStart` skill (no `Skill` tool call) keeps the standalone `Skill content · <name>` pill (the name is the skill's directory basename; its body still renders as full Markdown when expanded). Injected bodies are excluded from derived titles and full-text search. The classification — and the skill-body fold — land on existing history the next time the cache syncs (a one-time, lossless re-ingest of the re-derivable conversation cache).

**Reader UX (#175, #176).** While a conversation's first page loads, the reader shows an animated spinner (suppressed under `prefers-reduced-motion`). When you scroll deep into a tall turn so its start is off the top of the reading column, an unobtrusive floating "↑ Top of turn" button appears at the bottom-right of the reader; clicking it scrolls that turn back to its start (reduced-motion aware). Nothing floats over the reading column itself — the button is anchored clear of the bottom-center "↓ N new" pill and is hidden again on a session switch. The assistant model renders as a colored `.chip` (matching the rest of the dashboard) rather than plain text, with no chip shown when the model is unknown. Finally, the open conversation **live-tails**: once you've paged to the end, new turns from an active session appear within about a second with no manual reload — the reader sticks to the newest turn if you're already at the bottom, or surfaces a floating "↓ N new" pill (click to jump to the latest) if you've scrolled up, and the cost/model totals update along with the new turns. Live-tail engages only after the conversation is fully paged; already-loaded turns are not re-fetched. See [Live-tail](#live-tail) for how the ~1s latency is delivered and how to opt out.

**Transcript export & extraction (#217 S5, F1/F5).** The reader header gains an **`Export ▾`** menu offering four Markdown scopes, each with a **Copy** (to clipboard) and a **Download** (`.md` file) action: **Whole transcript** (`all` — every turn, with prose, thinking blockquotes, Edit/MultiEdit/Write rendered as fenced diffs, Bash as `$ cmd` + output, meta turns as labeled blockquotes, and subagent turns grouped under a `⎇ Subagent: <kind>` heading), **Prompts only** (`prompts` — your main-session prompts as `## Prompt N` sections), **Chat only** (`chat` — human + assistant prose only, main-session, no thinking/tools/meta — deliberately leaner than the live "Chat" focus mode), and **Replay recipe** (`recipe` — a `# Replay recipe` header + a numbered list of your prompts, the re-runnable script form). Because the reader is windowed, the export is computed **server-side over the whole assembled session** (the new `GET /api/conversation/<id>/export` route), so it is complete even for a long transcript you've only partially paged in; truncated tool inputs/results are marked `… [truncated]` rather than presented as complete. The download filename is the session title slugified (path/control/non-ASCII stripped, length-capped), falling back to the session id.

**Anonymize toggle & safe sharing (#281 S4).** Next to `Export ▾` is an **`Anon`** toggle — a persisted, always-visible mode indicator, **on by default**, so the natural "share this session" action is safe by default. While it is on, every Export-menu Copy/Download fetches the server-anonymized Markdown (`&anonymize=1`) and download filenames gain an `-anon` suffix; the per-card copy buttons also anonymize their copied text. The scrub rewrites your observed project paths, home directory, and username to stable placeholders (`project-N`, `~`, `user`) and redacts a documented set of high-precision secret patterns — it is **best-effort over known tokens; review before sharing** (the full is/isn't-covered list is on the [`transcript`](transcript.md) page, which is the same scrub the CLI applies). Turning the toggle off restores the exact raw export/copy behavior, byte-for-byte. **Per-card copy is fail-closed:** while the mode is on, a card's copy is written to the clipboard only after the current session's scrub plan (`GET /api/conversation/<id>/anon-map`) has loaded and applied successfully; on any failure the clipboard is left untouched and the button shows an error state, rather than silently copying raw text while the UI says "anon".

**Per-card extraction (#217 S5, F1/F5).** Two per-card affordances ride alongside the existing copy buttons. A diff card (Edit / MultiEdit / Write) gains a **`.patch`** download that builds a real unified diff — `diff --git a/<path> b/<path>` + `--- a/<path>` / `+++ b/<path>` (or `--- /dev/null` for a Write) + `@@` hunk headers — from the card's hunks and the tool's `file_path`. A **Write** yields an applyable add-patch; **Edit / MultiEdit** hunk line numbers are snippet-relative (not whole-file offsets), so those `.patch` files are a best-effort shareable representation and may not cleanly `git apply`. A Bash card gains a **`copy full`** action that copies the whole session form — `$ <command>` + stdout + a stderr block — appending a `… [truncated]` marker when the result was clipped at ingest and not loaded full (it copies the bounded text, never auto-fetching the full output).

**Files-touched tab (#217 S5, F2).** The outline sidebar gains an **`[Outline] [Files]`** tab toggle. The Files tab lists every file the session modified via an Edit / MultiEdit / Write call (in first-touch order), each with its basename prominent, its directory muted, and a summed **`+N −M`** badge over all touches of that path; expanding a file reveals its individual touches as `Edit / MultiEdit / Write` jump rows that scroll the transcript to the turn that made the change. When a touch's line-stat can't be computed (a truncated edit whose stamped stat is also absent), the touch is still listed and its side of the badge is omitted. The narrower `{Edit, MultiEdit, Write}` set is deliberate — `NotebookEdit` and read-only tools never appear. A session with no edits shows a quiet "No files modified". The tab selection resets to **Outline** on a genuine session switch.

**Tool-type & subagent focus filters (#217 S5, E4).** Beside the `All / Chat / Prompts / Errors` segmented control, a **`▾ More`** menu adds three more focus filters on the same single-select axis: **Edits** (only turns that ran an Edit / MultiEdit / Write tool), **Bash** (only Bash turns), and a **Subagent ▸** submenu that lists each top-level subagent thread (labelled by its agent kind, or its key when no metadata is available) so you can isolate one subagent's turns. Picking a More filter clears the four primary modes (the segmented buttons show unselected and the `▾` shows the active label); the `v` key keeps cycling only the four primary modes and returns to All from a More filter. Like the primary modes, these filter only the reading column (the outline is untouched), coalesce hidden turns into the `· N hidden ·` marker, and reset to All before an outline/Files jump whose target the active filter would hide.

**Git-context diff rendering (#217 S5, F6).** Claude Code sometimes injects a git diff into a transcript as an unfenced `Injected context` block (e.g. `- Unstaged changes: diff --git a/CLAUDE.md b/CLAUDE.md … @@ … @@`). The reader now splits such a context body into prose and diff segments and renders the diff segments as a real red/green unified diff — per-file path header, a `+N −M` stat, and the same syntax-highlighted hunk rows as an Edit diff card — while the surrounding prose stays Markdown. Detection is conservative: a diff is recognized only on a real `diff --git a/… b/…` marker (and the git extended headers — `index`, `new file mode`, `rename from`/`to`, `similarity index`, etc. — that follow it), so a plain Markdown bullet list whose lines start with `-`/`+` is never mistaken for a diff. A context body with no `diff --git` marker renders exactly as before (all prose).

**Task-completion summary (#217 S5, F7).** When a session's main-thread to-do checklist (the `Task*` family, or legacy `TodoWrite`) ends fully completed, the reader header shows a green **`✓ Complete · N`** chip (N = the task count) that stays visible regardless of scroll position; clicking it jumps to the turn carrying the final checklist. The outline also gains a `✓ Session complete (N tasks)` landmark at that turn. Both appear only when the **final main-thread** snapshot is entirely done — a subagent's own checklist never triggers them (task state is tracked per thread), and a session with no tasks or an unfinished checklist shows neither.

**In-reader cost/token analytics (#217 S6, F3).** Each assistant turn's cost footer gains a thin **micro-bar** whose width and intensity encode that turn's cost relative to the most expensive turn loaded so far — a quick visual scan for "where did the money go" without reading every figure (the exact `$`/token text stays; the bar is a relative cue, with the precise cost in its tooltip). The reader header carries a **cumulative-cost chip** showing `$so-far / $total` plus a thin progress bar that tracks your scroll position: as you read down, the running total sums the cost of every turn through the topmost-visible one. When earlier pages aren't loaded yet (a partial window after a tail-open or a jump), the cumulative figure is prefixed `~` to mark it a lower bound; a costless session hides the chip entirely. Finally, the outline's cache-rebuild stat expands from a single `Cache · N rebuilds` line into a **per-rebuild jump list** — each row labelled with its turn, its wasted tokens, and its `~$` cost, worst-first, capped at three with a "+N more" expander — so you can jump straight to each cache-rebuild moment. The list honors the same `dashboard.cache_failure_markers` opt-out as the rest of the cache markers.

**Bookmarks & notes (#217 S6, F4).** Each turn's hover/focus action row gains a **★ bookmark toggle**; bookmarking a turn reveals an inline editor to attach a short **note** (saved on Enter or blur, cancelled on Esc). Bookmarks persist in your browser's local storage only — they are never uploaded or synced anywhere — and surface as **★ landmarks in the outline** (labelled with your note, or the turn's own heading), including bookmarks on subagent turns. The outline's "Jump to" cluster gains a **★ chip**, and the reader adds keys: `i` / `I` step to the next / previous bookmark, and `t` toggles a bookmark on the current turn. A bookmark on a turn that has since been compacted away is quietly skipped.

**Inline PDF (#217 S6, F9).** A PDF attachment in a transcript now offers a **`view inline ▾`** toggle beside its `open ↗` link; expanding it renders the PDF right in the reading column at a capped, scrollable height, with a `collapse ▴` control. PDFs load lazily — the viewer mounts (and the bytes fetch) only when you expand one — and continue to be served behind the same privacy gate as every other transcript media request; a browser without a built-in PDF viewer falls back to the `open ↗` link automatically.

**Session navigation & insight (#177 S5).** The reader gains a full-session navigation layer that stays oriented even in a long transcript you've only partially paged in. A collapsible **outline sidebar** (toggle with `o`, or the `☰ Outline` button in the reader header; the open/closed state persists across sessions) lists every turn as a compact landmark — prompts, assistant replies, subagent threads, plan/question moments, and errors — with nested thinking entries, and scroll-sync highlights the entry for whatever turn sits at the top of the reading column. Clicking an outline entry jumps to that turn, paging the detail in first if it isn't loaded yet and expanding a collapsed subagent thread when the target lives inside one. A **stats overview** at the top of the outline summarizes the whole session: turn counts, wall-clock duration, total tokens, cost, the models used, and a tool-use histogram, plus an error row that jumps to the first error (and hides itself when the session has none). **Jump-to-next** keys move you between landmarks from your current position with no wrap-around: `e`/`E` for errors, `u`/`U` for prompts, `b`/`B` for subagent threads, and `p`/`P` for plan/question turns (uppercase = previous); a glyph-cluster of clickable buttons mirrors each, carrying a count. **Focus modes** filter the reading column without touching the outline: `v` cycles All → Chat → Prompts → Errors (a segmented control in the header does the same), and runs of hidden turns coalesce into a quiet `· N hidden ·` marker you can click to drop back to All at that spot; a jump-to-next or outline click whose target the current mode would hide resets to All before landing, never a silent no-op. Finally, each turn now carries a **timestamp** — a quiet `· HH:mm` at the end of its header (rendered in your `display.tz`, with the precise instant in a tooltip) — and the reader inserts **gap and day markers** between turns: a `⏸ 42 min later` rule when adjacent turns are ten or more minutes apart, a `— Jun 13 —` rule when the calendar day changes, or a combined `⏸ 9.5 h later · Jun 13` when both apply. Assistant turns that carry token data extend their cost footer to break out usage — `$0.0214 · in 1.2k · out 4.8k · cache 310k` — with the four exact counts in a tooltip; a turn with token data but no attributable cost shows a tokens-only footer, and an un-reingested turn keeps today's cost-only footer.

### Browse filters

The Browse rail narrows the conversation list by four axes through a compact **`Filters ▾`** popover beside the search box. **Date** matches a session's **last activity** — the same instant the rail sorts and groups by, so the date-section headers, the `recent` order, and the filter all agree; it offers presets (This month / Last month / Last 7d) plus a from→to range, with "this/last month" resolved in your `display.tz`. **Project** is a multi-select that matches ANY of the chosen project labels (its options come from the `/api/conversations/facets` endpoint, each shown with its conversation count). **Cost** filters on the session's total USD with an optional minimum and/or maximum (quick `≥$1` / `≥$5` / `≥$10` presets). **Cache rebuilds** filters on the per-session cache-rebuild count with a threshold (`≥1` / `≥3` / `≥5`, or a custom `≥ N`) — the same count the [session modal's Cache rebuilds section](#cache-rebuilds-in-the-session-modal) and the in-reader chips report. Axes combine with AND; active filters render as small removable chips under the search box (e.g. `Jun 2026✕`, `cctally-dev✕`, `≥1 ♻✕`) with a "Clear all", and numeric/text inputs apply live (debounced). Filtering is **server-side** — the predicates are pushed into the list query's SQL before paging, so pagination and the result count stay correct over the filtered set.

Filters apply to **both Browse and Search**: the same filter set is shared, so when a full-text search needle is active the Filters button stays enabled, the active-filter chips stay visible, and the chosen axes narrow the search results too (the file-path and AI-title kinds narrow to the filtered sessions as well). Filters **and the rail sort persist across reloads** (stored in your browser's local storage), so a reader returns to the discovery layout you left. While the conversation cache is still building its per-session rollup on a brand-new or `--no-sync` dashboard, the project/cost/rebuild axes show a brief muted "Project/cost/rebuild filters apply once indexing finishes." note and only the date axis applies (in search mode the equivalent "Some filters unavailable while indexing." note appears); this self-corrects after the first full sync.

### Rail sort

A compact **Sort** control in the rail header orders the Browse list by **Recent** (default, newest last-activity first), **Oldest**, **Cost** (high→low), **Messages** (high→low), or **Project** (A→Z, with un-labeled sessions last). The chosen sort persists across reloads alongside the filters. Recent / Oldest / Messages are always available; Cost and Project sort the per-session rollup, so on a brand-new or `--no-sync` dashboard that is still indexing they briefly fall back to Recent order with a muted "Cost/Project sort unavailable while indexing — showing recent order." note, self-correcting after the first full sync.

### Open-at-bottom & reverse pagination (#217 S3)

The reader now opens a long conversation **at the bottom** — the newest turns — rather than at the top, so a session you return to lands where the action is. On open it fetches the last page in one request (`?tail=1`) and lands on the newest turn with live-tail engaged; a short conversation that fits a single page opens at the top instead, so it reads from the start. From there the window pages **both directions**: scrolling down appends older→newer pages (as before), and scrolling up **prepends** earlier pages via a reverse cursor, anchored so the turn you were reading stays fixed under the viewport instead of jumping. The two edges are independent — a backward (scroll-up) page never disturbs the live-tail "follow the bottom" state, so a reader opened at the tail keeps following the live session even after you scroll up to read history.

**Reading-position memory.** The reader remembers where you were in each conversation: when you switch away and come back later, it restores you to the turn you were last reading (anchored to that turn, not a pixel offset, so it survives a different window size or a grown transcript). A deep-link or jump target always wins over the saved position; if the saved turn no longer exists, the reader falls back to opening at the bottom (or the top for a single-page session). The memory is a small bounded list (the ~50 most-recently-read conversations) kept in your browser's local storage, keyed by source plus the opaque qualified conversation key so native-key collisions cannot share a position.

**Direct jumps to the last prompt / last error.** Beyond the `e`/`E` and `u`/`U` step-to-next-or-previous keys, two keys jump **directly to the most-recent occurrence**: `a` lands on the last prompt and `L` lands on the last error turn — handy for "take me to where I last asked something" or "show me the latest failure" without stepping. Both are no-ops when the conversation has none, and both are suppressed while a filter input or modal is focused. The outline's **"Jump to" chips** follow the same model: a **primary click jumps to the latest occurrence** of that landmark family, and **shift-click steps to the previous** one (the chip arrows / reader step-keys are unchanged).

### Outline upgrades & compaction landmark (#217 S3)

**Per-subagent cost.** Each subagent thread in the outline now shows its **cost** beside the thread label — a display-only, summed-cost-once figure (never a reconciled/budget number, like the cache-saved figure). Every subagent bucket carries one, including older threads whose spawn metadata was never captured.

**Resizable outline column.** A thin vertical **resize divider** sits between the reading column and the outline. Drag it to widen or narrow the outline (the column is on the right, so dragging left widens it); it's also **keyboard-resizable** when focused — `←`/`→` step the width, `Home`/`End` jump to the widest/narrowest — and carries a labeled `separator` role with `aria-valuenow`/`min`/`max` for screen readers. The width is clamped to a sensible band and **persisted** in your browser's local storage, so it survives reloads. On a narrow desktop the outline collapses to a slide-over sheet (as before) and the divider is hidden.

**Tree outline (nested subagents).** When a subagent spawns its own sub-subagents, the outline now **nests** those children indented beneath their parent thread — a small tree mirroring how the reader nests them — instead of listing every thread flat. A session with no subagent-inside-subagent nesting looks exactly as before (the tree degenerates to the flat list).

**Compaction landmark + jump.** A conversation **compaction** (the auto-summarize point) now shows as a dedicated outline landmark with its own "Jump to" chip, and `m`/`M` step to the next/previous compaction in the reader — so a long, compacted session is easy to navigate around the summary boundary.

### Reader polish & a11y (#217 S3)

A batch of smaller reading and accessibility refinements:

- **Search the list from inside a reader.** The `f` key focuses the conversation-list search input even while a conversation is open, so you can search for another session without pressing `Esc` to leave the reader first. (`/` stays reader-aware: with a reader open it opens the in-conversation find bar.)
- **Errors badge counts error turns.** The header focus-mode `Errors` badge now shows the number of error **turns** — the same count as the "Jump to" error chip and exactly what clicking the filter navigates between — rather than the raw total of error events. (The outline stats card keeps its "N errors in M turns" reconciliation phrasing.)
- **Long Bash output collapses by default.** A Bash card with more than 20 rendered output lines opens collapsed with a `show N lines` hint (see the **Code & shell tool cards (#177 S3, #178)** note under [Conversation viewer endpoints (Plan 2)](#conversation-viewer-endpoints-plan-2)).
- **WebSearch error chip.** A WebSearch results-count chip no longer shows green when the search errored — it takes a neutral/error style, with the `· error` marker alongside.
- **List Load-more button.** The browse list's "Load more" button now disables and shows a loading label while a page is fetching, matching the search results list.
- **Smaller touches.** The Find control uses an inline icon (matching the other reader controls), the `· N hidden ·` focus-mode marker carries a screen-reader label naming the hidden-turn count, the load-full affordance disables when there's no addressable session, and the diff result disclosure names the snippet's line count.

### Jump to latest

The reader header carries a **`Latest ↓`** action (titled "Jump to latest", and the `End` key) that takes you straight to the conversation's most recent turn. It **resets the window to the tail page in a single request** (instant even on a very long session — no forward drain), then scrolls the final turn into view with the usual flash. Landing at the bottom parks you in the stick-to-bottom position, so a live session's subsequent appends keep following automatically — jump-to-latest and the live-tail "↓ N new" pill are complementary. The control is hidden for an empty conversation, and the `End` key is suppressed while a filter input or a modal is focused.

### Reader pagination (#217 S2)

`GET /api/conversation/<id>` pages the assembled turn list with one of three mutually-exclusive cursors plus `limit` (default `500`, clamped `1`–`1000`); supplying more than one cursor is a `400`. `?after=<id>` pages **forward** (older → newer) from the turn whose anchor id is `<id>`; `?before=<id>` pages **backward** (the page of turns immediately before `<id>`); `?tail=1` opens **at the bottom** — it returns the last page in a single request, so a client can land on the newest turns without paging forward through the whole session. The response `page` object carries `next_after`/`has_more` (whether newer turns remain to page forward to) and the additive `prev_before`/`has_prev` (whether older turns remain to page back to), computed uniformly from the page's position, so a short backward page at the head still reports `has_more: true`. A stale cursor (an id not in the current item list) yields an empty page with both `has_more` and `has_prev` false — it never silently re-serves the head or tail. The existing no-cursor and `after` responses are byte-stable except for the two additive `prev_before`/`has_prev` keys; `last_anchor` (the whole-session tail) is unchanged.

### Search depth (#177 S6)

Conversation search now reaches past prose into what a session actually contains — commands, file paths, error strings, and the assistant's thinking — with exact kind facets, result counts, and a browser-style in-conversation find bar. Three surfaces share one consolidated full-text index, so totals, pages, and match badges are exact by construction.

**Rail search kinds, counts, and load-more.** While a search needle is active, a single-select chip row — `All · Prompts · Assistant · Tools · Thinking` followed (after a subtle separator) by the two structural facets `Title · Files` — sits between the input and the results. The chips map 1:1 to the `kind` param on `/api/conversation/search`: `All` is unscoped, `Prompts` and `Assistant` scope to prose in your prompts vs the assistant's replies, `Tools` searches tool inputs and results (commands, file paths, error output, recorded answers, Bash stderr), and `Thinking` searches the assistant's thinking blocks; the trailing `Title` facet searches the AI-generated session titles and `Files` searches the write-class file paths each session edited or created (see [Title search](#search-depth-177-s6) and [File-path search](#search-depth-177-s6) below). Unlike `Tools`/`Thinking`, the `Title` and `Files` facets do **not** ride the split index, so they stay enabled even during the prose-only indexing window. Switching kind aborts any in-flight fetch and restarts at page one. The row wraps to a second line on a narrow rail and every chip keeps the mobile ≥44px touch target. Under the chips, a `aria-live` count line shows `No results`, `{N} results`, or `{N} results · basic search` (the last when the host has no FTS5 and search falls back to the degraded substring mode below). Non-prose hits carry small uppercase match badges (`tool`, `thinking`, `title`, `file`) in the title row; a hit matching only prose is unbadged, and a turn matching in several columns at once dedups to one row. A **title** hit shows the matched session title as its snippet and opens the session at its first turn; a **file** hit leads with the file path (file styling) with the session title secondary, its snippet is the plain path, and it opens the session at that path's most-recent touch. Results page in fixed chunks: when more remain, a `Load {N} more ({M} remaining)` button at the list end fetches the next page and appends (capped at 50 per click), with no infinite scroll. Search-as-you-type matches the last word as a prefix, so `cache.d` finds `cache.db` before you finish typing.

**The find bar.** With a conversation open, pressing `/` **or `⌘F` / `Ctrl+F`** opens a floating find pill in the top-right of the reading column (Cmd+F style, zero layout shift); the `⌘F` / `Ctrl+F` intercept suppresses the browser's own find bar only inside the Conversations workspace (the native shortcut still works on the dashboard panels), and only when no modal/overlay or text-input mode is up. With no conversation open, `/` and `⌘F` / `Ctrl+F` focus the rail/Sessions search input instead. The input owns `Enter` (next match), `Shift+Enter` (previous), and `Esc` (close, restoring focus to the transcript so `j`/`k` resume); with the bar open but the input blurred, `n`/`N` step matches and `Esc` closes. While the bar is open `Tab` / `Shift+Tab` cycle within its own controls (a focus trap), so keyboard focus can't escape to the page chrome; `Esc` is the documented exit. Navigation wraps around, and each step runs the same jump flow as an outline click — it pages the target turn in if it isn't loaded yet, scrolls it into view with a flash, and (when the matched anchor is a tool or thinking hit) imperatively expands that turn's collapsed disclosures so the match is visible; a target the active focus mode would hide resets the mode to All before landing. An exact `k / N` counter (`aria-live`) shows your position, a `· first 500` note appears when a conversation has more than 500 matching turns (the anchor list caps there with `anchors_truncated: true`), the current match's kind badge shows when it matched in tool/thinking content, and a `basic search` hint shows in LIKE mode.

Two toggles sit beside the input — `.*` (**regex**) and `Aa` (**case-sensitive**), each with `aria-pressed` state and persisted across reloads (`cctally.conv.find.regex` / `cctally.conv.find.case` in localStorage). Regex matching runs server-side over the full transcript corpus (prose, tool input/results/stderr, thinking — the same columns the kind facets cover), so `search_depth` is always `full` for a regex/case search; an invalid pattern surfaces an `invalid regex` hint (announced via `role="alert"`) with matches cleared. While find is active, your search terms are wrapped in `<mark>` in the rendered prose — honoring the case toggle — skipping code blocks and structured tool cards; in **regex** mode the matches now get a best-effort inline underline too (#223 — per rendered text segment, case-toggle-aware; the server-driven match count and jump-to-match remain authoritative, and an invalid or pathological pattern degrades to no underline). The match list **live-refreshes** as the open conversation grows: each live-tail merge re-runs the query (debounced) and the selected match is preserved by uuid across the refresh — your cursor stays on the same turn when it survived, resetting to the first match only when it vanished.

**Endpoints.** Both surfaces sit behind the same fail-closed [transcript gate](#conversation-viewer-endpoints-plan-2) (loopback by default, LAN only under `dashboard.expose_transcripts`, IP-literal `Host` only). `/api/conversation/search` gains `kind` (default `all`; an unknown value is `400`) and each hit gains an additive `match_kinds: ["tool", "thinking"]` array. The new `GET /api/conversation/<id>/find?q=…&kind=…` returns `{"anchors": [{"uuid", "match_kinds"}], "total", "anchors_truncated", "mode": "fts"|"like"|"regex", "search_depth"}` — rendered-turn anchors in document order (matched physical rows fold onto their rendered turn, so a tool-result match collapses into its owning assistant turn and counts once), `total` over rendered turns, anchors capped at 500. An unknown session is `404`, an unknown `kind` is `400`, and an empty query returns zero anchors. Both responses carry an additive `search_depth` field (no schema-version bump — the additions are backward-compatible). **`/find` also accepts `regex=1` and `case=1` (#217 S4):** when either is set the matcher bypasses FTS/LIKE and scans the physical message rows over the full corpus columns (so `search_depth` is `full` and the prose-only interim window never blocks it) — `mode: "regex"` for a regex scan, `mode: "like"` for a case-only substring scan; otherwise the existing FTS/LIKE fast path is byte-stable. An invalid regex is pre-validated in the handler and returns `400 {"error": "invalid regex: …"}` (never a `500`). The scan is bounded (pattern/query length and per-row scanned text) as a best-effort ReDoS/perf guard.

**Title search & filtered cross-session search (#217 S2).** `/api/conversation/search` adds the `kind=title` facet, which searches the AI-generated session titles and returns one session-level hit per matching session — `match_kinds: ["title"]`, the snippet drawn from the matched title, and the hit anchored to that session's first turn. The `total` counts only **anchorable** sessions (a title row whose session has no surviving message rows is excluded from both the count and the page), so the count never lies. `kind=title` is a cross-session-search-only facet: the in-conversation `/api/conversation/<id>/find` route rejects it (and the `files` facet below) with a `400`, never a `500`. The search route also accepts the same server-side browse filters as the [browse rail](#browse-filters) — `date_from`/`date_to`/`projects`/`cost_min`/`cost_max`/`rebuild_min` — applied uniformly across **every** kind as a session-scope restriction; a malformed filter value is a `400`. As on the browse rail, when the per-session rollup is still indexing (a brand-new or `--no-sync` dashboard) a rollup-only filter (project/cost/rebuild) is dropped, only the date axis applies (over the session's last-activity timestamp), and the response carries an additive `filter_degraded: true`. A request with no filters is byte-stable with the prior search output (no `filter_degraded` key). When the host lacks FTS5, `kind=title` degrades to a `LIKE` scan over the titles, the same as the prose kinds.

**File-path search (#217 S2).** `/api/conversation/search` adds the `kind=files` facet, which searches the write-class file paths each session touched — files opened by an `Edit`, `MultiEdit`, `Write`, or `NotebookEdit` tool call (read-only `Read` and shell `Bash` invocations are deliberately excluded, so the axis answers "which sessions *modified* this file"). It returns one hit per distinct `(session, file path)` — `match_kinds: ["file"]`, the snippet drawn from the file path, and the hit anchored to that path's most-recent touch — and `total` is the distinct `(session, file path)` count. A query is matched as a **substring** of the file path (#223), so a bare basename (`dashboard.md`) or a mid-path fragment (`cctally`) finds the sessions that touched a matching path — there is no path-prefix special case. The match is a deliberate full scan over the modest touch table (a leading wildcard can never use a path index) and is case-insensitive. `kind=files` is a cross-session-search-only facet — the in-conversation `/api/conversation/<id>/find` route rejects it with a `400`, never a `500` — and it composes with the same browse filters as the other kinds. The axis is backed by a re-derivable `conversation_file_touches` table (cache migration `019`); existing history is backfilled once on the first sync after upgrade (the distinct `conversation_reingest_file_touches_pending` flag), and a `cache-sync --rebuild` repopulates it.

**The `search_depth` interim window.** The tool/thinking facets are served from a split full-text index that this session builds. On the first dashboard sync (or `cache-sync`) after upgrade, a one-time index split runs automatically under the cache lock — a resumable backfill of the new tool/thinking columns from the cached transcript data, followed by an atomic swap of the search index. The work is gated by cache migration `010`'s flag and is lossless and re-derivable; `cache-sync --rebuild` forces it through in one pass. Until it completes, search and find responses report `search_depth: "prose-only"`: prose search (`All`/`Prompts`/`Assistant`) works normally, the `Tools` and `Thinking` chips render disabled with an `indexing…` hint, and the find bar restricts itself the same way. The state self-heals to `"full"` on the first response after the split finishes — no action required.

**Degraded basic-search mode.** On a host whose SQLite was built without FTS5, search and find fall back to a LIKE scan over the same three columns (prose, tool, thinking). This is a deliberately weaker mode: it matches the whole query as a single substring rather than term-wise, surfaced as `· basic search` in the rail count line and a `basic search` hint on the find bar (`mode: "like"` in the JSON). Kind scoping and match badges still work; only the match semantics degrade.

### Deep-linking & per-turn permalinks

The conversation reader reflects its state into the URL hash: `#/conversations/<sessionId>` for an open conversation and `#/conversations/<sessionId>/<turnUuid>` for a specific turn. Reloading or using the browser Back/Forward buttons restores the conversation and re-lands the turn jump. Hovering any turn — prose, tool-result, or system-marker — reveals a link button that copies a permalink straight to that turn and points the address bar at it; on prose turns it sits beside the copy button, and on the collapsible tool-result and system-marker chips it sits in the summary row. These links are local-first: a permalink is relative to your dashboard's origin and only resolves for someone who can already reach it (loopback, or your LAN when started with `--host 0.0.0.0`) — it is not a public, shareable-off-host URL.

### Live-tail

When you have a conversation open and fully paged to its end, the reader follows the live session in near-real time. Instead of waiting for the periodic 5-second dashboard snapshot tick (configurable via `--sync-interval`), the open reader keeps a dedicated per-conversation SSE stream to `GET /api/conversation/<id>/events` that watches only that session's JSONL file(s) and pings the client within about a second of the file growing. On a ping the reader fetches just the new turns (`event: tail`), and the server emits `: keep-alive` comments while the session is idle so proxies don't drop the stream. The 5-second snapshot tick remains a slow backstop, so even with the live-tail stream unavailable new turns still surface on the next tick.

The watch loop runs a targeted single-file cache ingest scoped to only the open session's file(s) — it never triggers a full project-tree walk — so following a live conversation stays cheap. It is fail-closed behind the same [transcript privacy gate](#conversation-viewer-endpoints-plan-2) as the other conversation routes: loopback by default, LAN only under `dashboard.expose_transcripts`, and an IP-literal `Host` only (a spoofed/rebinding `Host` is `403`).

`--no-sync` makes the stream **passive**: the dashboard was started to freeze data at the startup snapshot, so the live-tail endpoint sends keep-alives only and performs no ingest and no `tail` emit, leaving the frozen snapshot untouched.

To opt out of the live-tail entirely, set `dashboard.live_tail` to `false` (via `cctally config set dashboard.live_tail false` or the dashboard Settings overlay). The reader then falls back to the 5-second snapshot tick for new turns. The default is `true` (absence is ON); see [`config.md`](config.md#allowed-keys).

### Cache rebuilds in the session modal

The Recent Sessions (session-detail) modal carries a **Cache rebuilds** section for the selected conversation, surfacing the same prompt-cache-failure signal the [conversation viewer](#conversation-viewer-endpoints-plan-2) detects per turn. It shows the count of cache-rebuild events, the total wasted USD they cost, the total tokens re-created, and a green session cache-value-saved figure (what the session's cached prefixes saved versus paying full input price, shown only when positive). Below the tiles a worst-first list (capped, with a "+N more" expander) gives one jump-link per rebuild — clicking one opens the conversation viewer scrolled to the message that triggered that rebuild. A session with no rebuilds shows a "No cache rebuilds ✓" zero-state instead of the list. The section respects the `dashboard.cache_failure_markers` opt-out (it disappears when the markers are turned off, the same toggle that hides the in-reader cache-rebuild chips), and it is absent entirely when transcripts are not accessible — for example on a LAN-bound dashboard without `dashboard.expose_transcripts`, where the gated outline data the section reads returns `403`.

### Session comparison (#217 S7)

The conversation viewer can put **two** sessions side by side and diff their **human-prompt sequences** — built for comparing variations of the same or a similar task to see, at a glance, where two runs took different paths and which run was cheaper / faster / cleaner.

**Entry.** From an open reader, click **⟷ Compare with…** in the reader head. The conversation list enters a **pick-mode** — a banner ("Comparing with `<anchor>` — pick a session") with a **Cancel** control (or **Esc**); the anchor session's own row is greyed out and non-pickable, every other row picks the second session. Choosing one opens the comparison. The comparison is shareable and cold-loadable via its URL, `#/conversations/compare/<A>/<B>`; an `A === B` URL degrades to the single reader, and an unknown/removed session shows a "couldn't load — close comparison" fallback rather than a broken split.

**Layout.** Above ~1100px the view is a **two-column** split (run A left, run B right); below ~1100px it falls back to a **unified** single column (the same aligned data, shared one alignment model — this is the mobile path and also a legitimate desktop view if you prefer it). The header source-labels both runs. A **metrics strip** heads the view with six A→B cells — Cost, Tokens, Prompts, Errors, Duration, Files. Within one provider it keeps the normal deltas and lower-is-better cues. Across providers, only semantically compatible Cost and Prompts receive deltas; Tokens, Errors, and Files show the two native values as `provider-specific`, and Duration is `unavailable`, preventing a false comparison between provider-native event models. The header carries **⇄ swap** (flip which run is on the left — the URL follows) and **✕ close** (return to the single reader). Copying the comparison produces two complete, source-labelled export sections rather than blending their bodies.

**The aligned diff.** The two runs' prompts are aligned by a longest-common-subsequence over each prompt's **normalized first line** (trimmed, whitespace-collapsed, lowercased). Matched prompts sit on a shared neutral row; a replaced region is marked with a **⚡ DIVERGENCE** bar and rendered A (removed) / B (added); a prompt only one run has renders as a real row with a hatched gap on the empty side. Divergence is conveyed by the ⚡ bar + a ◆ marker + the add/del styling, never by color alone. Clicking any row lazily fetches and expands the **full** prompt text for both runs inline (via [`/api/conversation/<id>/prompts`](#endpoints), once per session), each with an **"open in reader →"** jump to that session's single reader at the turn.

> **Heuristic, first-line alignment.** The alignment matches on prompt first lines only, so two genuinely different prompts that share a first line read as "matched", and a paraphrased prompt reads as "divergent". That is acceptable for a *sequence-level* overview — the full-text expand exists precisely so you can verify any row by eye. The expanded panel shows both full prompts side by side **without** an A↔B word-diff (they are two different prompts, so a word-diff would be noise). The comparison is a static, re-openable snapshot — it does **not** live-tail.

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
