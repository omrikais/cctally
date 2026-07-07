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
- **Loopback dashboard endpoint.** `GET /api/debug/backend` returns the **last completed** snapshot build's timing tree (present only if the dashboard was started with `CCTALLY_PERF_TRACE=1`; otherwise `null` with a `tracing_disabled` note) plus on-demand cache-state and dataset row counts.

The endpoint ships `schemaVersion: 1` for basic tooling but is **documented unstable**: phase names, nesting, and fields may change without a version bump. It is a diagnostic, not a consumer API — treat it structurally, never byte-golden its phase names or timings.

### Where the numbers live

This doc is the qualitative contract. Concrete budgets — target warm-rebuild time, ingest throughput, idle CPU — are M3's committed benchmark baselines, measured with the same phase vocabulary above. When you need live numbers for the machine in front of you, read them from `CCTALLY_PERF_TRACE` or `/api/debug/backend`; when you need the regression thresholds, read the M3 baselines.
