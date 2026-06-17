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
| `/` | Open the conversation find bar when a reader is open; otherwise focus the rail / Sessions search input |
| `n` / `N` | Next / previous search match — and, with the find bar open and its input blurred, step to the next / previous in-conversation match |
| `s` | Open settings overlay |
| `q` | Close the tab (best-effort) |
| `?` | Toggle help overlay |
| `o` | Toggle the conversation outline sidebar (reader) |
| `v` | Cycle the reader focus mode (All → Chat → Prompts → Errors) |
| `e` / `E` | Reader: jump to next / previous error turn |
| `u` / `U` | Reader: jump to next / previous prompt |
| `b` / `B` | Reader: jump to next / previous subagent thread |
| `p` / `P` | Reader: jump to next / previous plan/question turn |
| `j` / `k` | Reader: move the focused-turn cursor down / up |
| `End` | Reader: jump to the conversation's latest turn (pages forward to the end, then lands and flashes it) — suppressed while a filter input or modal is focused |

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

## Startup sync

The dashboard binds its HTTP port immediately and serves the current cached snapshot; the first full sync (and any pending one-time conversation-enrichment reingest) runs in the background and is pushed to the page over SSE when it completes. On a large transcript history the background reingest is resumable — interrupting the dashboard mid-sync and relaunching resumes where it left off rather than restarting. To start without any sync, use `--no-sync`; to consume a pending reingest in one foreground pass, run `cctally cache-sync` (or `cache-sync --rebuild`).

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
| `GET /api/conversations` | Conversation-viewer browse rail — all-history per-session rows with per-session cost. Optional server-side filter params (`date_from`/`date_to`/`projects`/`cost_min`/`cost_max`/`rebuild_min`); a malformed value is `400`. See [Browse filters](#browse-filters). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). |
| `GET /api/conversations/facets` | Conversation-viewer filter facets — sorted distinct project labels with per-label conversation counts, for the Filters popover's project multi-select. Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). See [Browse filters](#browse-filters). |
| `GET /api/conversation/<id>` | Conversation-viewer reader — one session's deduped, turn-grouped messages with cost-once. Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). |
| `GET /api/conversation/search` | Conversation-viewer cross-session FTS search (`?q=…&kind=…&limit=…&offset=…`; LIKE fallback when FTS5 is unavailable). `kind ∈ {all, prompts, assistant, tools, thinking}` (default `all`; an unknown value is `400`). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). See [Search depth](#search-depth-177-s6). |
| `GET /api/conversation/<id>/find` | Conversation-viewer in-conversation find (#177 S6) — document-ordered rendered-turn anchors for one open session (`?q=…&kind=…`). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). See [Search depth](#search-depth-177-s6). |
| `GET /api/conversation/<id>/payload` | On-demand "load full tool payload" (#178) — re-reads the source JSONL line to serve the un-capped tool `result` or `input` for one `tool_use_id` (`?tool_use_id=…&which=result\|input`). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). |
| `GET /api/conversation/<id>/media` | On-demand media bytes (#177 S4) — re-reads the source JSONL line to decode and serve one inline image or PDF (`?tool_use_id=…&index=N` for tool-result media, or `?uuid=…&index=N` for user-content media). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2) **plus** a cross-origin Fetch-Metadata check; see [Conversation viewer endpoints](#conversation-viewer-endpoints-plan-2). |
| `GET /api/conversation/<id>/events` | Conversation-viewer live-tail SSE stream — watches only the open session's JSONL file(s) and emits `event: tail` within ~1s of growth (with `: keep-alive` comments while idle). Behind the [transcript gate](#conversation-viewer-endpoints-plan-2). See [Live-tail](#live-tail). |
| `POST /api/sync` | OAuth refresh + snapshot rebuild (chip / `r`). 204 on clean success; 200 + `{warnings:[{code: ...}]}` when refresh-usage returned a non-`ok` status (`rate_limited`, `no_oauth_token`, `fetch_failed`, `parse_failed`, `record_failed`); 503 if another sync is in flight. Origin-vs-Host parity CSRF (see [Threat model](#threat-model)). |

> `/api/conversation/search`, `/api/conversation/<id>/payload`,
> `/api/conversation/<id>/media`, `/api/conversation/<id>/find`, and
> `/api/conversation/<id>/events` are all matched **before** the
> `/api/conversation/<id>` reader, so neither `search` nor a `…/payload` /
> `…/media` / `…/find` / `…/events` suffix is ever treated as a session
> id. `/api/data` carries a
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

**Enriched data contract (#177).** The reader payload carries a richer, additive contract for downstream tool-rendering work. Each assistant `tool_call` block now exposes the full **structured** tool input as `input` (the original argument object, size-bounded so a pathological input can't bloat the payload) alongside an `input_truncated` flag set when any bound clipped a value; the legacy `input_summary`/`preview` fields stay for back-compat. A tool result carries `full_length` — the pre-clip character count of the underlying output — beside the existing capped `text`/`truncated`, so a "showing X of Y" affordance can be built without the full body (the cap was also raised to 16 000 characters). Each assistant turn item carries a `tokens` breakdown (`input`/`output`/`cache_creation`/`cache_read`) drawn from the **same** deduped `session_entries` row its `cost_usd` comes from — note these are the same *source row*, not an arithmetic identity: when a vendor supplies a raw cost it overrides the token-derived math, so never assume `cost == f(tokens)`. Assistant items also surface `stop_reason` and `attribution_skill` / `attribution_plugin` (the skill/plugin that drove the turn) when present, omitted when absent. A parser-populated `search_aux` column plus a parallel `conversation_fts_aux` index quietly denormalize tool input, tool results, and thinking text for future tool-content search — this session builds and maintains the index but adds no search query over it yet (#177 S6 later replaces this groundwork with a split `(text, search_tool, search_thinking)` index that the search/find surfaces query directly — see [Search depth](#search-depth-177-s6)). Every one of these fields is additive: the existing client keeps reading the prior shape unchanged, and the new fields land on existing history the next time the cache syncs (a one-time, lossless re-ingest of the re-derivable conversation cache, gated by the distinct `conversation_reingest_enrichment_pending` flag that migration `007` sets).

**Subagent thread cards (#166).** On modern transcripts the reader surfaces a subagent's *kind* in the thread-card eyebrow (`SUBAGENT · <kind>`, e.g. `SUBAGENT · Explore`) and a dim second line with its result meta — tokens, wall-clock duration, tool-use count, and status (a bare `✓` on success; `✕ error` or `⚠ <status>` spelled out on failure). The kind and meta come from the spawn `Task`/`Agent` `subagent_type` joined to the record-level `toolUseResult` in the query kernel. Older transcripts that predate the capture lack the linkage, so their cards gracefully fall back to the title-only rendering.

**AI titles & tool/subagent descriptions (#193).** The viewer titles each conversation by the **AI-generated title** Claude Code writes for the session (falling back, in order, to the first prompt, the project label, then the session id) — both in the rail and as the reader header, and a title that gets rewritten mid-session updates an already-open reader via the live tail. A **subagent thread** is titled by the spawning `Task`'s `description` when present (both in the thread-card header and the matching outline landmark), falling back to the first prompt of the subagent. A **Bash** tool call shows its own `description` on the dimmed chip line, falling back to the command; the command itself always remains in the expanded `$ …` body. Each label is fallback-guarded, so older transcripts and tool calls without a stored description keep their prior rendering.

**Decision & planning tool cards (#177 S2).** Three decision/planning tools now render as dedicated semantic cards instead of the generic JSON tool chip. `AskUserQuestion` becomes a Q&A card: each question with its header tag and single/multi-select mode, every option laid out, and the option(s) you actually chose highlighted in green (a free-text answer that matches no option renders in its own "your answer" block). `TodoWrite` becomes a checklist card, collapsed by default to a one-line progress preview (`done / total` with a mini progress bar and the current in-progress item), expanding to the full list with completed items struck through, the in-progress item flagged in amber, and pending items dimmed. `ExitPlanMode` becomes a plan card that renders the proposed plan as Markdown (clamped with a "Show full plan ↓" reveal) and badges the outcome as Approved, Rejected, or a neutral Responded — never defaulting to Approved on an ambiguous result. Every other tool keeps the generic chip. The chosen-answer highlight prefers the structured answers captured at ingest and falls back to parsing the harness result string on older transcripts; each card is a `<details>`, so the reader's collapse-all keystrokes still reach it.

**Code & shell tool cards (#177 S3, #178).** The Edit / MultiEdit / Write family and Bash now render as purpose-built cards instead of the generic JSON chip. `Edit`, `MultiEdit`, and `Write` render as a **unified diff card**: a header with the file's basename (bold) and dim parent dir, a `+N −M` added/removed stat, a `replace all` tag when the edit replaces every match, and an `N edits` tag for MultiEdit. The body is a unified red/green diff with relative line numbers in the gutter and intra-line word-level highlighting — the exact words that changed are brightened on the removal and addition rows. Unchanged context lines get full per-language syntax highlighting (inferred from the file extension); the changed lines carry the red/green tint and word emphasis as plain text. MultiEdit shows one diff hunk per edit under an `edit k of n` divider, and Write shows the new contents as an all-added `wrote N lines` block (create-vs-overwrite isn't knowable from the input, so it's never labeled "new file"). Beneath each diff a collapsed `result · cat -n snippet` sub-panel carries the file's **real** line numbers (the diff's own gutter is relative, since absolute offsets can't be derived from the old/new strings alone). `Bash` renders as a **terminal**: a `$ <command>` prompt (bash-highlighted) over the output, with any `stderr` split into a red block and an `● error` or `■ interrupted` status badge; the small fraction of outputs carrying ANSI color codes are honored (foreground SGR colors), and everything else is plain monospace. Legacy transcripts captured before the Bash stream split degrade to a single merged terminal block, and a request-only Bash (no recorded result) shows the command alone. When an edit's input or a result was clipped at ingest, a **load-full** affordance ("load full input" / "load full output") fetches the un-capped payload on demand from the `/api/conversation/<id>/payload` route (#178), which re-reads the original JSONL line behind the same transcript gate — the diff is recomputed or the terminal re-rendered from the full text, and a rotated/deleted source surfaces a "source no longer available" note. Every card is a `<details>`, so the reader's collapse-all (`[` / `]`) and `j` / `k` navigation still reach it; the load-full spinner honors `prefers-reduced-motion`.

**MCP, web & inline media (#177 S4).** MCP tool calls, web tools, and images now render with purpose-built chrome instead of falling through to the generic JSON chip. An MCP call chip leads with the **action** (`browser_take_screenshot`) and a quiet pill carrying the friendly server name, with a per-server icon — `playwright`, `chrome` (claude-in-chrome), `computer` (computer-use), and `codex` each get a dedicated glyph, and any other MCP server gets a generic plug icon plus its raw server name; the full original namespaced tool name (`mcp__plugin_playwright_playwright__browser_take_screenshot`) stays in the chip's title tooltip and the expanded request panel. A malformed MCP name with no action segment degrades to a server-only chip without breaking, and every non-MCP tool that has no dedicated card renders exactly as before. `WebFetch` becomes a semantic source card: a header with the fetched **domain** and an HTTP status chip (green for `2xx`/`3xx`, red for `4xx`/`5xx`; absent on older transcripts captured before the status was recorded), labeled `url` (an external link) and `prompt` fields, and the fetch summary rendered as Markdown — clamped with a "Show full summary" reveal, and a "load full summary" affordance through the `/payload` route when the result was clipped at ingest. `WebSearch` becomes a card with the quoted query, a result-count chip, and a clickable list of the `{title, url}` results (the first ten, then a "+ N more results" expander) with each result's domain shown dim beneath its title; an older transcript with no captured link list falls back to the plain result-text panel. Both web cards only ever render a clickable link for `http:`/`https:` URLs — any other scheme (including `javascript:`) renders as inert text — and neither card makes any outbound request (no favicon fetches); the only network traffic is a link you click yourself. The MCP server/action split and the web captures land on existing history the next time the cache syncs (the one-time reingest described below); older rows simply keep today's rendering until then.

**Inline media & the media route (#177 S4).** Images that previously showed only as a byte-count badge — or, for screenshots returned inside an MCP `tool_result`, were dropped entirely — now render inline. A figure shows the image clamped to a readable height with a quiet caption row (`media type · ~size · open full size ↗`), where the open link loads the full-resolution image in a new browser tab; images load lazily (`loading="lazy"`), so a screenshot-heavy session only fetches the images you scroll near. PDF documents are not embedded inline — they keep an upgraded badge with an `open ↗` link that opens the file in a new tab. The pixel data is never written to `cache.db`: the figure's `src` points at the new `GET /api/conversation/<id>/media` route, which **re-reads the original session JSONL line on demand** (the same mechanism as `/payload`), decodes the base64, and streams the raw bytes — so the cache does not grow and there is no new at-rest copy of your screenshots. Media is addressed by exactly one of `?tool_use_id=<id>&index=N` (an image/PDF inside that tool's result) or `?uuid=<uuid>&index=N` (an image/PDF you attached to a message), where `index` is the ordinal among the media items in that content list; supplying both keys, neither, or a non-integer/negative index is a `400`. The route sits behind the **same fail-closed transcript gate** as the other conversation routes (loopback by default, LAN only under `dashboard.expose_transcripts`, IP-literal `Host` only — a spoofed/rebinding `Host` is `403`) and, because an image can be embedded cross-origin in a way the JSON routes cannot, additionally rejects any request whose browser-set `Sec-Fetch-Site` cross-origin marker is present and not one of `same-origin`/`same-site`/`none` with a `403` (an absent header — `curl`, older browsers — is allowed; this is defense-in-depth layered on the primary gate). Only the allowlisted media types `image/png`, `image/jpeg`, `image/gif`, `image/webp`, and `application/pdf` are served, always with the canonical `Content-Type` for the matched type (never an echoed transcript string) and `X-Content-Type-Options: nosniff`; images additionally get `Content-Security-Policy: default-src 'none'`, while PDFs instead get `Content-Disposition: inline; filename="attachment-N.pdf"` (a CSP sandbox would break native PDF viewers). A non-allowlisted media type is a `404` (no existence oracle), a source line whose file has been rotated/deleted or whose payload is no longer decodable is a `410`, and a pathologically large payload is rejected at a `413` before any decode. The reader degrades gracefully on every error path: a figure that fails to load (`404`/`410`/`413`) falls back to the byte-count badge with a "source no longer available" hint, and a row that predates the reingest — and so carries no media ordinal to address — shows the badge until the reingest backfills it.

**One-time media reingest (#177 S4).** Like the earlier conversation-enrichment passes, the media placeholders and web captures are backfilled onto your existing transcript history by a one-time, lossless re-ingest of the re-derivable conversation cache, gated by a distinct `conversation_media_reingest_pending` flag that cache migration `009` sets on first open after upgrade. It runs on the next `cache-sync` (foreground) or in the background on the next dashboard sync, resumable per the usual sorted-path cursor, and clears its flag when complete; until then, MCP/web/image rows render in their pre-S4 form. `cache-sync --rebuild` is the clean one-shot way to force it through.

**Injected (`isMeta`) content.** Lines Claude Code injects into a transcript that you did not type — a skill's body (when an assistant turn invokes the `Skill` tool, or a `SessionStart` skill), git-context blocks, "Continue from where you left off.", pasted-image placeholders, slash-command plumbing — are never rendered as a "You" prompt. They collapse into quiet, collapsed-by-default disclosures: slash-command plumbing keeps the `System marker` pill, and everything else becomes a neutral `Injected context` pill. A skill body invoked via the `Skill` tool now **folds into its Skill tool chip** — the chip itself expands to the rich-Markdown body (the redundant "Launching skill: <name>" result is dropped), so the skill reads as one nested unit inside the turn rather than a detached pill below it. A `SessionStart` skill (no `Skill` tool call) keeps the standalone `Skill content · <name>` pill (the name is the skill's directory basename; its body still renders as full Markdown when expanded). Injected bodies are excluded from derived titles and full-text search. The classification — and the skill-body fold — land on existing history the next time the cache syncs (a one-time, lossless re-ingest of the re-derivable conversation cache).

**Reader UX (#175, #176).** While a conversation's first page loads, the reader shows an animated spinner (suppressed under `prefers-reduced-motion`). When you scroll deep into a tall turn so its start is off the top of the reading column, an unobtrusive floating "↑ Top of turn" button appears at the bottom-right of the reader; clicking it scrolls that turn back to its start (reduced-motion aware). Nothing floats over the reading column itself — the button is anchored clear of the bottom-center "↓ N new" pill and is hidden again on a session switch. The assistant model renders as a colored `.chip` (matching the rest of the dashboard) rather than plain text, with no chip shown when the model is unknown. Finally, the open conversation **live-tails**: once you've paged to the end, new turns from an active session appear within about a second with no manual reload — the reader sticks to the newest turn if you're already at the bottom, or surfaces a floating "↓ N new" pill (click to jump to the latest) if you've scrolled up, and the cost/model totals update along with the new turns. Live-tail engages only after the conversation is fully paged; already-loaded turns are not re-fetched. See [Live-tail](#live-tail) for how the ~1s latency is delivered and how to opt out.

**Session navigation & insight (#177 S5).** The reader gains a full-session navigation layer that stays oriented even in a long transcript you've only partially paged in. A collapsible **outline sidebar** (toggle with `o`, or the `☰ Outline` button in the reader header; the open/closed state persists across sessions) lists every turn as a compact landmark — prompts, assistant replies, subagent threads, plan/question moments, and errors — with nested thinking entries, and scroll-sync highlights the entry for whatever turn sits at the top of the reading column. Clicking an outline entry jumps to that turn, paging the detail in first if it isn't loaded yet and expanding a collapsed subagent thread when the target lives inside one. A **stats overview** at the top of the outline summarizes the whole session: turn counts, wall-clock duration, total tokens, cost, the models used, and a tool-use histogram, plus an error row that jumps to the first error (and hides itself when the session has none). **Jump-to-next** keys move you between landmarks from your current position with no wrap-around: `e`/`E` for errors, `u`/`U` for prompts, `b`/`B` for subagent threads, and `p`/`P` for plan/question turns (uppercase = previous); a glyph-cluster of clickable buttons mirrors each, carrying a count. **Focus modes** filter the reading column without touching the outline: `v` cycles All → Chat → Prompts → Errors (a segmented control in the header does the same), and runs of hidden turns coalesce into a quiet `· N hidden ·` marker you can click to drop back to All at that spot; a jump-to-next or outline click whose target the current mode would hide resets to All before landing, never a silent no-op. Finally, each turn now carries a **timestamp** — a quiet `· HH:mm` at the end of its header (rendered in your `display.tz`, with the precise instant in a tooltip) — and the reader inserts **gap and day markers** between turns: a `⏸ 42 min later` rule when adjacent turns are ten or more minutes apart, a `— Jun 13 —` rule when the calendar day changes, or a combined `⏸ 9.5 h later · Jun 13` when both apply. Assistant turns that carry token data extend their cost footer to break out usage — `$0.0214 · in 1.2k · out 4.8k · cache 310k` — with the four exact counts in a tooltip; a turn with token data but no attributable cost shows a tokens-only footer, and an un-reingested turn keeps today's cost-only footer.

### Browse filters

The Browse rail narrows the conversation list by four axes through a compact **`Filters ▾`** popover beside the search box. **Date** matches a session's **last activity** — the same instant the rail sorts and groups by, so the date-section headers, the `recent` order, and the filter all agree; it offers presets (This month / Last month / Last 7 days / Pick month) plus a from→to range, with "this/last month" resolved in your `display.tz`. **Project** is a multi-select that matches ANY of the chosen project labels (its options come from the `/api/conversations/facets` endpoint, each shown with its conversation count). **Cost** filters on the session's total USD with an optional minimum and/or maximum (quick `≥$1` / `≥$5` / `≥$10` presets). **Cache rebuilds** filters on the per-session cache-rebuild count with a threshold (`≥1` / `≥3` / `≥5`, or a custom `≥ N`) — the same count the [session modal's Cache rebuilds section](#cache-rebuilds-in-the-session-modal) and the in-reader chips report. Axes combine with AND; active filters render as small removable chips under the search box (e.g. `Jun 2026✕`, `cctally-dev✕`, `≥1 ♻✕`) with a "Clear all", and numeric/text inputs apply live (debounced). Filtering is **server-side** — the predicates are pushed into the list query's SQL before paging, so pagination and the result count stay correct over the filtered set.

Filters apply to **Browse only**: when a full-text search needle is active the rail switches to search results and the Filters button is disabled (filters stay set and re-apply when the search box clears). Filter state is session-only — it resets on reload. While the conversation cache is still building its per-session rollup on a brand-new or `--no-sync` dashboard, the project/cost/rebuild axes show a brief muted "indexing — some filters unavailable" note and only the date axis applies; this self-corrects after the first full sync.

### Jump to latest

The reader header carries a **`Jump to latest ↓`** action (and the `End` key) that takes you straight to the conversation's most recent turn. It reuses the same jump pipeline as an outline click — paging the detail forward to the end (with a loading state on long conversations), then scrolling the final turn into view with the usual flash. Unlike the outline jumps, jump-to-latest pages **all the way to the end** with no page cap, so it reaches the last turn even in very long conversations. Landing at the bottom parks you in the stick-to-bottom position, so a live session's subsequent appends keep following automatically — jump-to-latest and the live-tail "↓ N new" pill are complementary. The control is hidden for an empty conversation, and the `End` key is suppressed while a filter input or a modal is focused.

### Search depth (#177 S6)

Conversation search now reaches past prose into what a session actually contains — commands, file paths, error strings, and the assistant's thinking — with exact kind facets, result counts, and a browser-style in-conversation find bar. Three surfaces share one consolidated full-text index, so totals, pages, and match badges are exact by construction.

**Rail search kinds, counts, and load-more.** While a search needle is active, a single-select chip row — `All · Prompts · Assistant · Tools · Thinking` — sits between the input and the results. The chips map 1:1 to the `kind` param on `/api/conversation/search`: `All` is unscoped, `Prompts` and `Assistant` scope to prose in your prompts vs the assistant's replies, `Tools` searches tool inputs and results (commands, file paths, error output, recorded answers, Bash stderr), and `Thinking` searches the assistant's thinking blocks. Switching kind aborts any in-flight fetch and restarts at page one. Under the chips, a `aria-live` count line shows `No results`, `{N} results`, or `{N} results · basic search` (the last when the host has no FTS5 and search falls back to the degraded substring mode below). Non-prose hits carry small uppercase match badges (`tool`, `thinking`) in the title row; a hit matching only prose is unbadged, and a turn matching in several columns at once dedups to one row. Results page in fixed chunks: when more remain, a `Load {N} more ({M} remaining)` button at the list end fetches the next page and appends (capped at 50 per click), with no infinite scroll. Search-as-you-type matches the last word as a prefix, so `cache.d` finds `cache.db` before you finish typing.

**The find bar.** With a conversation open, pressing `/` opens a floating find pill in the top-right of the reading column (Cmd+F style, zero layout shift); with no conversation open, `/` keeps focusing the rail/Sessions search input as before, and an open modal swallows the key. The input owns `Enter` (next match), `Shift+Enter` (previous), and `Esc` (close, restoring focus to the transcript so `j`/`k` resume); with the bar open but the input blurred, `n`/`N` step matches and `Esc` closes. Navigation wraps around, and each step runs the same jump flow as an outline click — it pages the target turn in if it isn't loaded yet, scrolls it into view with a flash, and (when the matched anchor is a tool or thinking hit) imperatively expands that turn's collapsed disclosures so the match is visible; a target the active focus mode would hide resets the mode to All before landing. An exact `k / N` counter (`aria-live`) shows your position, a `· first 500` note appears when a conversation has more than 500 matching turns (the anchor list caps there with `anchors_truncated: true`), the current match's kind badge shows when it matched in tool/thinking content, and a `basic search` hint shows in LIKE mode. While find is active, your search terms are wrapped in `<mark>` in the rendered prose — case-insensitive — skipping code blocks and structured tool cards (the last-term-as-prefix glob applies to which turns *match*, server-side; the prose highlight marks the literal terms you typed). The match list is a point-in-time snapshot: live-tail arrivals do not silently refresh it; re-running the query (typing, or `Enter` on an unchanged needle) refreshes.

**Endpoints.** Both surfaces sit behind the same fail-closed [transcript gate](#conversation-viewer-endpoints-plan-2) (loopback by default, LAN only under `dashboard.expose_transcripts`, IP-literal `Host` only). `/api/conversation/search` gains `kind` (default `all`; an unknown value is `400`) and each hit gains an additive `match_kinds: ["tool", "thinking"]` array. The new `GET /api/conversation/<id>/find?q=…&kind=…` returns `{"anchors": [{"uuid", "match_kinds"}], "total", "anchors_truncated", "mode": "fts"|"like", "search_depth"}` — rendered-turn anchors in document order (matched physical rows fold onto their rendered turn, so a tool-result match collapses into its owning assistant turn and counts once), `total` over rendered turns, anchors capped at 500. An unknown session is `404`, an unknown `kind` is `400`, and an empty query returns zero anchors. Both responses carry an additive `search_depth` field (no schema-version bump — the additions are backward-compatible).

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
