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

Six benchmark families (16 benchmarks) exercise the paths the recent perf work optimized: the dashboard **snapshot** spine (`_tui_build_snapshot` with `precompute_envelope=True`) in three modes — **cold** (fresh accelerator state), **warm** (the dispatch signature moved, forcing a full rebuild with warm sub-caches), and **idle** (signature unchanged, so the reuse short-circuit engages and reads near-zero); cache **ingest**, including the exact two-provider `frontier.caught_up` validation over a previously full-seeded store plus `sync_cache` no-op and one-file delta; the **conversations** rail (`list_conversations` page-1, cost-sorted, and filtered); cross-session **search** + in-conversation **find**; **payload/outline** assembly (`_assemble_session` + `get_conversation_outline`, measurement-only); and the two warm **reconcile** helpers (projects-envelope + cache-report). The frontier row reports the total tracked provider-file count as its `count`, making tiny/small/large receipts directly comparable for the intended O(roots), not O(rows), scaling property. Each benchmark runs `--iterations N` times, discards the first as warmup, and reports the median plus min/max — the in-process analogue of the `hyperfine` methodology above. Note that `search.cross_session` queries a term (`"benchmark"`) that matches **every** synthetic message by design, so its timing (and its large `count`) is a deterministic worst-case guard value — a stable ceiling to catch a regression in the search path — not a realistic user-search latency.

### Fixture and scale

The runner builds a deterministic seeded synthetic fixture — real `*.jsonl` written under a scratch Claude root and real Codex rollout `*.jsonl` written under two scratch `$CODEX_HOME` provider roots, then ingested through the production `sync_cache` and `sync_codex_cache` paths so `cache.db` has genuine shape and through `sync_claude_conversations` and `sync_codex_conversations` so `conversations.db` contains real Claude sidechains and Codex messages/events — and never reads or writes the real `~/.local/share/cctally`, `~/.claude/projects` or `~/.codex` (it pins `CCTALLY_DATA_DIR`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `HOME` before importing the backend). Since #583 S1 there are THREE uniform profiles — `tiny`, `small` and `large` — and every one carries the same discriminators, differing only in cardinality: two Codex accounts with unequal spend and unequal quota, two canonical project roots sharing a basename, both model-pool axes `bin/_lib_codex_pools.py` recognises, and one same-identity Spark-versus-standard weekly collision. Without them no benchmark could reach `build_source_bundle`, which #566 measured at 79% of a profiled build. `tiny` and `small` differ by more than 10x on both provider axes and both build in seconds, which is what lets a row-count-invariance gate use them as a pair; `large` is the maintainer receipt and takes minutes. The corpus is anchored at a fixed reference epoch and every consumer measures it at `CORPUS_CLOCK_UTC`, the instant its quota geometry is built around — one live weekly cycle, everything else in the past. `--scale tiny` is the cheap half of the >=10x pair; `--scale small` is the other half and the self-test / fast-local-iteration profile; `--scale large` (the default, and the committed-baseline scale) is the ~300K-entry-class corpus. A `large` build is slow (~a minute), so the fixture is cached under the scratch dir keyed by `(seed, scale, pricing-date)` and rebuilt only on a key miss — repeated runs on the same machine reuse it instantly. Determinism is **semantic**, not byte-level: `sync_cache` stamps a few wall-clock metadata columns during ingest, so `cache.db` is not byte-identical across builds; reproducibility is defined over a content hash of the semantic columns (see `build-bench-fixtures.py::semantic_hash`).

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

`bench/baselines/backend.json` is the committed baseline measured from a real `--scale large` run, and since #583 S1 it carries two top-level blocks. `contract` is structural and IS asserted: the exact benchmark-name set, the scale and seed, the corpus fingerprint, the generator version, and the dataset counts across both providers. `receipt` is advisory and is never asserted: the cctally version, the machine label, and the per-benchmark medians. `classify()` reads its numbers from `receipt` and still accepts the pre-S1 flat shape, so a baseline from an older checkout compares rather than reading as malformed. `--compare` diffs the current run against it, printing a per-benchmark Δ column and a verdict — `OK`, `REGRESSED` (over tolerance), `MISSING` (in the baseline but dropped from the current run), or `NEW` (added since the baseline) — and **exits 0** (advisory by default). `--gate` is the same but **exits non-zero** on any `REGRESSED`/`MISSING` or a malformed baseline, for a human enforcing locally or in a PR. A machine-label mismatch prints a loud banner and stays advisory (the compare is shown, but `--gate` does not fail on cross-machine numbers alone). `--update-baseline` reruns and overwrites the baseline, stamping the current version + machine label. The compare is **never** wired into `cctally-test-all`/CI: #271's ~100–130 ms machine-to-machine variance exceeds a real 40–60 ms regression, so a hard threshold would flap. `bin/cctally-bench-test` (auto-discovered by `cctally-test-all`) asserts only the structure — the `--json` schema, all 16 benchmark names, the committed baseline's `contract` block, and isolation — and **never** asserts a wall-clock timing. The baseline check reads the file directly and does **not** run the large benchmark.

### Diagnosis latency (`explain-benchmark.py`)

