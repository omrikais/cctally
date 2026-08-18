# `cctally dashboard-perf`

Read a running dashboard's tick cost, and arm or disarm its deep phase trace.
Answers the question "why is my dashboard slow, and how slow is it?" from the
running process — no restart, and no `CCTALLY_PERF_TRACE=1` at launch.

Restarting to answer that question does not work: it discards both the
accumulated memos and the condition being diagnosed. So the counters this
command reads are always on, and the deeper phase trace is armed at runtime.

## Modes

| Mode | What it does |
|---|---|
| `cctally dashboard-perf` | Human-readable report from a running dashboard |
| `cctally dashboard-perf --json` | Machine-readable JSON to stdout (`schemaVersion: 1`) |
| `cctally dashboard-perf --trace on` | Arm the deep phase trace, then report |
| `cctally dashboard-perf --trace off` | Disarm the deep phase trace, then report |

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--host HOST` | `127.0.0.1` | Loopback **IP literal** of the running dashboard |
| `--port PORT` | the `cctally dashboard` default | Port of the running dashboard |
| `--token TOKEN` | none | Bearer token, when the dashboard minted one for LAN access |
| `--trace {on,off}` | none | Arm or disarm the deep phase trace on the running process |
| `--json` | off | Emit the stamped JSON envelope instead of the report |

`--host` accepts only an IP literal in `127/8` or `::1`. A hostname is refused
at argument validation, `localhost` included, and the command exits 2 without
opening a connection.

**This is stricter than the server, deliberately.** The endpoint's
anti-rebinding gate treats `localhost` and `::1` as loopback names before it
tries to parse an IP literal, so `Host: localhost:8789` is served with 200. The
command refuses a name anyway: a name is resolved by the operating system, the
CLI cannot verify that the resolver's answer is this machine, and an
unambiguous target is worth more here than the convenience. A hostname that is
not a loopback name, such as `evil.example.com`, is refused by the server too.
The command never contacts a LAN address.

An instance on a non-default port needs `--port`; the default is resolved the
same way `cctally dashboard` resolves its own.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | A decoded HTTP 200 |
| 2 | Argument validation — a non-loopback target, or a port outside 1–65535 |
| 3 | Connection, HTTP, authentication, timeout, or malformed response — including "no dashboard is running" |

## What the report says

**Publish period.** Three rows. The first, `all ticks`, is the period over
every retained tick whatever its Codex regime — the flagship number, and it is
always available when the process has published twice.

Beneath it the same period is stated separately for Codex-active and
Codex-idle ticks, because those are different populations: an idle tick reuses
the whole Codex source bundle and never executes the Codex leg, so a single
averaged period describes neither. The regime rows **refine** the first row;
they do not gate it. That distinction is not cosmetic — a dispatch-`idle` tick
returns before the Codex source build, so no build reaches the Codex decision
and the tick is `not_observed`, which the regime partition discards. On a
mostly-idle install both regime rows can read `no samples yet` while every
tick after the first has a perfectly good measured period.

Each row reports its sample count, the median, and the observed range. The
median rather than the mean, because one startup or recovery outlier otherwise
dominates a 64-sample window; the range keeps that outlier visible.

A row with no qualifying sample reads **`no samples yet`** with its count —
never a zero, a dash, or an omitted row. Telling "measured and fast" apart
from "not measured" is the point of the surface, and a zero cannot do it.

**Tick cost.** The mean ingest time, the mean builder time, and the mean whole
tick, plus the newest tick in full. The two halves are **mutually exclusive**:
nested time is subtracted in both directions, so a progressive-fill build that
runs inside the ingest is counted once, as builder time, and subtracted from
the ingest that contains it. `ingest_ns + builder_ns` is therefore always at
most `duration_ns`, and the remainder is orchestration and publish overhead
rather than being forced into either bucket.

A tick that did not ingest — a `--no-sync` dashboard — reports `ingest_ran`
false and zero, rather than null, because a null is indistinguishable from a
missing measurement.

**Cache pin.** The `cache.db` read transaction that the source build holds,
stamped at its own `BEGIN` and `ROLLBACK` boundaries. It is reported as held
**inside** the builder rather than as a third exclusive half, because it is a
subset of builder time and not a sibling of it — so it is *not* subtracted from
anything and `ingest_ns + builder_ns <= duration_ns` is unaffected. Measuring
it at the boundaries is deliberate: the enclosing function's own duration also
counts the work before `BEGIN` and after `ROLLBACK`, which makes that figure an
upper bound on the hold rather than the hold. A tick whose source bundle was
reused never opens the pin and reports zero. Within one refresh the figure is
the **sum** of holds rather than the longest single one, because a progressive
fill can run several builds and each opens its own pin.

**Conversation sync loop.** The dashboard's second work loop — transcript
ingest into `conversations.db` — reported beside the first. The rows are the
mean wall cost of a pass, the mean thread CPU time it consumed, the pass period
as a median with its observed range, the measured one-core share, and a count
per outcome (`ok`, `store_unavailable`, `error`).

The outcome describes the **store open and the two provider syncs** and nothing
else. The retention prune runs after them and swallows its own failures, so a
pass that succeeded at ingest and then failed only in the prune is still
counted as `ok`. A row whose status is missing, empty, or not a string is
counted as `malformed`, so a field set this reader does not understand cannot
appear as though it were an outcome name. An unrecognised outcome *name* is a
different case and is rendered verbatim: a newer server that publishes a fourth
status shows it as written rather than as `malformed`.

The share is `sum(cpu_ns) / sum(period_ns)`, taken from `time.thread_time_ns()`
over the loop's own thread. Both sums cover the **same** passes: a pass
contributes only when it carries a forward period, which is the gap from its
own start to the **next** pass's start. The newest retained pass therefore has
none — its successor has not started yet — and excluding it from both sums is
what keeps the ratio honest. Over the passes that do pair, the periods add up
to the interval from the oldest retained start to the newest one, and each
pass's CPU falls inside its own period, so the figure is the loop's true duty
over that span. Because numerator and denominator come from the same process,
it is a measurement rather than a claim about machine speed.

`work >= interval` bounds that share at 50%; see the conversation loop's duty
bound in [`backend-performance.md`](../backend-performance.md). An empty ring
reads **`no samples yet`**, never a zero — and under `--no-sync` the thread is
never started, so that is the correct and permanent reading in that mode.

**Dispatch mix.** Lifetime counts of `full`, `idle` and `degraded` ticks. The
three sum to the number of completed refresh ticks. Both this and the Codex
regime are aggregated over the whole outer refresh rather than taken from the
last build inside it: several builds can run in one refresh and disagree, and
last-write classification would file an expensive tick as an idle one.

**Group A cache-open failures.** Counts for the daily, weekly and monthly
bucket builders. Each of those falls back to a wide from-scratch fetch when it
cannot open the cache, with byte-identical output — so the failure is
otherwise invisible, and a persistent count here explains a slow tick that
looks healthy everywhere else.

**Phase trace state.** The applied state, the requested state, and when a
pending request takes effect. They are reported separately because
`--trace off` returns before the flip happens: the change is applied at the
dashboard's next authoritative build, so a request landing mid-ingest cannot
split one ingest across two tracing states.

## Arming the deep phase trace

`--trace on` requests the phase trace and returns immediately. The report it
then prints shows `requested on`, `applied off` and
`applies_at next_authoritative_build`. After the dashboard's next rebuild the
same command reports `applied on`, and the diagnostic carries a phase tree.

Arming the trace does not suppress the dashboard's progressive first-paint
publications. It used to; a diagnostic silently changing product behaviour was
the wrong trade, and the thread-state hazard behind it is now handled by
isolating the nested build instead.

`--trace off` disarms it the same way. The stored phase tree is not cleared by
disarming, so the report states the instant that tree was generated — a stale
tree reads as stale rather than as the last tick.

## JSON output

```json
{
  "schemaVersion": 1,
  "status": "ok",
  "diagnostic": { "...": "the server payload, verbatim" }
}
```

`schemaVersion` is stamped first and the wrapper (`status`, `diagnostic`) is
the stable part a script may key on. **`diagnostic` is explicitly opaque**: it
passes `/api/debug/backend` through unchanged, and that surface is a
diagnostic rather than a consumer contract — phase names, nesting and fields
may change without a version bump. On a failure the same envelope is emitted
with `"status": "error"`, an `error` message, and a null `diagnostic`.

## Privacy

The endpoint this reads is loopback-only and always has been: the unspoofable
TCP peer is the primary check, a loopback `Host` authority is the
anti-rebinding defence, and `dashboard.expose_transcripts` is never consulted.
"Loopback authority" means an IP literal in `127/8` or `::1`, or the names
`localhost` and `::1` — so a rebinding attempt through an attacker-controlled
hostname is refused while `Host: localhost` is served. The tick record
contains only timings, counts and enum names — no filesystem path and no
transcript text.

`--trace on/off` is a write, so it is gated by three layers in order: the
bearer token when the dashboard minted one, the loopback and anti-rebinding
check, and Origin/Host parity. The command sends an `Origin` matching the
`Host` it calls, which is what the third layer requires.

## See also

- `docs/commands/dashboard.md` — the dashboard itself
- `docs/backend-performance.md` — the measured backend performance record
- `cctally doctor` — the read-only health report
