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
| Per-process accelerators | dashboard in-memory caches | Signature-keyed rebuild state in `bin/_lib_snapshot_cache.py`: the reconcile caches, bucket/session caches, the Codex dirty-path accounting population, and the idle-dispatch `(signature, snapshot)` memo. | **Yes** — dropped on process exit; re-warmed on the next rebuild. Never persisted. |

Above those stores sit the **endpoint groups** the dashboard serves: the snapshot/SSE spine (`/api/data`, `/api/events`); the conversation viewer (browse/search/reader/find/live-tail under `/api/conversation*`); share/export; and doctor/update. Each group reads the tiers above but never writes authoritative state on a GET.

## 2. Hot-path map

The map below is written in the **same phase vocabulary as the instrumentation** (section 2 of the Session A spec), so this doc and `/api/debug/backend` name things identically. A phase wraps a structural seam, never a per-row loop; row volume is captured as a `count`, not as N timed phases.

### The snapshot spine (`_tui_build_snapshot`)

Every dashboard rebuild is a three-path dispatch keyed on a cheap composite `signature` (MAX-id descents over `cache.db` + `stats.db`, the reset-event change-signal, and a generation counter):

- **Idle** — signature unchanged and no wall-clock day/week/month boundary rolled over ⇒ reuse the prior snapshot's heavy rows, re-patch only time-derived fields, and return. An idle dashboard sits near 0% CPU. Phase: `idle-decision`.
- **Warm/cold rebuild** — signature moved ⇒ run the builders. Under the `snapshot` root the phases are: `sync` (the once-per-rebuild ingest, which nests the `sync_cache` seams below), `signature`, the four `reconcile.{weekref, projects_env, bugk, cache_report}` phases (each carrying its `use_*_cache` hit boolean as meta — the only place those build-time locals are observable), the builders `build.{current_week, forecast, trend, sessions, milestones, weekly_periods, monthly_periods, projects_envelope}`, then `doctor` and `envelope.precompute`.

The reconciles run **once per rebuild** (not once per SSE client): they refresh the signature-keyed accelerator caches so each builder can opt into an incremental read instead of a full-window walk. A failed or absent reconcile always falls back to direct compute — byte-identical output, just slower.

For a Codex-active warm rebuild, cache migration 044's durable accounting
change ledger identifies the dirty `(source_root_key, source_path)` pairs. The
source builder reloads those paths, replaces their prior immutable entries,
and updates only the affected daily/monthly/weekly/session/project/account
groups. A cursor gap, whole-store clear, range regression, or semantic-key
change takes the cold path. The ledger is an invalidation index, never an
authoritative store: `codex_session_entries` remains the re-derivable source
population.

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
- **Codex dirty-path completeness.** Every accounting insert, semantic update, delete, and project-identity metadata change must advance migration 044's accounting sequence and retain both old and new path identities where they differ. A destructive clear emits one full marker. If a consumer cannot prove an unbroken sequence, it must rebuild cold.
- **The sync loop's duty bound.** The background rebuild loop's publish period is never shorter than twice the rebuild it just performed, which caps its CPU use at 50% of one core no matter how large the store grows. No path — including a user's manual refresh — may start a rebuild sooner. See below.
- **The conversation loop's duty bound.** The transcript ingest thread carries the same 50% property, measured over its whole pass — open, both syncs, retention prune and close. Both loops are bounded; neither bound may be expressed through the other's helper. See below.

### The sync loop's duty bound (#313, extended by #583 S2)

The dashboard's background loop rebuilds the snapshot, then cools down before the next rebuild. The cooldown is `max(interval, work)`, where `interval` is `--sync-interval` and `work` is the duration of the rebuild that just finished. So the next rebuild starts at:

```
next_start = (t0 + work) + max(interval, work)
```

which makes the period `work + max(interval, work)`, and therefore never less than `2 * work`. That is the whole point: the loop always rests at least as long as it just worked, so its duty cycle stays at or below 50% of one core. The bound is scale-independent — it does not assume the rebuild is fast, and it degrades gracefully rather than saturating a core as a corpus grows. Consequently `--sync-interval` is a floor on the cooldown and not the period: raising it slows publication, and lowering it below `work` has no effect at all.

