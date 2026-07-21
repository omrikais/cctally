# Backend performance: the read model, hot paths, and invariants

This is the qualitative contract for cctally's backend read model — what owns which data, where request time goes, and which invariants must never break. It is deliberately a public architectural doc (a contract, not secrets), consolidating knowledge that previously lived only in scattered gotchas docs and session memory. It cross-links to [architecture.md](architecture.md) and the private `docs/*-gotchas.md` files rather than duplicating them.

Quantitative budgets (how many milliseconds a warm rebuild "should" take) are **not** here — those are M3's committed benchmark baselines. This doc is the shape; the numbers live with the benchmarks. For live introspection, use the opt-in instrumentation described in [Introspection](#introspection-cctally_perf_trace--apidebugbackend).

## 1. Read-model ownership

cctally has three tiers of state. Knowing which tier owns a fact tells you whether it is authoritative, re-derivable, or a per-process accelerator — and therefore what you may safely rebuild.

| Tier | Store | Owns | Re-derivable? |
| --- | --- | --- | --- |
| Authoritative | `stats.db` | User/runtime facts: weekly usage snapshots, percent milestones, week-reset events, weekly credit floors, budget milestones. | **No** — the source of truth. Losing it loses recorded history. |
| Core derived read model | `cache.db` | Compact Claude/Codex accounting entries and cursors, quota observations, Codex thread identity, and the `mutation_seq` change-signal counters. | **Yes** — re-derived from local JSONL by `cache-sync --rebuild`; direct readers may fall back to JSONL where documented. |
| Transcript derived read model | `conversations.db` | Claude prose, Codex physical/normalized events, browse rollups, file-touch axes, AI titles, and FTS indexes. | **Yes** — independently re-derived from the same JSONL without blocking the core cache. |
| Per-process accelerators | dashboard in-memory caches | Signature-keyed rebuild state in `bin/_lib_snapshot_cache.py`: the four reconcile caches (weekref cost, projects-envelope, Bug-K segment, cache-report per-day) plus the Group A/B bucket + session caches and the idle-dispatch `(signature, snapshot)` memo. | **Yes** — dropped on process exit; re-warmed on the next rebuild. Never persisted. |

Above those stores sit the **endpoint groups** the dashboard serves: the snapshot/SSE spine (`/api/data`, `/api/events`); the conversation viewer (browse/search/reader/find/live-tail under `/api/conversation*`); share/export; and doctor/update. Each group reads the tiers above but never writes authoritative state on a GET.

## 2. Hot-path map

The map below is written in the **same phase vocabulary as the instrumentation** (section 2 of the Session A spec), so this doc and `/api/debug/backend` name things identically. A phase wraps a structural seam, never a per-row loop; row volume is captured as a `count`, not as N timed phases.

### The snapshot spine (`_tui_build_snapshot`)

Every dashboard rebuild is a three-path dispatch keyed on a cheap composite `signature` (MAX-id descents over `cache.db` + `stats.db`, the reset-event change-signal, and a generation counter):

- **Idle** — signature unchanged and no wall-clock day/week/month boundary rolled over ⇒ reuse the prior snapshot's heavy rows, re-patch only time-derived fields, and return. An idle dashboard sits near 0% CPU. Phase: `idle-decision`.
- **Warm/cold rebuild** — signature moved ⇒ run the builders. Under the `snapshot` root the phases are: `sync` (the once-per-rebuild ingest, which nests the `sync_cache` seams below), `signature`, the four `reconcile.{weekref, projects_env, bugk, cache_report}` phases (each carrying its `use_*_cache` hit boolean as meta — the only place those build-time locals are observable), the builders `build.{current_week, forecast, trend, sessions, milestones, weekly_periods, monthly_periods, projects_envelope}`, then `doctor` and `envelope.precompute`.

The reconciles run **once per rebuild** (not once per SSE client): they refresh the signature-keyed accelerator caches so each builder can opt into an incremental read instead of a full-window walk. A failed or absent reconcile always falls back to direct compute — byte-identical output, just slower.

### Ingest (`sync_cache`)

