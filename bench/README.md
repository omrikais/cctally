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