#583 S2 added a request-driven start, so a queued manual refresh no longer waits out a full interval. It is bounded by the same rule: the earliest a queued request can begin is `t0 + 2 * work`. That floor is never later than the automatic deadline — when `work >= interval` the two coincide, and when `work < interval` the deadline is strictly later — so a queued request is always served at or before the tick it would otherwise have waited for, and never earlier than the duty bound allows.

### The conversation loop's duty bound (#583 S4)

The dashboard runs a **second** work loop, on its own thread and its own SQLite file: transcript ingest into `conversations.db`. Until #583 S4 it had no scale-independent bound. It computed `interval = max(5.0, --sync-interval)` once and ended every pass with a fixed sleep of that length, which prevents literal 100% duty for finite work but caps nothing below it: a pass costing 30 seconds ran 30-on/5-off, about 86% duty, and that share grew with the store. Measured on a live instance against an 8.28 GB store, the thread was 15.5% of process CPU and system-time dominated (476 s system against 190 s user).

It now uses `_conversation_next_deadline(t0, interval, work)`, which carries the same algebra as `_next_deadline` above, so once `work >= interval` the period is `2 * work` and the thread's CPU duty is capped at 50% of one core regardless of store size. The helper is deliberately a **separate function** rather than a call into `_next_deadline`: the two loops' bounds are independent regressions, and sharing one helper would let a later change to the main loop's scheduling silently remove this one. A test asserts the two currently agree, so a divergence has to be a deliberate act.

`work` charges the **whole** pass — the store open, both provider syncs, the retention prune and the close. Anything measured outside it would be work outside the duty denominator, which is the defect this bound exists to fix. The prune is attempted on every pass whose store OPENED, including one whose sync then failed, and is skipped when the open itself failed: the prune opens its own connection to the same store and would otherwise retry an unopenable open every pass forever.

The cost is published on `/api/debug/backend` under `tick.conversation_sync` and rendered by `cctally dashboard-perf`, which reports the measured one-core share as `sum(cpu_ns) / sum(period_ns)` over the passes carrying a forward period. `period_ns` is the interval from a pass's own start to the **next** pass's start, stamped onto that pass when its successor records, so the newest retained pass carries none and contributes to neither sum. Reading the interval backwards instead — the gap that preceded the pass — pairs each pass's CPU with someone else's interval, and that ratio has no upper bound: a long pass after short ones renders above 100%, past the 50% ceiling this bound guarantees.

The per-pass outcome (`ok`, `store_unavailable`, `error`) describes the store open and the two provider syncs. The retention prune runs after them and swallows its own failures, so a pass that ingested successfully and then failed only in the prune is still recorded as `ok`.

The user-visible consequence, on a store large enough for a pass to exceed the interval, is that transcript backfill lands up to about twice as late. An open conversation is unaffected while its live-tail stream is working; with `dashboard.live_tail` set to `false`, or the stream unavailable, the periodic tick is that conversation's documented fallback and it inherits the same increase.

## 4. Introspection (`CCTALLY_PERF_TRACE` + `/api/debug/backend`)

The read model is instrumented by an opt-in, off-by-default phase collector (`bin/_lib_perf.py`). With `CCTALLY_PERF_TRACE` unset it is invisible — no allocation, no timing, no output. Two surfaces render the same nested phase tree:

- **CLI stderr trace.** `CCTALLY_PERF_TRACE=1 cctally cache-sync` prints an indented `backend-perf:` tree (the `sync_cache` seams) to **stderr**; stdout stays byte-identical. The hidden `tui --render-once` path flushes the `snapshot` tree the same way, for profiling a single build without running the server.
- **Loopback dashboard endpoint.** `GET /api/debug/backend` returns the **last completed** traced build's timing tree (present only if the dashboard was started with `CCTALLY_PERF_TRACE=1`; otherwise `null` with a `tracing_disabled` note) plus on-demand cache-state and dataset row counts. As of M5 the last-completed tree can be a **conversation** trace as well as the `/api/data` snapshot: the assembly-relevant conversation routes (list, search, outline, find, export, prompts, detail) stash their `endpoint.conversation_*` tree on exit (the long-lived `/events` SSE and the `/payload`/`/media` binary routes deliberately do **not**, so they can't clobber the last useful assembly trace). Distinguish which build you're looking at by the root phase name.