Ingest is the other hot path, shared by every JSONL-reading command through the read-through delta cache. Under the `sync_cache` root, the core path acquires only `cache.db.lock`, discovers files, and writes compact accounting/cursor state. Transcript parsing, file touches, FTS triggers, and browse-rollup recompute run later through `sync_claude_conversations` / `sync_codex_conversations` on `conversations.db` and their independent locks. The core commit therefore remains available even when transcript work is slow or unavailable.

As of #279 S2, `cctally cache-sync` traces one shared `cache-sync` root with `sync_cache` (the Claude ingest) and `sync_codex_cache` (the Codex ingest) as children, so a single flushed tree carries both vendors. The Codex sync now carries the same coarse `flock`/`discover`/`walk` seams as the Claude sync (its `walk` counts `files_processed`, never per-row).

### Cache-state diagnostics

Core signature legs and accounting row counts are queryable from `cache.db`; transcript row counts and rebuild flags belong to `conversations.db`. They are **not** timed phases. `/api/debug/backend` computes the available diagnostics at request time even when tracing is off.

## 3. Invariants that cannot be broken

These hold regardless of performance work; a change that violates one is a bug even if it is faster.

- **Privacy gate.** The `/api/debug/backend` diagnostic is loopback-only, always — its primary check is the unspoofable loopback TCP peer, with an IP-literal loopback `Host` as anti-DNS-rebinding defense-in-depth. It never consults `dashboard.expose_transcripts`. (The transcript endpoints have their own, deliberately more permissive, gate.)
- **No transcript text in diagnostics.** The diagnostic surfaces leak only timings, counts, flag names, signature legs, and already-safe cache-table names — never prompt/prose/paths.
- **Read-only, no-side-effect doctor.** `doctor` gathers and reports; it never heals, migrates, or writes.
- **Both derived stores are re-derivable.** `--rebuild` may reconstruct them from JSONL, but code must never unlink a live SQLite main/WAL/SHM family.
- **Byte-identical CLI stdout.** Instrumentation and diagnostics change no command's stdout or `--json` output. `CCTALLY_PERF_TRACE` writes only to stderr. No golden moves.
- **`mutation_seq` change-stamp correctness.** An id-stable in-place finalization UPSERT still advances the per-file `mutation_seq` leg, so the dashboard leaves the idle path and recomputes exactly the affected bucket. A signature that fails to move on a real data change silently serves stale rows.
- **Leading-and-trailing-edge cache eviction.** Signature-keyed accelerator caches must evict at both edges of their window — a leading-edge-only eviction leaves stale trailing buckets that a later read wrongly reuses.

## 4. Introspection (`CCTALLY_PERF_TRACE` + `/api/debug/backend`)

The read model is instrumented by an opt-in, off-by-default phase collector (`bin/_lib_perf.py`). With `CCTALLY_PERF_TRACE` unset it is invisible — no allocation, no timing, no output. Two surfaces render the same nested phase tree:

- **CLI stderr trace.** `CCTALLY_PERF_TRACE=1 cctally cache-sync` prints an indented `backend-perf:` tree (the `sync_cache` seams) to **stderr**; stdout stays byte-identical. The hidden `tui --render-once` path flushes the `snapshot` tree the same way, for profiling a single build without running the server.
- **Loopback dashboard endpoint.** `GET /api/debug/backend` returns the **last completed** traced build's timing tree (present only if the dashboard was started with `CCTALLY_PERF_TRACE=1`; otherwise `null` with a `tracing_disabled` note) plus on-demand cache-state and dataset row counts. As of M5 the last-completed tree can be a **conversation** trace as well as the `/api/data` snapshot: the assembly-relevant conversation routes (list, search, outline, find, export, prompts, detail) stash their `endpoint.conversation_*` tree on exit (the long-lived `/events` SSE and the `/payload`/`/media` binary routes deliberately do **not**, so they can't clobber the last useful assembly trace). Distinguish which build you're looking at by the root phase name.

The endpoint ships `schemaVersion: 1` for basic tooling but is **documented unstable**: phase names, nesting, and fields may change without a version bump. It is a diagnostic, not a consumer API — treat it structurally, never byte-golden its phase names or timings.

### Where the numbers live

