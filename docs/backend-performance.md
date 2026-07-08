# Backend performance: the read model, hot paths, and invariants

This is the qualitative contract for cctally's backend read model — what owns which data, where request time goes, and which invariants must never break. It is deliberately a public architectural doc (a contract, not secrets), consolidating knowledge that previously lived only in scattered gotchas docs and session memory. It cross-links to [architecture.md](architecture.md) and the private `docs/*-gotchas.md` files rather than duplicating them.

Quantitative budgets (how many milliseconds a warm rebuild "should" take) are **not** here — those are M3's committed benchmark baselines. This doc is the shape; the numbers live with the benchmarks. For live introspection, use the opt-in instrumentation described in [Introspection](#introspection-cctally_perf_trace--apidebugbackend).

## 1. Read-model ownership

cctally has three tiers of state. Knowing which tier owns a fact tells you whether it is authoritative, re-derivable, or a per-process accelerator — and therefore what you may safely rebuild.

| Tier | Store | Owns | Re-derivable? |
| --- | --- | --- | --- |
| Authoritative | `stats.db` | User/runtime facts: weekly usage snapshots, percent milestones, week-reset events, weekly credit floors, budget milestones. | **No** — the source of truth. Losing it loses recorded history. |
| Derived read model | `cache.db` | Everything computed from the on-disk JSONL: cost entries (`session_entries`), transcript rows (`conversation_messages`), the FTS search indices, the `conversation_sessions` browse rollup, file-touch axes, AI titles, the Codex parallel tables, and the `mutation_seq` change-signal counters. | **Yes** — fully re-derivable from `~/.claude/projects` JSONL. `rm cache.db`, `cache-sync --rebuild`, or a reader-side fallback all recover it. |
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

Ingest is the other hot path, shared by every JSONL-reading command through the read-through delta cache. Its coarse seams, under the `sync_cache` root: `flock` (acquire the exclusive `cache.db.lock`), `backfills` (the rare, upgrade-only reingest/backfill flags), `discover` (glob + stat, `count` = files), `walk` (the fused per-file parse-and-write loop as **one** phase, `count` = files processed — parse, session-entry writes, conversation-message inserts, file-touch maintenance, and AI-title upserts are fused into one per-file transaction, and FTS is trigger-driven behind the message inserts), and `recompute.conversation_sessions` (the post-walk browse-rollup re-derive). The `walk` loop is never instrumented per-row; volume is a `count`.

### Cache-state diagnostics

The signature legs, per-cache-table row counts, and pending reingest flags are all queryable from `cache.db` on demand — they are **not** timed phases. `/api/debug/backend` computes them at request time so they are available even when tracing is off.

## 3. Invariants that cannot be broken

These hold regardless of performance work; a change that violates one is a bug even if it is faster.

- **Privacy gate.** The `/api/debug/backend` diagnostic is loopback-only, always — its primary check is the unspoofable loopback TCP peer, with an IP-literal loopback `Host` as anti-DNS-rebinding defense-in-depth. It never consults `dashboard.expose_transcripts`. (The transcript endpoints have their own, deliberately more permissive, gate.)
- **No transcript text in diagnostics.** The diagnostic surfaces leak only timings, counts, flag names, signature legs, and already-safe cache-table names — never prompt/prose/paths.
- **Read-only, no-side-effect doctor.** `doctor` gathers and reports; it never heals, migrates, or writes.
- **`cache.db` is re-derivable.** Any code may rebuild it from JSONL. It carries no authoritative state, so `--rebuild` and the reader-side fallback are always safe.
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

The conversation reader assembles a whole session from `conversation_messages` on **every** call — `_assemble_session` runs the full dedup → turn-grouping → fold → sweep → meta-classify → cost/usage-stamp pipeline over the entire session, and `get_conversation` (each page), `get_conversation_outline`, `get_conversation_export`, `get_conversation_prompts`, and `find_in_conversation` (after a non-empty match probe) all funnel through it. Nothing is materialized or cached across calls. M5's mandate was measurement-first: instrument that path (deep `assemble.*` seams, §4), sweep its cost across a synthetic size ladder, find the threshold where whole-session assembly becomes human-perceptible, and only then decide whether to materialize rendered turns in `cache.db`.

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