The endpoint ships `schemaVersion: 1` for basic tooling but is **documented unstable**: phase names, nesting, and fields may change without a version bump. It is a diagnostic, not a consumer API — treat it structurally, never byte-golden its phase names or timings.

### The always-on tick record (#583 S1)

The phase collector answers a deep question and answers it only when the process was started under `CCTALLY_PERF_TRACE`. That is the wrong shape for the first question an operator on a slow install asks — *how long is a tick, and where does the time go* — because restarting to get an answer discards both the accumulated memos and the condition being diagnosed.

So a second, always-on instrument sits beside it: `bin/_lib_tick_stats.py`, a stdlib-only leaf module holding an immutable, lock-guarded record of the last 64 ticks. Every update takes one module-level lock, constructs the complete replacement state under it, and rebinds a single global; readers take no lock. It deliberately does **not** copy `_LAST_BACKEND_PERF`'s bare rebind, which is safe only because that slot is written by whole replacement — a counter update is read-modify-write and would lose increments. The whole state is bounded at 64 records and 64 KiB, both asserted.

Each record carries the tick's total duration, its **mutually exclusive** ingest and builder halves, its dispatch path (`full` / `idle` / `degraded`), its Codex regime (`active` / `idle` / `not_observed`), whether it was cold, and the instant it published. The two halves are exclusive in *both* directions: an A2 progress build runs synchronously inside `sync_cache`, so its time is subtracted from the enclosing ingest. Without that, `ingest_ns + builder_ns` can exceed the whole tick and the orchestration remainder means nothing.

Both classifications are **aggregated over the outer refresh**, not taken from the last build in it. Several builds can run inside one refresh and disagree, and last-write classification would file an expensive tick as an idle one. The Codex regime reads the realised source-leg decision at the `codex is None` predicate, never `CodexIngestStats.rows_changed` — a quota-only batch leaves `rows_changed` at zero while advancing `codex_physical_mutation_seq`, which forces a genuinely expensive rebuild that `rows_changed` would stamp as idle.

The record also counts the silent Group A cache-open failures. `_group_a_daily_buckets`, `_group_a_weekly_buckets` and `_group_a_monthly_buckets` each fall back to a wide from-scratch fetch when they cannot open the cache, with byte-identical output — so a persistent failure is invisible everywhere else and explains a slow tick that looks healthy. The count is taken in a wrapper around `open_cache_db`, matching the caller by `__code__` identity and failing open, so the three helpers stay byte-unchanged and the original exception is never replaced.

### Arming the trace at runtime (#583 S1)

`POST /api/debug/backend/trace` with `{"enabled": true|false}` arms or disarms the deep phase trace on a **running** process. The request goes into an atomic mailbox and the flip is applied at the next authoritative build, after the ingest's cache connection closes — so a request landing mid-ingest cannot split one ingest across two tracing states. `requested`, `applied` and `applies_at` are reported separately, because otherwise `off` appears to succeed while tracing is still on.

`_ENABLED` is process-global and HTTP handlers open their own roots concurrently, so deferring the flip bounds only the rebuild thread. A root therefore captures the armed state at its own creation and every phase beneath it consults that captured value; a thread with no root scope open falls back to the global. Every root is wholly traced or wholly untraced whatever happens mid-flight.

Arming no longer suppresses A2 progressive fill. The partial build runs inside `_lib_perf.isolated_thread_state()`, which saves the exact `_tls.stack` and `_tls.root` object references and restores them in `finally` — by identity, because each open `Phase` holds that same list — so the partial's unconditional `reset_thread()` cannot reach the enclosing `sync_cache` trace.

`cctally dashboard-perf` reads all of this and drives the arm; see `docs/commands/dashboard-perf.md`.

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

## 7. Incremental Codex aggregation (#582)

Issue #566 removed repeated per-row lookups but left one full-population Codex
aggregation on every dirty tick. Migration 044 and the process-local caches
described above close that remaining path without changing the published data
version or the #313 cooldown formula.

