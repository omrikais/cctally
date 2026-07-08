# Benchmarks

This directory holds reproducible benchmarks cited from the project's public README.

## `cctally-vs-ccusage.sh`

First-table latency for `cctally daily` vs. `ccusage daily` on the user's existing `~/.claude/projects/` session data.

### What it measures

- **`cctally daily` cold cache** — deletes `~/.local/share/cctally/cache.db` before each run, so the time includes building the JSONL → cache delta from scratch.
- **`cctally daily` warm cache** — leaves `cache.db` intact; the time reflects the steady-state path most users will see day-to-day.
- **`ccusage daily`** — the upstream tool, for comparison.

The script wraps `hyperfine` when present (5 runs after 2 warmup runs; median + stddev reported). If `hyperfine` is absent, it falls back to `time` over 5 runs and prints the median.

### Caveats

- **Hardware-dependent.** First-table latency varies by disk speed, CPU, and Python startup cost.
- **Data-volume dependent.** A user with 6 months of dense session JSONL will see different cold-cache numbers than someone with two weeks. The `--days N` flag bounds the query window; it does NOT bound the cache rebuild scope (cache.db ingests every JSONL byte regardless of `--days`).
- **Cold vs. warm matters.** The README's cited number is the **warm** cctally vs. ccusage delta — that's the steady state. The cold cctally number is reported separately so readers can see the one-time setup cost.
- **`ccusage` install.** If `ccusage` isn't on `PATH`, the script skips that row with a clear message and still reports the cctally numbers.

### How to run

```bash
bench/cctally-vs-ccusage.sh           # default --days 30
bench/cctally-vs-ccusage.sh --days 7
```

### Optional: install hyperfine

```bash
brew install hyperfine        # macOS
cargo install hyperfine       # any platform
```

### Reproducing the README's number

The README's "first-table latency" line cites a specific median measured on a specific date and hardware. To reproduce:

1. Have at least 30 days of session JSONL under `~/.claude/projects/`.
2. Ensure both `cctally` (this repo) and `ccusage` (`npm install -g ccusage`) are on `PATH`.
3. Run `bench/cctally-vs-ccusage.sh --days 30`.
4. Compare the warm-cctally and ccusage medians.

Numbers will vary by hardware. The README's cited number was measured on macOS arm64 (M-series Apple silicon) and may not match your environment.

## Backend benchmarks (`bin/cctally-bench`)

`bin/cctally-bench` is a standalone, in-process backend benchmark runner (a dev/maintainer tool like `bin/cctally-release`, **not** a `cctally` subcommand, and never shipped to npm). It protects the #268–#275 backend performance wins from silent regression by timing the backend hot paths directly (importing the modules and calling the functions), excluding the ~50–100 ms Python-startup noise that would swamp sub-millisecond internal work. It complements `cctally-vs-ccusage.sh` above, which stays as the end-to-end first-table-latency benchmark. Companion generator: `bin/build-bench-fixtures.py` (issue #276, M3).

### What it measures

Six benchmark families (14 benchmarks) exercise the paths the recent perf work optimized: the dashboard **snapshot** spine (`_tui_build_snapshot` with `precompute_envelope=True`) in three modes — **cold** (fresh accelerator state), **warm** (the dispatch signature moved, forcing a full rebuild with warm sub-caches), and **idle** (signature unchanged, so the reuse short-circuit engages and reads near-zero); cache **ingest** (`sync_cache` no-op over many files + a one-file delta); the **conversations** rail (`list_conversations` page-1, cost-sorted, and filtered); cross-session **search** + in-conversation **find**; **payload/outline** assembly (`_assemble_session` + `get_conversation_outline`, measurement-only); and the two warm **reconcile** helpers (projects-envelope + cache-report). Each benchmark runs `--iterations N` times, discards the first as warmup, and reports the median plus min/max — the in-process analogue of the `hyperfine` methodology above.

### Fixture and scale