`python3 bench/explain-benchmark.py` is the fail-closed end-to-end receipt for `cctally explain` and the dashboard's `/api/diagnosis` route. Its default three-day window gives both the current and immediately preceding windows real support. The large corpus includes Claude sidechains, a 2,500-row injected meta/tool history, Codex normalized events and child threads, two unequal real Codex accounts, unattributed Claude rows and typed withheld cases. Three independent rounds each discard warmups and report Claude, Codex and All round medians plus pooled p95. A fresh dashboard process supplies the separate cold-route sample and canonical CLI/dashboard comparison; an extra All run samples aggregate RSS across the parent and isolated provider worker. The receipt also compares one dashboard request with four simultaneous identical requests, requiring one response body, canonical parity, at most one provider-worker protocol tree (three descendants including tracker/forkserver), and aggregate RSS within 256 MiB of the one-request dashboard baseline and below 2 GiB. `--bounds` builds over-cap adversarial stores and fails on the fixed query and per-subject row ceilings.

Run it only through the remote wrapper:

```bash
bin/cctally-test-remote python3 bench/explain-benchmark.py --json
bin/cctally-test-remote python3 bench/explain-benchmark.py --bounds --json
```

### Assembly scan (`--assembly-scan`)

A separate mode (**not** more default benchmarks): `bin/cctally-bench --assembly-scan` sweeps the cost of assembling a *whole* conversation across a size ladder — one synthetic session per turn-count rung — and prints a per-rung table (or `--json`). It measures the conversation reader's core hot path: `_assemble_session` runs over the entire session on every reader page, every outline, and every non-empty find, with nothing materialized between calls. Per rung it records deterministic counts (`turn_count`, `msg_count`, `item_count`) + payload bytes (`assembled_items_bytes` — the whole assembled list serialized, a **materialization-footprint proxy**, not an HTTP payload; the real endpoint `page_bytes@{200,500,1000}`; `outline_bytes`) and advisory median-of-N timings (`assemble_ms`, `detail_tail_ms`/`detail_page_ms`, `outline_ms`, `find_hit_ms`, and the derived `open_pair_ms` = detail + outline — the repeated-reassembly paths that would justify materializing turns).

The scan uses its **own** isolated `assembly` fixture (a size ladder, one session per rung), never the default `small`/`large` corpus, so it can never perturb `bench/baselines/backend.json`. `--assembly-ladder-scale {small,large}` picks the ladder: `small` = the fast self-test rungs (`ASSEMBLY_TURN_LADDER_SMALL`), `large` = the full evidence ladder (`ASSEMBLY_TURN_LADDER`, `[250, 500, 1000, 2000, 4000, 8000]` turns). The fixture marker carries a `params_hash` over the exact ladder + generator shape, so editing the ladder busts only the `assembly` scratch cache. The mode reads/writes its own `bench/baselines/assembly.json` and is **incompatible** with the default-suite `--compare`/`--gate`/`--update-baseline` flags (it errors if combined). Structural columns are deterministic → goldenable; the `*_ms` timings are machine-variant → recorded but never asserted (`bin/cctally-bench-test` checks structure/determinism/isolation only).

```bash
bin/cctally-bench --assembly-scan --assembly-ladder-scale small --json   # fast self-test rungs
bin/cctally-bench --assembly-scan --assembly-ladder-scale large --json    # the committed evidence run
```

The committed `bench/baselines/assembly.json` is the maintainer-machine evidence run behind the materialization decision recorded in [docs/backend-performance.md §5](../docs/backend-performance.md) (the threshold where whole-session assembly crosses `ASSEMBLY_VISIBLE_MS` = 100 ms, and the go/no-go on building a `conversation_turns` table). Re-run the scan and compare against that baseline when the real-session size distribution shifts. An operator `--data-dir`/`--claude-dir` pair runs an advisory realism cross-check (largest real session per size bucket), non-committed.

### Dashboard memory and background-work soak (`dashboard-soak.py`)

The soak runs the real dashboard, samples process RSS/CPU/I/O, retained owners,
full-tick durations and publication periods, and exercises HTTP/SSE, diagnosis
recovery, source add/remove, account rotation and cache rebuild. A synthetic
`--scale large` run remains a useful diagnostic, but it is **not** a #716/#679
certification: the generator disables retention and is not a multi-GB
production copy. `--gate` now refuses to certify that fixture.

For the certification soak, supply `--fixture-copy` as an operator-prepared
**copy** with `data/` (including `conversations.db` and `config.json`),
`claude/projects/`, and at least one `codex-*/sessions/` root. The script
rejects symlinks, clones the supplied copy into a new `--root` for each arm,
and mutates only those clones. Never point `--root` or `--fixture-copy` at live
production stores. The source copy must have a conversation DB of at least
2 GiB and retention configured above zero. The producer makes retention due
only on its isolated clone and must observe a successful `delete` phase before
staging scenarios. A synthetic `large` fixture
does not satisfy this requirement regardless of its entry count.
Production-copy runs explicitly remove `CCTALLY_AS_OF` from the dashboard
environment so current retention, window selection and freshness are measured;
only deterministic synthetic runs keep the January 2026 fixture pin.

#### Copied fixtures: corpus precondition

A copied fixture's normalization (`cache-sync --rebuild`) keeps a `conversation_source_files` row whose path is not genuinely absent on the measurement host, which is the product's own retention rule: a path whose `lstat` succeeds survives, and so does one whose `lstat` fails with anything other than `ENOENT` or `ENOTDIR`. Such a row describes a corpus outside the clone, so the copy would refuse in its cursor evidence only after the full rebuild. Before copying, the harness therefore reads that one table from the fixture's `data/conversations.db` (read-only) and `lstat`s every path. One surviving or unreadable row refuses the copy with exit 2, naming the count, the first path, and whether the rows lie under the marker's `root` or its `retainedPriorSourceRoot`. Accounting cursors and Codex conversation cursors are not consulted, because normalization clears them whether their files exist or not. A present `.cctally-soak-normalized.json` marker must be a regular file with one of the two shapes the harness writes: `schemaVersion` 1, an absolute `root` and a 64-hex `fixtureSourceFingerprint`, optionally with both `retainedPriorSourceRoot` and `retainedPriorSourceSha256`. Anything else refuses as malformed.