The acceptance copy held 429,035 Claude entries, 154,600 Codex entries in
2,376 rollout files, and 267,503 quota observations. A frozen two-row dirty
tick measured 10.04 s before the change, including 7.88 s in
`build.source_bundle`. The final frozen dirty build measured 4.47 s. A
caught-up live Codex-active iteration that ingested two new JSONL rows completed
in 4.767 s: 2.204 s ingest and 2.151 s source construction. Under the unchanged
#313 formula (`work + max(5 s, work)`), that is a 9.767 s publish period. The
frozen output remained exactly 3,083,774 bytes with SHA-256
`564af30f87bd7afce8580ad038410dc862e162cf2e168a66cf15457d1ace1074`.

The regression gate is structural as well as temporal: tests require a warm
dirty build to request only the changed physical path, reuse precomputed costs,
and reproduce the cold builder's full `SourceDashboardState` exactly. A mutant
that forces the accounting cache cold must fail the bounded-path assertion.

## 8. The corpus envelope oracle (#583 S1)

The two envelope references recorded above and in `docs/dashboard-gotchas.md`
were taken against the maintainer's private store. They differ from each other
because the capture is per-store rather than because either is wrong, and
neither can be reproduced by anyone who does not have that store. The #566
figure is recorded in `docs/dashboard-gotchas.md` and the #582 figure in §7
above; this section deliberately does not restate the #566 one, because that
document is private and this one is published to the public mirror. #583 S1 adds a
third, separately named artifact rather than replacing them: a byte reference
over the GENERATED bench corpus, which anyone can rebuild from the generator and
its seed. **Hashes are corpus-specific.** A value from this section is never
comparable with a value from §7 or from `docs/dashboard-gotchas.md`, and this
oracle does not re-take, reconcile or supersede either of them.

Capture it with `bin/cctally-snapshot-measure --corpus small --envelope <path>`,
and **verify a tree against it with `bin/cctally-snapshot-measure --corpus small
--verify`**, which re-captures, compares `stable_sha256`, the byte count and the
corpus fingerprint, reports all three axes, and exits 3 on any mismatch. A
fingerprint mismatch is reported first and separately, because it means the two
captures describe different corpora and their hashes were never comparable — the
generator changed, not the envelope. Pytest deliberately does not own this
check: the corpus root is part of the capture contract, so a pytest temporary
directory reproduces a different hash by construction. The authoritative suite
owns the canonical-root verification through the private
`bin/cctally-envelope-oracle-test` harness.
The full record, including the corpus fingerprint the hashes are keyed to, lives
in `bench/baselines/envelope-oracle.json`. The current capture is 164,619 bytes
— read the sidecar for the exact figure — with rebuild-stable SHA-256
`68a35bfd3605ff49d340d7917cd2755341e62d9292eca7fb110334369fa6b525` over corpus
fingerprint `6d5d8358e1765415c40da93f3644b66e64fe4a81a253809248bde641b3a9082d`
at generator version 6. #565 moved that capture from 164,743 bytes and
`ca63029af9bae0ce50072c0d9bef8d899b01422633ca889d30b0784e90181ad2`:
the source schema moved from 10 to 11, and the generated corpus's decorated
Codex provider no longer contributes the obsolete `multi_account_unsupported`
cause. Claude's cycle is unresolved in this corpus, so the fail-closed result
now reports only `claude_cycle_unresolved`. A normalized structural diff found
exactly those changes, while the corpus fingerprint stayed fixed. #583 S3 had
moved the earlier capture from 300,411 bytes and
`e733e922d30942f9eb0a1b4cde798f69a4903879eb6686f276e0a33186369421`; the corpus
fingerprint did not move, which is what makes the two figures comparable, and
the structural difference between the two captures contains exactly three
entries — `source_schema_version` going 9 to 10 and the two provider members
under `sources.all.data.providers` becoming null. Almost the whole 135,668-byte
reduction is that one removal, which is why the reduced figure is not a
surprise.

Read the two figures in the previous paragraph carefully: the pre-change capture
was 300,411 bytes, and the value this section and the sidecar carried before the
#583 S3 re-take was 299,863. Those disagree by 548 bytes, so the oracle had
already drifted against `main` before S3 touched it. Nothing in the
authoritative suite runs `--verify` — the corpus root is part of the capture
contract, so a pytest temporary directory reproduces a different hash by
construction — and a session between #583 S1 and S3 therefore moved the envelope
without re-taking the oracle. The figures recorded here are measured against the
current tree. #583 S2 had moved the capture from 299,702 bytes and
`83da0f3d462b3cc92fca08c7fb8d07936c60e0a25cb00077c03b2dee96277ada` by the 161
bytes of the additive `sync_activity` object, and that entry is retained above
as history. Earlier captures in this session read 154,915, 232,328 and 306,026 bytes;
all are history, superseded as the corpus was rescaled, its Codex cycle made
resolvable, and the tool's environment handling settled. Every capture is now
refused unless the Codex leg renders fully, so a recorded envelope can never be
the degraded short branch.