This doc is the qualitative contract. Concrete budgets — target warm-rebuild time, ingest throughput, idle CPU — are M3's committed benchmark baselines, measured with the same phase vocabulary above. When you need live numbers for the machine in front of you, read them from `CCTALLY_PERF_TRACE` or `/api/debug/backend`; when you need the regression thresholds, read the M3 baselines.

## 5. Conversation assembly: measured cost & materialization decision

The conversation reader assembles a whole session from `conversations.db`'s `conversation_messages` on **every** call — `_assemble_session` runs the full dedup → turn-grouping → fold → sweep → meta-classify → cost/usage-stamp pipeline over the entire session, and `get_conversation` (each page), `get_conversation_outline`, `get_conversation_export`, `get_conversation_prompts`, and `find_in_conversation` (after a non-empty match probe) all funnel through it. Nothing is materialized or cached across calls. M5's mandate was measurement-first: instrument that path (deep `assemble.*` seams, §4), sweep its cost across a synthetic size ladder, find the threshold where whole-session assembly becomes human-perceptible, and only then decide whether to materialize rendered turns in `conversations.db`.

### The measurement

`cctally-bench --assembly-scan --assembly-ladder-scale large` builds an isolated `assembly` fixture — one synthetic session per turn-count rung — and times each rung against it. The committed evidence run below is `bench/baselines/assembly.json` (darwin-arm64, cctally 1.64.0, median-of-5, warmup discarded). Structural columns (counts, bytes) are deterministic and goldenable; the `*_ms` timings are machine-variant and advisory — never asserted by a test. `assembled_items_bytes` is the whole assembled item list serialized (a **materialization-footprint proxy**, not an HTTP payload — the reader caps `limit` at 1000 items); `page_bytes@1000` is the real reader payload at the largest page.

| Rung (turns) | Messages | Items | `assemble_ms` | `outline_ms` | `find_hit_ms` | `open_pair_ms` | `assembled_items_bytes` | `page_bytes@1000` |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 250 | 500 | 500 | 3.0 | 3.9 | 9.4 | 7.2 | 270,840 | 273,389 |
| 500 | 1,000 | 1,000 | 6.0 | 7.6 | 11.5 | 13.8 | 537,632 | 542,129 |
| 1,000 | 2,000 | 2,000 | 12.1 | 15.8 | 19.1 | 28.6 | 1,084,699 | 550,529 |
| 2,000 | 4,000 | 4,000 | 25.0 | 33.7 | 34.7 | 60.6 | 2,161,237 | 547,421 |
| 4,000 | 8,000 | 8,000 | 53.5 | 67.9 | 65.0 | 125.1 | 4,326,104 | 547,112 |
| 8,000 | 16,000 | 16,000 | 131.4 | 161.2 | 146.9 | 294.4 | 8,742,068 | 552,662 |

`open_pair_ms` is `detail_tail_ms + outline_ms` — the two assembly-backed reads a reader fires to open a conversation (the detail page and the outline rail), each of which re-assembles the whole session independently.

### Threshold analysis

Assembly cost scales approximately **linearly** with message count, at roughly **8.2 µs per message** for a bare `_assemble_session` (a mild super-linear tail appears at the top rung — doubling 8,000→16,000 messages costs 2.46×, not 2×, from the growing per-item dict/JSON work). Fitting each timing against `msg_count` and solving for the `ASSEMBLY_VISIBLE_MS` = **100 ms** visibility budget (a human-perceptible fraction of interactive response):

- **`_assemble_session` alone** crosses 100 ms at ≈ **12,700 messages (~6,350 turns)**.
- **A reader "open"** (`open_pair` = detail page + outline, ~18.5 µs/msg) crosses 100 ms at ≈ **5,900 messages (~2,950 turns)** — the earliest crossing, because it pays assembly twice.
- **`find` on a matching token** (assembly + its own walk) crosses at ≈ **11,100 messages (~5,570 turns)**; **`outline`** at ≈ **10,400 messages (~5,180 turns)**.