The remedy is to run on a host where those paths are absent, or to move that corpus away first. Never move or delete another task's retained evidence without its owner's approval of the exact paths. For the §6.4 write-budget leg, use preparation v2, which mounts the fixture at its own root instead of copying it.

#### Write-budget leg, preparation v2

The §6.4 write-budget leg (and its `56e66f07a` baseline reference) no longer measures a copy of the fixture. A copy re-derives under a new path and a new device, so its first sync replays the corpus and the leg measures that replay. Preparation v2 instead keeps the fixture inside an APFS sparse-bundle disk image, attached without sudo at the fixture marker's own `root` (the canonical root, `/private/tmp/cctally-dashboard-evidence-f7709be6a636` by default), so every stored path names a real file at its stored spelling. Everything runs as a direct admin-ssh job on the runner, not through `bin/cctally-test-remote`, under one working root `W` on an external volume. Data lives under the canonical root inside the image; logs, receipts and evidence live under `W/evidence/<step>/`, and temp files under `W/tmp/<step>/`, so they outlive every detach. `--fixture-copy` is refused by every v2 step.

Since `#1080` the Codex quota index survives an attach. A cloned or re-attached image keeps its volume UUID, inode numbers and birth times when its device number changes, so a tree from `#1080` on keeps using the index the fixture was sealed with. An earlier tree that has the index rebuilds it whenever an attach gives the volume a different device number. The P1 receipt's `quotaIndex` records whether the index was rebuilt and by what (`rebuiltBy`). A write-budget comparison between trees on either side of `#1080` can therefore differ by that one rebuild.

P0 runs once. It reads the fixture as a pax tar stream on stdin, extracts it into a new image, checks the census and the two-sided content digest against the source host's read-only pre-pass, normalizes it with its producer `4892abbec` (retention deferred, then the pinned prune on the fixture's recorded clock), compares the normalized stores with the retained originals, and seals the detached bundle:

```bash
ssh <source-host> "tar --no-mac-metadata --format pax -C <fixture> -cf - ." | \
  ssh <runner-admin-host> "cd '<W>/repo' && python3 -I bench/dashboard-soak.py \
    --prepare-fixture-image --root '<W>' --output '<W>/p0-receipt.json' \
    --source-fingerprint <pre-pass fingerprint> --source-digest <pre-pass digest>"
```

Create the stream with `--no-mac-metadata` when the source host runs macOS: bsdtar's default `--mac-metadata` adds AppleDouble `._*` entries, which break the census.