The runner builds a deterministic seeded synthetic fixture — real `*.jsonl` written under a scratch Claude root, then ingested through the production `sync_cache` path so `cache.db` has genuine shape — and never reads or writes the real `~/.local/share/cctally` or `~/.claude/projects` (it pins both `CCTALLY_DATA_DIR` and `CLAUDE_CONFIG_DIR` before importing the backend). `--scale small` is a tiny corpus for the self-test and fast local iteration; `--scale large` (the default, and the committed-baseline scale) is the ~300K-entry-class corpus. A `large` build is slow (~a minute), so the fixture is cached under the scratch dir keyed by `(seed, scale, pricing-date)` and rebuilt only on a key miss — repeated runs on the same machine reuse it instantly. Determinism is **semantic**, not byte-level: `sync_cache` stamps a few wall-clock metadata columns during ingest, so `cache.db` is not byte-identical across builds; reproducibility is defined over a content hash of the semantic columns (see `build-bench-fixtures.py::semantic_hash`).

### Running

`bin/cctally-bench` prints an aligned human table by default and `--json` for the machine form (`schemaVersion`, `cctally_version`, `machine_label`, `scale`, `seed`, `dataset_counts`, and per-benchmark `{median_ms, min_ms, max_ms, count?, bytes?}`). `--trace` additionally flips Session A's M2 phase collector on per benchmark and attaches its phase sub-tree, so the bench and the dashboard's `/api/debug/backend` endpoint speak one phase vocabulary. The `--json` output is a diagnostic (documented unstable, like `/api/debug/backend`) and is never byte-goldened.

```bash
bin/cctally-bench --scale small --iterations 2     # fast local loop
bin/cctally-bench --scale large                    # the committed-baseline scale
bin/cctally-bench --scale large --trace --json     # with M2 phase sub-trees
```

### Realism mode

For an ad-hoc sanity check against real-shaped data, `--data-dir <copied CCTALLY_DATA_DIR>` + `--claude-dir <copied Claude root>` point the run at two operator-supplied **copies** (both axes are required — the cache/stats dir and the JSONL source are independent). The bench never copies prod itself; realism-mode numbers are compared only against a locally saved baseline, never the committed one.

### Baseline, compare, and gate

`bench/baselines/backend.json` is the committed advisory baseline (version, machine label, dataset counts, per-benchmark medians) measured from a real `--scale large` run. `--compare` diffs the current run against it, printing a per-benchmark Δ column and a verdict — `OK`, `REGRESSED` (over tolerance), `MISSING` (in the baseline but dropped from the current run), or `NEW` (added since the baseline) — and **exits 0** (advisory by default). `--gate` is the same but **exits non-zero** on any `REGRESSED`/`MISSING` or a malformed baseline, for a human enforcing locally or in a PR. A machine-label mismatch prints a loud banner and stays advisory (the compare is shown, but `--gate` does not fail on cross-machine numbers alone). `--update-baseline` reruns and overwrites the baseline, stamping the current version + machine label. The compare is **never** wired into `cctally-test-all`/CI: #271's ~100–130 ms machine-to-machine variance exceeds a real 40–60 ms regression, so a hard threshold would flap. `bin/cctally-bench-test` (auto-discovered by `cctally-test-all`) asserts only the structure — the `--json` schema, all 14 benchmark names, and isolation — and **never** asserts a wall-clock timing.

### Tunable constants

The regression tolerance is `max(BENCH_TOLERANCE_PCT × baseline, BENCH_TOLERANCE_FLOOR_MS)` — the proportional part (`0.15`) catches regressions on the big benches, and the absolute floor (`15.0` ms) stops sub-millisecond benches from flapping on same-machine noise and cleanly handles a zero/near-zero baseline (idle). `DEFAULT_ITERATIONS` (`5`), `DEFAULT_SEED` (`42`), and `DEFAULT_SCALE` (`large`) round out the knobs. All five are named constants at the top of `bin/cctally-bench` and are meant to be tuned after the first real run on a new reference machine: if the same-machine repeat-run spread on the big benches exceeds the floor, raise `BENCH_TOLERANCE_FLOOR_MS` and note it here.