The scan surfaces the decisive shape: **`page_bytes@1000` plateaus at ~550 KB** across every rung (the reader caps `limit` at 1000 items, so payload bytes are bounded regardless of session size), while **`assembled_items_bytes` grows without bound** to 8.3 MB at 16,000 items. Pagination caps the *wire* cost but **not the assembly cost** — every reader page, plus each outline and each non-empty find, re-pays the full whole-session `_assemble_session`. That repeated re-assembly, not payload size, is the only thing a `conversation_turns` materialization would remove.

### Ruling: DEFER the materialization (no-go for now)

Per the design's Q1 the default is to **defer** building a materialized turn store unless the numbers clearly demand it, and they do not. Every crossing above sits **far past** the size of a typical Claude Code session: a reader open stays under the 100 ms budget until ~2,950 turns, and a bare assemble until ~6,350 turns, whereas ordinary sessions run in the tens-to-low-hundreds of turns and even a heavy resumed session rarely reaches a few thousand. For the overwhelming majority of real sessions, assembly is **invisible** (single-digit-to-low-tens of milliseconds), and the reader already bounds the network cost at ~550 KB/page. Materializing rendered turns is a substantial, risk-bearing build (parity-tested against the live assembler, `flock`-gated, kept re-derivable) whose marginal benefit only accrues to the rare multi-thousand-turn session — a classic premature optimization at today's data shape.