The local machine only pipes the stream. The source host computes both values in a read-only pre-pass in the same session, from a checkout of this tree: the fingerprint with `python3 -I bench/soak_image.py fingerprint <fixture>` (exactly the harness's `_fixture_source_fingerprint`) and the digest with `python3 -I bench/soak_image.py digest <fixture>`. P0 refuses unless both equal what it measures on the extracted image.

The stream can be slow when it passes through another machine. To compress it in transit, pipe it through `zstd` on both sides; the job still reads a plain pax stream on stdin:

```bash
ssh <source-host> "tar --no-mac-metadata --format pax -C <fixture> -cf - . | zstd -3 -T0 -q -c" | \
  ssh <runner-admin-host> "zstd -dc -q | (cd '<W>/repo' && python3 -I bench/dashboard-soak.py \
    --prepare-fixture-image --extract-only --root '<W>' --output '<W>/p0-checkpoint-extracted.json' \
    --source-fingerprint <pre-pass fingerprint> --source-digest <pre-pass digest>)"
```

P0 seals two checkpoints, so a later failure never repeats the stream or the rebuild. Each one detaches the working bundle, clones it with one `clonefile(2)` to `<W>/p0-<stage>-<UTC>.sparsebundle`, seals the clone, writes `<W>/p0-checkpoint-<stage>-<UTC>.json` and reattaches the working bundle. The `extracted` checkpoint follows a verified extraction; the `rebuilt` checkpoint follows the producer's successful `cache-sync --rebuild --source all`, before the pinned prune. The final receipt lists both under `checkpoints`.

`--extract-only` stops after the `extracted` checkpoint, and `--output` names its JSON (the job's own receipt goes to `<W>/p0-extract-<UTC>-receipt.json`). `--resume-from <checkpoint.json>` replaces stdin and takes the pre-pass values from the checkpoint, so it refuses `--source-fingerprint` and `--source-digest`. It never attaches the checkpoint bundle: it refuses a bundle that no longer matches its seal, attaches a fresh clone, requires the checkpoint's exact inventory, and re-runs the fingerprint and the content digest before it continues. From `extracted` it runs the rebuild and everything after it; from `rebuilt` it runs the pinned prune, the comparison and the seal:

```bash
ssh <runner-admin-host> "cd '<W>/repo' && python3 -I bench/dashboard-soak.py \
  --prepare-fixture-image --root '<W>' --resume-from '<W>/p0-checkpoint-extracted.json' \
  --output '<W>/p0-receipt.json'"
```

P1 runs once per tree. It clones P0's bundle with one `clonefile(2)`, lets the tree's own migrating openers, stats rebuild, sync and catchup drain settle it, refuses any replay that no migration's pending key accounts for, and seals the result on the device it presents:

```bash
python3 -I bench/dashboard-soak.py --prepare-tree-image --root '<W>' \
  --p0-receipt '<W>/p0-receipt.json' --tree <sha> --output '<W>/p1-<sha>-receipt.json'
```

The leg and the reference then take the tree's P1 image. Each arm clones it, attaches it at the canonical root, requires the sealed `(st_dev, st_ino, size, mtime_ns)` identity before any product process starts, and admits through catchup's image mode (`WRITE_ATTRIBUTION_INPUTS=image:<manifest>`), whose manifest is bound to that identity receipt:

```bash
python3 -I bench/dashboard-soak.py --write-budget-leg --capture-baseline-reference --root '<W>' \
  --fixture-image '<W>/p1-<baseline-sha>-<UTC>.sparsebundle' --image-receipt '<W>/p1-<baseline-sha>-receipt.json' \
  --output '<W>/reference.json'
python3 -I bench/dashboard-soak.py --write-budget-leg --root '<W>' \
  --fixture-image '<W>/p1-<candidate-sha>-<UTC>.sparsebundle' --image-receipt '<W>/p1-<candidate-sha>-receipt.json' \
  --baseline-reference '<W>/reference.json' --gate
```

A reference and a candidate qualify against each other on the shared keys only: the P0 seal and receipt digest, the preparation contract (version 2), host, runtime, instrumentation, storage plus the image descriptor, and the workload. Each run's P1 attests on its own: its tree must be the run's tree, its P0 seal the shared one, and its seal must still match the bundle its arms clone. A v1 reference or one built on another P0 does not qualify.

A faithfully prepared backlog arm can be append-free on `56e66f07a`. Every tick then takes the idle decision, and the reference has no warm full build in that arm (#937 measured this on the #857 fixture). The loader therefore adjudicates a reference arm by arm (policy `per-arm/1`). When the reference's only defect is that backlog population, it is `usable` but not `valid`. That population must have complete, gap-free tick evidence with no sample over the arm's full duration, recorded and recomputed from the digest-bound raw diagnostics at the capture's recorded poll times. The capture's own verdict and job records must be consistent with exactly that defect. The capture file is never rewritten. The leg then runs every requested arm and gates append against append. It records backlog's full-build comparison as INVALID and uncertified, whatever the candidate's samples, and it still judges backlog's write, steady, RSS, API and maintenance gates. Such a leg exits 2 and never carries a verdict. Any other reference defect leaves the reference unusable, and no arm runs: incomplete evidence, an empty append population, a job or teardown problem, or a changed artifact. Builds are never manufactured to fill the population.

Every job holds the runner's host lock (retried every 30 s for at most 30 minutes, then refused) until its teardown completes, so the reference and the candidate run serially and never beside another runner job. A budget watchdog meters the backing disk's cumulative write counter: full-size jobs share 1.5 TB and 16 hours across P0, both P1s, the reference and the leg in `W/budget.json` (P0's rebuild alone writes about 600 GB, about 20 times the stores' net growth), and the relocation oracle has 30 GB and 1 hour per tree. A job stops early enough that its teardown still fits inside the budget, and an unreadable meter refuses or stops it.

Teardown is owned by the image lifecycle: it stops the appender, reaps the processes the job started, reaps the job's own detached workers, lists every holder of a file on the volume, detaches only the owned device and verifies the canonical root is gone. Every job mints a random owner token and passes it to each child in `CCTALLY_BENCH_OWNER_TOKEN`; a product worker that a child started in its own session inherits it. A holder whose own environment (read only for processes of the same user) carries exactly that token is an owned worker: its start identity is recorded when it is classified and checked again before each signal, and it is sent SIGTERM, then SIGKILL after the grace. The receipt lists each one under `ownedWorkers` with its proof and outcome; the product's own worker records, where the tree has them, only corroborate. The same rule runs before every seal and checkpoint. Any other holder the job did not start is reported and never signalled, and it makes the attempt INVALID, as does an owned worker that cannot be reaped. A watchdog stop makes the receipt INVALID even when it fires during in-process work. A refusal names its remedy; a stale mount or leftover directory at the canonical root is never removed automatically, so run the printed `hdiutil detach <device>` or `rmdir <canonical root>` yourself. A step's own images (arm clones, the oracle's clones) are removed after a clean detach; P0 and P1 bundles stay under `W` until the operator removes `W`.

The relocation oracle checks the mechanism for one tree on a synthetic small corpus, at its own canonical root `/private/tmp/cctally-937-oracle-<UTC>`, and writes one `W/oracle/<tree>/<case>.json` per case: `python3 -I bench/dashboard-soak.py --relocation-oracle --tree <sha> --root '<W>'`. Its v1 case must show a replay, and every other case (control, v2, a clean second verification, Codex and Claude replacement, append and discovery) must pass. The control is held to the same identity equality as the image cases, and the summary records the result as `controlIdentity`. In the replacement and append cases, catch-up reports the backlog not drained because the same-size Claude replacement is observed only and never detected. Exactly that cause, the one unconsumed entry being that file with reason "not consumed", is recorded as `expectedNotDrained` with its evidence; any other not-drained cause fails the case.

Absolute candidate ceilings are: 5% true-idle process CPU, 50%
sampled whole-process CPU, 25% combined main/conversation CPU duty, 1.5 GiB
RSS, 768 MiB summed retained-owner bytes, +4 MiB/min one-sided 95% post-warm
RSS slope, 5 s full-tick p50, 10 s full-tick p95, 3 s API p95, 10 s main and
conversation publication p95 with no individual gap over 15 s, and 10 s
mutation-to-render. Missing, nonfinite
or insufficient numeric evidence fails closed. Owner-specific caps, thread/I/O
bounds, shutdown, stress, SQLite settings and the earlier paired relative
latency/cadence comparisons remain in force; a receipt cannot raise an absolute
ceiling. The paired baseline must resolve to the true pre-epic commit
`2eb71fe3305fa91f2936a14fa935212283d72962`. A dirty candidate tree
cannot pass `--gate`.

The full-size `bothActive` regime gates only part of this list, by operator decision; see *Full-size `bothActive` gate* below. The direct soak and the other regimes are unchanged.

True-idle CPU is measured in a separate 180-second interval after warm-up and
manual refresh. The harness makes no HTTP/SSE request, changes no source, and
runs no stress action during that interval. CPU is cumulative process CPU-time
delta divided by wall time, not an instantaneous `ps %cpu` reading; at least
150 seconds and six samples are required. The backend trace must also cover
the interval with idle main ticks and caught-up conversation passes, so active
loop samples cannot be relabeled as idle.

`--baseline-ref` materializes that commit without changing a checkout and runs
both arms on one remote host. If the old binary cannot open the copied current
schema, create a separately measured pre-epic receipt on a compatible isolated
copy and pass it with `--compare`; do not substitute a post-epic baseline or
silently waive the comparison. In either form, both receipts must name the same
initial fixture-population metadata fingerprint (relative file paths, sizes
and mtimes) and the same measurement host. This detects a changed source
population without hashing every byte of a multi-GB DB; it is not a
cryptographic content proof, so review source-copy provenance separately.

The single soak cannot itself perform every adversarial regime. The revised `--produce-evidence` path first normalizes the multi-GB copy, runs due retention once through the real dashboard, and records the elapsed time, deleted payload bytes, reclaimed file bytes, the inherited backlog, and zero pending/freelist state. Normalization first runs the product's own foreground `cctally db rebuild --db stats`: a copy keeps its source build's stats epoch, and the candidate's first stats read would otherwise detach an epoch rebuild and make the clone's `cache-sync --rebuild` exit "retry shortly" (the 2026-09-25 full-size copy took 531 s at epoch 1015 to 1016). Production reclaim is budgeted to two seconds a pass and goes dormant below 256 MiB until the next daily run, so it cannot drain a multi-GB backlog inside any setup bound; the 2026-09-24 copy drained about 143 pages a second. When the deletion's budgeted continuation leaves a backlog, the prepass compacts the remainder with the product's own `cctally db vacuum --db conversations`, then restarts the dashboard until the product's next reclaim pass clears its now-stale record. The evidence names that `drainMechanism` (`production` or `production+compaction`) and records the production, compaction and settle stages separately, each with its own duration and database-family bytes. The validator fails closed unless a successful due deletion is followed by a budgeted reclaim, the payload deleted is non-zero, the stages' bytes chain consistently, and any second deletion during the settle is the labelled cleanup of a dormant record. The same drain runs before the template is staged from the direct soak's post-rebuild root and at the end of each cold preparation, once both rails have caught up after its Claude rebuild, because a from-zero replay restores rows older than retention and its forced prune deletes them again. The one-hour stats-rebuild bound, the four-hour prepass bound, the one-hour compaction bound and the 30-minute settle bound cover setup only; none alters a measured interval or ceiling. The producer then runs the direct soak and stages a template only after reclaim completes. `idle` and `bothActive` run on separate full-size clones. The eight other regimes run on separate small-fixture clones against the absolute ceilings of their regime. Both prior-schema upgrade probes run during the full-size producer on committed prior-schema fixture databases; those upgrade inputs are not multi-GB migration copies. The source, template, direct root, and each probe root remain available for read-back under unique paths. `--small-pipeline` exercises the complete two-tier producer and manifest validator with small fixtures in both tiers, but cannot certify full size. Each clone runs the real dashboard path before its named runtime action. The direct soak finishes its live measurement and stops the dashboard before `cache-sync --rebuild`; transcript recovery requires the database family to have no open readers. It then starts a dashboard on the rebuilt clone and requires `/api/data` to succeed. Active-provider regimes append real source records, publish activity, and wait until the running dashboard reports a full/final tick that holds the mutation: the cache population has increased, and each provider's published cache ID has advanced and equals the store's current cache signature; their `mutationToRenderMs` is measured from mutation to that observation, never from a standalone cache-sync command. Hook regimes exercise the runtime frontier against real marker and database state; the race runs cache ingestion across two real appends; and pricing skew tampers and re-derives materialized conversation costs. The degraded-reader regime holds the real maintenance flock while requesting `/api/conversations`, then observes HTTP recovery after release. Overload sends 80 concurrent HTTP requests to the isolated dashboard's `/api/diagnosis`, counts real overload responses and recovery, and samples that server process's RSS and thread count during the burst. The producer records only values observed from process/debug timing, HTTP results, runtime results, and SQLite state. Pytest pass counts, asserted success constants, and measurements copied from the direct soak are rejected as production evidence. It also copies the committed cache-044 and conversations-009 pre-migration fixtures into separate temporary data roots, opens them with the candidate CLI, and measures their exact schema, marker delta and integrity before and after migration. These upgrade roots and the soak root are isolated; the operator's source copy remains read-only. The producer requires `--candidate-sha` and compares the materialized tracked tree and untracked-file set with that exact commit, independent of the runner's current `HEAD`. It refuses a different checkout, the wrong baseline, a missing production fixture or an existing output/raw directory. Any failed/empty probe or out-of-bound measurement stops production without a manifest, so hand-authored JSON is not the supported evidence path.

The version-1 validator requires all artifacts to be contained regular
files under the bundle's directory (no symlinks); each
reference carries its exact SHA-256 digest. The bundle and each artifact bind
the candidate to the measured committed SHA, the true pre-epic baseline and
the fingerprint of its declared fixture tier. The full-size tier shares the
direct soak's fingerprint; the small tier has a distinct fingerprint. Distinct artifacts carry
`kind`, `name`, `measurements` (numbers; full-size `bothActive` also carries its `ungatedFields` list and may record `null` evidence), a unique `measurementRunId`, and an
observed `runtimeObservationCount`, plus a host, ordered timezone-aware
timestamps, the successful command argv/exit code and captured execution
duration, stdout and stderr. Every artifact
must name the direct soak's `measurementHost` (the runner's hostname). Duplicate
JSON keys and nonfinite numbers are
rejected. Regime commands that invoke pytest, execution receipts containing a
`passedCases` claim, duplicate run IDs, and identical common active-regime
measurements are rejected. An `externalEvidenceVerified: true` claim in a file
is never trusted.

The manifest has this shape (repeat for every required name; the digests here
are placeholders, not valid evidence):

```json
{
  "schemaVersion": 1,
  "candidateSha": "<40-hex-committed-candidate-SHA>",
  "baselineSha": "2eb71fe3305fa91f2936a14fa935212283d72962",
  "fixtureSourceFingerprint": "<64-hex-fingerprint-from-direct-soak>",
  "fixtureTiers": {"fullSize": "<direct-soak-fingerprint>",
                   "smallFixture": "<different-small-fixture-fingerprint>"},
  "pipelineMode": "fullSizeCertification",
  "retentionWhenDue": {"path": "raw/retention-when-due.json", "sha256": "<actual-sha256>"},
  "regimes": {
    "idle": {"path": "raw/idle.json", "sha256": "<actual-64-hex-sha256>"},
    "claudeActive": {"path": "raw/claude-active.json", "sha256": "<actual-sha256>"},
    "codexActive": {"path": "raw/codex-active.json", "sha256": "<actual-sha256>"},
    "bothActive": {"path": "raw/both-active.json", "sha256": "<actual-sha256>"},
    "missingHook": {"path": "raw/missing-hook.json", "sha256": "<actual-sha256>"},
    "ineffectiveHook": {"path": "raw/ineffective-hook.json", "sha256": "<actual-sha256>"},
    "mutationRace": {"path": "raw/mutation-race.json", "sha256": "<actual-sha256>"},
    "pricingSkew": {"path": "raw/pricing-skew.json", "sha256": "<actual-sha256>"},
    "degradedConversation": {"path": "raw/degraded-conversation.json", "sha256": "<actual-sha256>"},
    "requestOverload": {"path": "raw/request-overload.json", "sha256": "<actual-sha256>"}
  },
  "priorSchemaUpgrades": {
    "cache-044": {"path": "raw/cache-044.json", "sha256": "<actual-sha256>"},
    "conversations-009": {"path": "raw/conversations-009.json", "sha256": "<actual-sha256>"}
  }
}
```

Each referenced file has this envelope, with its **own** exact name and kind:

```json
{
  "schemaVersion": 1, "kind": "regime", "name": "missingHook",
  "fixtureTier": "smallFixture",
  "candidateSha": "<same-40-hex-SHA>",
  "baselineSha": "2eb71fe3305fa91f2936a14fa935212283d72962",
  "provenance": {
    "host": "<same-runner-alias>",
    "fixtureSourceFingerprint": "<small-fixture-fingerprint>",
    "startedAt": "2026-09-19T10:00:00Z",
    "finishedAt": "2026-09-19T10:03:00Z",
    "command": {"argv": ["python3", "bench/dashboard-soak.py",
                          "--execute-regime", "missingHook", "..."],
                "exitCode": 0}
  },
  "execution": {"durationMs": 812.4,
                "stdout": "{...runtime measurements...}", "stderr": ""},
  "measurements": {
    "measurementRunId": "<unique-64-hex-runtime-run-id>",
    "runtimeObservationCount": 6,
    "frontierExpirySeconds": 120, "invalidHookRejected": true,
    "fallbackRefreshCount": 2, "trustedFreshFrontierCount": 1,
    "untrustedStaleFrontierCount": 1, "observedMutationCount": 2,
    "mutationToRenderMs": 700
  }
}
```

Numeric bounds are checked by regime: idle needs a quiet 150-second/six-sample interval under 5% CPU; the small-fixture `claudeActive` and `codexActive` regimes each need four samples, their provider events, and absolute process/owner/CPU/duty/full-tick/API/publication/freshness ceilings; full-size `bothActive` needs the narrower gate described below; missing/ineffective hooks need observed trusted-fresh and untrusted-stale frontiers, invalidation, fallback and a visible mutation within 10 seconds; mutation race needs at least two observed and equally many rendered mutations, zero losses and freshness; pricing skew needs observed mismatches, equal recalculations and at most 1e-9 USD error; degraded conversation needs HTTP-observed degraded/recovered responses, zero unavailable-content leaks and bounded route latency; overload needs more than 64 concurrent HTTP requests, an observed overload response, HTTP recovery, zero unexpected responses and bounded server-process threads/RSS/API p95. The upgrades need their respective exact 44/9 starting heads, the current exact 46/10 heads afterward, exactly two/one applied migrations, integrity success, zero regressions and a finite positive duration. Unknown or missing regimes/heads fail. A `smallFixtureTrial` bundle cannot claim full-size verification, and the validator rejects an artifact assigned to the wrong tier. The expiry used by the direct soak is still read from the candidate source; external data cannot relabel a synthetic fixture, enlarge measured store size, or claim retention ran.

**Full-size `bothActive` gate.** By operator decision (#857 Task B), the full-size `bothActive` gate is narrower than the other active regimes, because at full size the product cannot meet the CPU, duty, build, freshness and publication-cadence ceilings. A full-size activity tick takes about 7 s and the product's duty cap stretches the cadence to 12.8–14.8 s, so only about three ticks fit in the probe window. The probe appends exactly one event per provider and never restimulates. It then requires every appended Claude and Codex event to become visible in a published tick within the unchanged probe window of `max(30, 8 × sync interval + 5)` seconds, which is 45 s at the default 5 s interval. Visible means a final full publication holds the events: the dataset count rose for both providers, and each provider's published cache ID has advanced and equals the store's current cache signature. The probe credits that publication even when one poll first sees both the higher count and the publication. The probe does not abandon 2 s after the 10 s freshness ceiling, as the small-fixture regimes do, and an event still invisible at the deadline fails the regime. Still gated at full size: no appended event missed (`observedClaudeEvents` ≥ `appendedClaudeEvents` ≥ 1, and the same for Codex), at least four process samples, `rssBytes` ≤ 1.5 GiB, `retainedOwnerBytes` ≤ 768 MiB and `apiP95Ms` ≤ 3 s. Still recorded as evidence but no longer gated at full size, and moved unchanged into #862 Task C's acceptance: `processCpuPercent` (50%), `combinedCpuDuty` (0.25), `fullBuildP50Ms`/`fullBuildP95Ms` (5 s / 10 s), `mutationToRenderMs` (10 s), and `publishP95Ms`/`publishMaxMs` and `conversationPublishP95Ms`/`conversationPublishMaxMs` (10 s / 15 s). Each of these is a finite non-negative number, or `null` when the run could not measure it (no full build, no usable publication period, or unmeasured CPU duty), except `mutationToRenderMs`, which is always measured because an invisible event fails the probe. The receipt lists all nine in `ungatedFields`, and the validator rejects any other list. The small-fixture `claudeActive` and `codexActive` regimes, the full-size `idle` regime, and every threshold, ceiling, duration and sampling setting are unchanged.

Produce the raw manifest first, then consume it in the paired gate on the same
remote host and unchanged candidate. Preserve both commands' output:

```bash
TZ=Etc/UTC bin/cctally-test-remote bench/dashboard-soak.py \
  --root /tmp/cctally-dashboard-evidence-<candidate-sha> \
  --fixture-copy <isolated-copy-path-on-runner> \
  --baseline-ref 2eb71fe3305fa91f2936a14fa935212283d72962 \
  --candidate-sha <candidate-sha> \
  --produce-evidence --duration-seconds 600 \
  --output /tmp/cctally-dashboard-evidence/manifest.json

TZ=Etc/UTC bin/cctally-test-remote bench/dashboard-soak.py \
  --root /tmp/cctally-dashboard-paired-<candidate-sha> \
  --fixture-copy <same-isolated-copy-path-on-runner> \
  --baseline-ref 2eb71fe3305fa91f2936a14fa935212283d72962 \
  --candidate-sha <candidate-sha> \
  --evidence /tmp/cctally-dashboard-evidence/manifest.json \
  --duration-seconds 600 \
  --output /tmp/cctally-dashboard-after.json --summary-only --gate
```

The companion real-browser pass is
`dashboard/web/e2e/memory-soak.spec.ts`. It loops long and short conversation
opens, reader/list/dashboard teardown, reload and EventSource reconnection,
then forces Chromium GC and records heap, DOM, document, listener, interaction,
subscriber, queued-delivery, and server-thread figures. The gate compares heap
and DOM medians between consecutive post-warmup windows at matching retained-
document counts, while document growth is gated separately, so a periodic
reload phase cannot masquerade as a positive per-cycle leak; regression slopes
remain diagnostic.
Thirty cycles are the default and `CCTALLY_BROWSER_SOAK_CYCLES=120` selects a
long pass.

This command is only one gate in #857 Task B. Preserve its exact JSON with the
browser, upgrade, mutation/concurrency, review, remote-suite, generation,
estate, CI and issue-state evidence for the same candidate SHA. Do not treat a
green soak, a synthetic fixture or verified digests as the frozen certification
verdict without independent raw-artifact review.

### Statement-cache cost campaign (`measure-778-statement-cache.py`)

`#778` disabled SQLite's per-connection statement cache on every guarded stats connection, so the authorizer runs on every execution rather than only at prepare. This driver measures what that costs. It compares a FIXED arm (the production `_cctally_store._STATS_CONNECT_KWARGS`) against a BASELINE arm that empties that constant, which is the only difference between the two.

Four workloads. `ingest` drives 160 `cctally record-usage` ticks through the real single-flight ingest cycle, crossing 96 integer weekly milestones (forty-eight in each of two weeks), closing five-hour blocks and taking one weekly reset to zero, then ten caught-up repeats, for 170 calls in total. `rebuild` runs one `rebuild_stats_index` plus its in-place publication over the million-line production-shaped journal `bin/build-journal-benchmark-fixture.py` writes. `dashboard` runs `bin/cctally-bench --scale large` and reads its own `snapshot.cold` / `snapshot.warm` / `snapshot.idle` medians, so corpus construction stays outside the reported metric even when it happens inside the run. `aa` is the A/A control: both labels run the production constant, so its separation is the noise floor the other three are read against.

Every measured run is a fresh subprocess over its own copied store, fixtures are built once outside every measured region, and rounds alternate ABBA so a monotone drift in machine load lands on both arms equally. The `dashboard` warm-up builds its corpus in the master store rather than in a scratch copy; an earlier form warmed through the per-run copy helper, which deleted the corpus it had just built and left every measured run rebuilding it inside its own timer. The campaign recorded for `#778` ran under that earlier form, so its `dashboard` wall and CPU summaries include corpus construction. Its verdict does not, because the verdict is read from `snapshot_total_s`. Results print to stdout as JSON, because the remote wrapper copies no files back. The verdict applies `max(15% of the baseline median, 15 ms)` — the same two tolerances `bin/cctally-bench` uses — to the paired 95% confidence interval: within budget when the whole interval sits inside it, `BREACH` when the whole interval sits beyond it, and `inconclusive` when the interval spans it, which forces a rerun rather than a pass.

```sh
CCTALLY_REMOTE_HOST=<runner-alias> bin/cctally-test-remote \
    python3 bench/measure-778-statement-cache.py campaign \
    --rounds 14 --workloads ingest aa --workdir /tmp/bench778
```

Pin one runner for a campaign, and clear the pin before any authoritative test run: a pinned host makes the receipt non-authoritative.

### Quota index remount control (`quota-index-remount.py`)

`#1080`: the Codex quota natural-key index records the identity of its keys file and of every journal segment it covers, and a sync whose stat no longer matches rebuilds the index. This driver checks, on real disk images, that remounting the volume that holds the data folder keeps the index valid. A remount gives the volume a new device number but keeps every inode, size, birth time and the volume UUID. The driver also checks that a store which is not the recorded one still invalidates the index. It is macOS only (`hdiutil`, `getattrlist`), runs on the Mac itself because it attaches disk images, takes about a minute, and is never part of a test run.

It extracts `git archive <sha>` of `--repo` under `--work`, so the receipt names the commit and tree that ran. It then creates fresh APFS sparse bundles A (the store), S (a spacer) and B, C, D (controls) under `--work`, attaches them without sudo under `/Volumes`, and builds the small bench fixture on A. `CCTALLY_DATA_DIR`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `HOME` all point into the images. Every image is detached before the driver exits. `--work` must not exist; put it on an external volume.

```sh
python3 bench/quota-index-remount.py --repo . --sha <commit> \
    --work /Volumes/<external>/<new dir> --label <tag>
```

- **Remount.** After sync-1 the driver detaches A, attaches S so that S takes A's device number, and re-attaches A, which therefore gets a new device number. It requires the keys file and every covered segment to keep their inode, size, birth time and volume while only the device changes, with the raw bytes of the sidecar and keys file unchanged.
- **Sync-2.** The driver appends one new Codex token_count event: a copy of a rollout's newest rate_limits line with a rising cumulative total. Before sync-2 runs, it fixes the two expected natural keys from the injected line's path and offset and from the logical limit keys the journal already records for the copied line. After sync-2, the index must have been consumed, not rebuilt:
  - the journal gained exactly those keys, once each;
  - the keys file is the same file;
  - its bytes are the old bytes plus exactly those two 19-byte lines;
  - the rebuild state is untouched;
  - the index is valid.
- **Sync-3.** The driver remounts back and runs sync-3, which must change nothing.
- **Controls.** Each control must leave the index invalid:
  - a copied store;
  - the store copied to another volume;
  - a forged store on another volume, with the keys file and every covered segment at A's recorded inode numbers and sizes, A's sidecar and different key content, with fresh and with A's birth times, attached beside A and then alone (where it reuses A's device number);
  - a replaced keys file;
  - a replaced covered segment.

The receipt is one JSON object on stdout. `checks` holds every boolean and `allChecks` is the verdict. `models` gives, for every state, the product's verdict and the verdict of each candidate identity rule evaluated on the same stats. On a tree before `#1080` the product must agree with `devIno` at every state, and the remount checks and both device-reuse controls fail. A pass is `allChecks: true`.

### Tunable constants

The regression tolerance is `max(BENCH_TOLERANCE_PCT × baseline, BENCH_TOLERANCE_FLOOR_MS)` — the proportional part (`0.15`) catches regressions on the big benches, and the absolute floor (`15.0` ms) stops sub-millisecond benches from flapping on same-machine noise and cleanly handles a zero/near-zero baseline (idle). `DEFAULT_ITERATIONS` (`5`), `DEFAULT_SEED` (`42`), and `DEFAULT_SCALE` (`large`) round out the knobs. All five are named constants at the top of `bin/cctally-bench` and are meant to be tuned after the first real run on a new reference machine: if the same-machine repeat-run spread on the big benches exceeds the floor, raise `BENCH_TOLERANCE_FLOOR_MS` and note it here. For `--assembly-scan`, `ASSEMBLY_VISIBLE_MS` (`100.0` ms, in `bin/cctally-bench`) is the visibility budget the threshold analysis solves against, and `ASSEMBLY_TURN_LADDER` / `ASSEMBLY_TURN_LADDER_SMALL` (in `bin/build-bench-fixtures.py`) are the full and self-test rung sets — editing either busts only the `assembly` scratch cache via the marker's `params_hash`.