Four properties of that capture were established by measurement. The first
three are why the record carries a rebuild-stable hash rather than a plain one;
the fourth is why the corpus root is part of the capture contract.

First, a plain SHA-256 over the whole envelope is deliberately NOT committed. It
is stable for one BUILT corpus and moves across a rebuild, and it also differs
between machines. Two captures against a corpus already on disk are
byte-identical; after deleting and rebuilding the same corpus at the same root
the only field that moves is `data_version`, at the top level and inside each
`sources[*]` entry — the snapshot signature, which folds in the wall-clock
metadata production ingest stamps.

Second, the envelope carries a machine-derived `doctor` object whose `counts`
summarise 62 host checks and whose `fingerprint` is a fixed-length SHA-1 digest.
Fixed length is exactly why a second machine reproduces the corpus fingerprint
and the byte count exactly while disagreeing on a hash over the bytes. The
Third, since #583 S2 the envelope carries a `sync_activity` object whose
`server_epoch` is minted fresh per server process. Unlike the first two, this one
moves on every run rather than across a rebuild or between machines, so leaving
it in would break the oracle continuously instead of moving it once. The token is
a fixed 16 characters, so the byte count stays a meaningful comparison and only
the digest excludes it. The other six `sync_activity` fields are real published
content and are NOT excluded.

The stable digest therefore excludes exactly three things: the top-level
`data_version` and `doctor`, and the nested `sync_activity.server_epoch`. All
three exclusion sets are pinned against literals by test, and a unit test proves
the digest ignores exactly those while still discriminating other content —
including each of the six remaining `sync_activity` fields, so that a trim which
quietly dropped the whole subtree would fail rather than pass.

Fourth, the timezone is load-bearing and is now pinned by the tool. `display.tz`
defaults to `local` and every date-bucketed panel buckets in it; the same
command under Asia/Jerusalem once produced a different byte count entirely.
`bin/cctally-snapshot-measure` sets `TZ=Etc/UTC` itself, so a capture no longer
depends on where it ran.

Fourth, the corpus root is load-bearing. Codex identity is derived from the
absolute canonical provider-root path: `source_root_key` hashes it, and every
Codex session key, project key, quota key and history key in the envelope
derives from that in turn. A corpus built under a different root therefore
publishes an envelope of identical LENGTH whose every Codex opaque key differs.
`cctally-snapshot-measure` defaults `--corpus-root` to the literal
`/tmp/cctally-oracle-<scale>-seed<seed>` rather than to
`tempfile.gettempdir()`, which resolves per machine. On macOS `/tmp` resolves to
`/private/tmp`, so a capture is comparable within an OS family and not across
one.

## 9. What every tick ships (#583 S3)

Three separate quantities changed, and they are measured apart because they are
not the same thing.

**The projection.** `_serve_api_events` used to call `snapshot_to_envelope` and
then `encode_dashboard_json` inside its per-connection loop, so N connected
clients projected and encoded the same data N times per tick. `SSEHub.publish`
now wraps each publication in one immutable `_SSEDelivery` that pins `now_utc`
and `monotonic_now` once and caches the complete byte-ready SSE frame per
variant — prefix, encoded JSON and terminating blank line. The variant key is
`(transcripts_visible, canonical oauth cfg, display_tz_pref_override,
runtime_bind)`; only the first two can differ between two connections in one
process today, and the two process constants stay in the key so a later change
making either per-connection cannot silently serve one client another client's
payload. The cache lock is per delivery with a double-checked lookup, because
the hub's own lock must never be held across a multi-megabyte projection and
because two clients racing on the same missing variant would otherwise both
project or UTF-8 encode, which is the entire cost being removed.