**Decision: no `conversation_turns` table, no schema migration.** The trigger to revisit is empirical, not architectural: if real-session data shows a **meaningful fraction** of sessions above ~3,000 turns (~6,000 messages, where a reader open crosses the visibility budget), re-open materialization as a **gated follow-up session** (mirroring M4's leash) — parity-tested, `flock`-gated, re-derivable turns — grounded in that measured distribution rather than a synthetic ladder. Until then the standing advice for a pathologically large session is the cheap lever already in place: the reader's page cap bounds payload, and `--assembly-scan` is the harness to re-measure the day the distribution shifts. Re-run it with `cctally-bench --assembly-scan --assembly-ladder-scale large` and compare against `bench/baselines/assembly.json`.

## 6. Dashboard startup: bind-before-build & the A3 persistence decision (#278 Theme A)

Before #278, `cmd_dashboard` was a strict sequential chain: `_dashboard_initial_snapshot` ran a **full** `_tui_build_snapshot` *before* `_QuietThreadingHTTPServer` was even constructed, and `socketserver.TCPServer.__init__` binds+listens synchronously in that constructor — so on a heavy-history instance the entire ~2 s cold aggregation sat in front of the socket bind, and the port could not accept a TCP connection until aggregation finished. #179 had deferred the *ingest* half of cold start (`skip_sync=True` moved `sync_cache` to the background thread), but the *aggregation* half still ran synchronously pre-bind.

**A1 — bind before build.** On a normal launch `_dashboard_initial_snapshot` now builds a **cheap partial** seed: only the two sub-ms headline panels (`current_week` + `forecast`, via the individual builders with `skip_sync=True`) plus the real doctor + envelope-config precompute, `hydrating=True`. It is built via the individual builders, **not** `_tui_build_snapshot`, so it never writes dispatch state or touches the accelerator caches (the first background tick therefore sees `prior_key=None` → a full cold build; idle-reuse can never serve the partial). The heavy panels hydrate over SSE from the background thread's first full build; the client renders a per-panel loading skeleton while `hydrating && <panel empty>`. Under `--no-sync` the seed stays the full pre-bind build (no background thread would ever fill a partial).

**A2 — progressive first-run fill.** The dashboard's locked rebuild closure decouples the ingest from the build on the `skip_sync=False` path: it runs `sync_cache` **standalone** with a throttled progress callback (`T = 2 s`, completion-measured, suppressed under `CCTALLY_PERF_TRACE`), then builds the final snapshot with `skip_sync=True`. The partials republish over the latest-wins SSE hub as files land, so a first-run / long-gap dashboard fills progressively instead of empty-then-jump. Self-limiting: a warm returning user's sync finishes under `T`, so the throttle never fires (exactly one publish).

### The measurement

Instrumentation first (§0): the six previously-unwrapped `_tui_build_snapshot` builders are now `_perf.phase`-instrumented, so a `--trace` cold build attributes them instead of dropping ~370 ms into "unattributed". A traced large-fixture cold build (`cctally-bench --scale large --trace`) confirms all six: `build.daily` ≈ 274 ms and `build.cache_report` ≈ 94 ms were the bulk of the previously-lost time; `build.weekly_history` / `build.blocks` / `build.alerts` / `build.five_hour_milestones` are sub-ms.

`snapshot.cold` is **~unchanged** — A1 *moves* the build off the pre-bind path, it does not make the builders faster. `cctally-bench --scale large --compare` reads 2060.6 ms baseline → 2124.4 ms current (+63.8 ms, status **OK**, within tolerance); the `_perf.phase` wraps are a near-noop off-trace.

Process-level startup on the heavy `large` bench fixture (295 K entries / ~720 MB `cache.db`; fresh-process subprocess launch → time-to-first-TCP-accept and time-to-first-full-data SSE frame; both `CCTALLY_DATA_DIR` + `CLAUDE_CONFIG_DIR` isolated to a fresh copy):

| Metric | Pre-change (full pre-bind build) | Post-change (A1 cheap seed) |
|---|---:|---:|
| time-to-accept (TCP bind) | ≈ 5.0–5.2 s | ≈ 1.9–2.2 s |
| time-to-first-data (headline panels) | ≈ 5.0–5.2 s (same as accept) | ≈ 2.0 s (SSE seeds the partial on connect) |
| time-to-full-data (heavy panels via SSE) | ≈ 5.0–5.2 s | ≈ 4.9–5.2 s |

A1 cut time-to-accept from ~5.1 s to ~2.0 s (~60 %), and the headline panels now paint ~3 s before the heavy panels. The pre-change figure is measured via `--no-sync` (which always builds the full snapshot before the bind — the same "aggregation-before-bind" shape the normal launch had). The residual ~2 s time-to-accept is **fixed process overhead** — Python module import, the startup self-heal, and the one-time SQLite migration-open of the 720 MB fixture `cache.db` — **not** aggregation (which A1 moved to the background). The bind-timing regression test (`tests/test_dashboard_responsive_startup.py`) asserts the robust, machine-independent property directly: the socket accepts **well before** the full-data SSE frame arrives (bind precedes full data by ≥ 1 s), which is naturally RED under the pre-change full-seed (time-to-accept ≈ time-to-full-data).

### Ruling: DEFER A3 (durable snapshot persistence — no-go for now)

A3 would persist the full snapshot across restarts (keyed by `SnapshotSignature`, with the process-local `generation` leg treated as provisional-pending-one-revalidation-pass since it resets to 0 on a fresh process), so the ~2 s background aggregation is skipped on the next launch and time-to-full-data collapses toward time-to-accept.

**It is not justified today.** The headline panels are instant (~2 s time-to-accept, dominated by fixed process overhead A3 would not touch), and the heavy panels hydrate progressively (A2) ~3 s later while the user is still orienting — not an empty-then-jump. Crucially, of the ~5 s time-to-full-data on the heaviest synthetic fixture, only the ~2.1 s `snapshot.cold` aggregation is what A3 targets; the rest is the same fixed import + one-time 720 MB migration-open that persisting a snapshot cannot remove. A3 is a substantial, risk-bearing build (a durable snapshot store that must stay parity-correct with the live builder, invalidate exactly on signature change, and revalidate the reset-to-0 `generation` leg once per fresh process) whose marginal benefit — shaving a background ~2 s that lands *behind* an already-interactive first paint — does not clear that bar. Classic premature optimization at today's data shape.

**Decision: no durable snapshot store, no schema change.** The trigger to revisit is empirical: if real-instance evidence shows the **background full build** (`snapshot.cold`) growing beyond **~5 s** — the point where the ~2 s-to-headline + progressive-hydration UX starts to read as broken because the *aggregation itself* (not fixed startup overhead) is the wait — re-open A3 as a gated follow-up (mirroring the M4/M5 leashes), grounded in that measured build time. Until then, re-measure with `cctally-bench --scale large --compare` (build time) and the `tests/test_dashboard_responsive_startup.py` subprocess timing (startup); the standing levers are A1 (headline-first paint) + A2 (progressive fill).