A delivery is shared only when its snapshot carries `envelope_precompute`.
Without it `snapshot_to_envelope` reads configuration inline and runs the real
doctor gather per call, so the result is not a function of the key. The
per-connection seed from `SSEHub.subscribe` is deliberately a fresh delivery
with the clock sampled at subscription, so a client connecting between ticks
does not render ages frozen at the previous publication.

**The structure.** `sources.all.data.providers` stopped carrying the two
provider data objects and publishes null for both. On the maintainer's
production-scale store that mirror was 1,562,096 bytes of a 3,248,564-byte
recompacted envelope. It is the half compression cannot touch: it is what the
browser decodes and allocates, not merely what it downloads.

**The wire.** Each SSE connection gets one `zlib.compressobj(level=6, wbits=16 +
MAX_WBITS)`, and `/api/data` compresses the same way, both negotiated by a
parsed `Accept-Encoding` rather than a substring test.

Measured over the pinned small corpus, the three compose rather than overlap:
the legacy v9-shaped frame is 305,466 bytes, the v10 frame is 166,877, and
gzipping the v10 frame gives 26,155 — 11.68 times smaller than the legacy frame,
of which gzip alone accounts for 6.38. They multiply because the two copies of
the provider data sat about 1.5 MB apart in the serialized envelope and zlib's
window at `MAX_WBITS` is 32 KiB, so the compressor could never have encoded the
second as a back-reference to the first.

Compression costs CPU on the publish path, and `bin/cctally-snapshot-measure
--compression` is the receipt. Over the same corpus, one client costs a median
1.76 ms of CPU per frame (compressor initialisation 0.001 ms, `compress()`
1.72 ms, `flush(Z_SYNC_FLUSH)` 0.047 ms); two clients cost 3.51 ms and four cost
6.92 ms, so the per-client cost is linear. That linearity is exactly what a
future multi-member design would have to beat, and it is why the deferral is
recorded rather than assumed: compressing once per tick and sharing the bytes
requires each frame to be an independent gzip member, cross-browser support for
incrementally decoding a concatenated multi-member stream under
`Content-Encoding` is not established, and the saving is proportional to
connected clients minus one — exactly zero at one open tab.

The frame encoding is now separate from that per-connection compression cost.
On the pinned large corpus (1,689,163 JSON bytes), directly replaying the old
frame-assembly path cost a median 0.519 ms for one client, 1.081 ms for two and
2.088 ms for four. Building the shared byte frame once cost 0.041–0.048 ms
independent of client count. A same-corpus before/after compression rerun stayed
within measurement noise (12.90 → 12.64 ms at one client, 25.70 → 24.41 ms at
two and 49.62 → 51.34 ms at four), as expected: this change shares UTF-8 frame
bytes, not compressor state.

**Memory is the other side of the same trade, and it is a real one.** A delivery
retains one complete frame byte string per variant for the whole publish period,
where the previous code retained the JSON string but built and encoded the frame
again for every connection write. On the maintainer's production-scale store
the version 10 envelope serializes to about 1.9 MB, so the retained object stays
roughly that size for the one variant a normal install produces; the frame
prefix and terminator add only 22 bytes. The retention is bounded three ways:
the key differs between two connections only by
`transcripts_visible` and the resolved `oauth_usage` block, so a single-user
dashboard has one variant; each delivery is dropped when the next publication
replaces `_last`; and a snapshot without `envelope_precompute` is never shared
and therefore never cached at all. The current exchange is one held byte frame
for N avoided per-connection frame assemblies and UTF-8 encodes.

**The hidden-tab suspend no longer necessarily reconnects the server stream.**
Tabs in one browser/origin share a `SharedWorker` that retains the newest parsed
snapshot and its delivery generation. Suspending one tab only stops deliveries
to that port while another tab remains active; returning receives the retained
seed without a new `SSEHub.subscribe` projection. A server reconnect and its
dedicated projection happen only when all worker ports were suspended (so the
worker closed its `EventSource`) or when the browser is using the direct
fallback. The older worst-case calculation still applies to that all-suspended
or fallback case: at most two dedicated projections per minute for repeated
thirty-second hide/return cycles, versus roughly nine frames per minute avoided
at the measured 6.5-second publish period. The ordinary multi-tab case is
cheaper: one server stream, one JSON parse per frame, and structured clones to
the active tabs.
