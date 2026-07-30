# `cctally codex quota`

Provider-native Codex quota history, current status, forecasts, reset blocks,
and percent-crossing breakdowns. This is a nested-only cctally extension; it
does not have a flat alias and is not a `ccusage-codex` drop-in.

Each result stays qualified by source root, logical limit, observed slot, and
the actual native duration. cctally never sums, averages, or relabels
independent quota percentages as a Claude five-hour or seven-day window.

## Synopsis

```text
cctally codex quota history [--since DATE_OR_ISO] [--until DATE_OR_ISO] [COMMON]
cctally codex quota statusline [--as-of ISO-8601] [COMMON]
cctally codex quota forecast [--as-of ISO-8601] [COMMON]
cctally codex quota blocks [--since DATE_OR_ISO] [--until DATE_OR_ISO] [COMMON]
cctally codex quota breakdown --reset-at ISO-8601 [--speed {auto,standard,fast}] [COMMON]

COMMON = [--root-key FULL_SOURCE_ROOT_KEY]
         [--limit-key FULL_LOGICAL_LIMIT_KEY]
         [--no-sync] [--config PATH] [--json]
```

## Local-rollout data and freshness

These commands read quota observations retained from local Codex rollout files.
By default they first run one Codex cache sync; `--no-sync` reads the retained
local evidence without that sync. Either path reconciles the durable quota
projection before rendering, so a previously interrupted cache/stats update can
heal without mutating the physical observations.

`freshnessSource` is always `"local-rollout"`. It is not a provider-live API
call and Codex has no OAuth refresh counterpart. Freshness uses the latest
physical local capture for an identity:

- `fresh` — capture age is within
  `max(900, min(windowMinutes * 6, 3600))` seconds.
- `stale` — capture age is above that bound.
- `future` — capture is more than 300 seconds later than `--as-of`.
- `unavailable` — no valid capture exists.

Captures up to 300 seconds ahead are `fresh` for freshness but are never a
forecast baseline. Forecasting and alert qualification use only observations
at or before `--as-of`; a future observation cannot produce a current value or
an alert by itself.

## Selectors and timestamps

`--root-key` and `--limit-key` are exact, case-sensitive selectors for the
full `sourceRootKey` and `logicalLimitKey`; prefix matching is never used. Omit
both to operate on every active identity, or provide either one to filter by
that component. A selected zero-match exits `2` and prints candidate
root/limit pairs on stderr.

`breakdown` must resolve to exactly one identity, then `--reset-at` must resolve
to exactly one native reset block. Date-only `--reset-at` is rejected; a naive
timestamp is UTC, and an offset-aware timestamp is normalized to its UTC
instant. Zero or ambiguous matches exit `2` with candidates. These commands
use cctally-native usage errors: malformed selectors and timestamps also exit
`2`.

`history` and `statusline` preserve OpenAI's raw `resetsAt` evidence. `blocks`,
`breakdown`, and the `--reset-at` selector use the canonical reset anchor that
cctally establishes when ingesting the complete observation population. When
provider jitter made the raw value join a nearby anchor, copying `resetsAt`
from `history` into `--reset-at` will not select the block; use the `resetAt`
shown by `blocks` (or its JSON output) instead.

For `history` and `blocks`, `--since` is inclusive and `--until` is exclusive.
A date-only bound is interpreted in `display.tz`; an ISO datetime must include
an offset and carries its own timezone. `statusline` and `forecast` accept
`--as-of`; a naive value is UTC. `--config PATH` supplies display settings for
that one invocation. JSON timestamps are always UTC with a `Z` suffix.

## Commands

### `history`

Shows root-qualified physical quota observations, including captured time,
used percent, reset time, an opaque source-path fingerprint, and line offset.
History preserves observed decreases and out-of-order physical evidence; it
does not manufacture a zero when a rollout has no quota window.

```bash
cctally codex quota history --root-key <full-root-key>
cctally codex quota history --since 2026-07-15 --until 2026-07-16 --json
```

### `statusline`

Shows one truthful native segment for every selected identity. A normal current
window is `ok`; a stale local capture is marked `STALE`. A capture beyond the
future-skew allowance is marked `FUTURE DATA`: a prior eligible observation is
shown when present, otherwise the current value is unavailable.

```bash
cctally codex quota statusline
cctally codex quota statusline --as-of 2026-07-15T12:00:00Z --json
```

### `forecast`

Forecasts each identity and native reset block independently. It derives a
rate only from adjacent observations with both positive elapsed time and a
positive percentage change, then clamps the projection to the current value
through 100%. It never combines roots, limits, slots, or reset windows.

The status is one of `ok`, `insufficient-history`, `unavailable`, `stale`, or
`future`. Forecasts need at least one usable interval; confidence is `low`,
`medium`, or `high` when a rate is available. `future` and `stale` are useful
diagnostics, not a basis for projected alert qualification.

```bash
cctally codex quota forecast --limit-key <full-limit-key>
cctally codex quota forecast --as-of 2026-07-15T12:00:00+03:00 --json
```

### `blocks`

Shows provider-native reset blocks. A block is keyed by the complete quota
identity plus its canonical reset anchor; `nominalStartAt` is reset minus the
actual `windowMinutes`. The first and last observed times may cover only part
of that interval. A reset outside the anchor tolerance starts a new block; this
is not the Claude five-hour blocks command.

```bash
cctally codex quota blocks --since 2026-07-15
```

### `breakdown`

Shows integer-percent crossings for one selected native block. The first
observation establishes the block's high-water baseline and emits no
milestone. Later upward jumps add the crossed integers; decreases and recovery
below the high water do not remove or recreate them.

Token and cost totals are correlated to the selected source root and physical
observation tuple at query time. `--speed {auto,standard,fast}` uses the
shipped Codex pricing-tier behavior, so current pricing applies to historical
breakdowns. `auto` resolves across the complete configured Codex root set, as
the existing accounting commands do.

```bash
cctally codex quota breakdown \
  --root-key <full-root-key> \
  --limit-key <full-limit-key> \
  --reset-at 2026-07-15T15:00:00Z \
  --speed standard
```

## JSON contract

Every leaf emits a stamped-first camelCase envelope with `schemaVersion: 1`,
`source: "codex"`, `generatedAt`, and `freshnessSource: "local-rollout"`.
Consumers must tolerate additive fields.

Shared nested shapes are:

```text
identity = {source, sourceRootKey, logicalLimitKey, observedSlot,
            windowMinutes, limitId, limitName}
freshness = {state, source: "local-rollout", capturedAt, ageSeconds,
             staleAfterSeconds}
observation = {capturedAt, usedPercent, resetsAt, sourcePathKey, lineOffset}
```

`sourcePathKey` is an opaque 32-hex-character path fingerprint, not a local
path. The top-level leaf-specific fields are:

| Command | JSON fields after the shared top level |
| --- | --- |
| `history` | `windows: [{identity, freshness, orphaned, observations}]` |
| `statusline` | `windows: [{identity, freshness, status, current, label}]`, where `current` is `{usedPercent, resetsAt}` or `null` |
| `forecast` | `forecasts: [{identity, freshness, status, currentPercent, ratePercentPerHour, projectedPercent, resetsAt, remainingSeconds, sampleCount, sampleSpanSeconds, confidence}]` |
| `blocks` | `blocks: [{identity, resetAt, nominalStartAt, firstObservedAt, lastObservedAt, firstPercent, currentPercent, orphaned}]` |
| `breakdown` | `identity`, `block: {resetAt, nominalStartAt}`, `speed`, `milestones: [{percent, capturedAt, inputTokens, cachedInputTokens, outputTokens, reasoningOutputTokens, totalTokens, costUSD, marginalCostUSD}]` |

Empty history/block results retain their top-level arrays. `statusline` emits
each selected active identity; `forecast` retains its explicit null/zero field
shape for unavailable or only-future evidence rather than inventing values.

## Alerts and lifecycle

Quota alerts are opt-in. Both the existing global `alerts.enabled` gate and
`alerts.quota.enabled` must be true. The quota block defaults to disabled,
with actual thresholds `[90, 95]` and no projected thresholds. Configure it as
one JSON object:

```bash
cctally config set alerts.quota \
  '{"enabled":true,"actual_thresholds":[90,95],"projected_thresholds":[],"rules":[]}'
```

Threshold lists contain strictly increasing integer percentages from 1 through
100; an empty list deliberately disables that class. Optional `rules` are
exact per-source/root/logical-limit overrides with all five keys:
`source`, `source_root_key`, `logical_limit_key`, `actual_thresholds`, and
`projected_thresholds`. A rule never matches by label, slot prefix, or a
partial root key.

The first activation of a rule suppresses already-satisfied thresholds as
backfill. Later qualifying crossings claim one durable terminal event per
identity/reset/threshold. An actual and projected qualification share that
claim, so a later kind cannot send a duplicate alert. Cache rebuild/recovery
retains alerted and suppressed claims.

Trusted setup-managed Codex `Stop`/`SubagentStop` hooks run the local tick
automatically. See [setup](setup.md#codex-quota-lifecycle-hooks) for the
trust-review boundary, multi-root behavior, and recovery of malformed hook
configuration.

## Multiple Codex accounts

Every quota window, block, milestone and alert is recorded against the account whose rollout produced it, so two accounts sharing one `$CODEX_HOME` keep separate weekly percentages, separate reset boundaries and separate milestone ladders. The account is decided once, when a rollout's bytes are first read, and journaled — a `cctally cache-sync --rebuild` replays those decisions instead of re-deriving them from whoever happens to be signed in at rebuild time. History recorded before that mechanism existed stays in the `unattributed` bucket rather than being guessed at.

On the command line, `--account <ref>` scopes `history`, `statusline`, `forecast`, `blocks` and `breakdown` to one account; see [account](account.md#account-refs) for how a ref is resolved. Without the flag the output is byte-identical to a single-account install.

In the dashboard the same axis is the account chip row under the hero (`a` cycles through it). Selecting an account switches the quota blocks, the milestone history and the hero's percentage, reset and spend to that account; opening a past cycle from the hero fetches that account's own cycle, so a cycle key minted from one account's history never resolves against another's. An account with no recorded evidence shows an explicit empty state instead of the previously selected account's numbers. Under "All accounts" the hero merges only spend and tokens — with more than one live cycle the headline percentage and reset are blank and each account's own values appear on its card, because independent quota allowances are never summed or averaged. All of this appears only when more than one real Codex account is known; a single-account install renders exactly as before. See [account](account.md#the-dashboard-account-selector) for the full selector behaviour.

Windows that run on a separate model allowance, such as GPT-5.3-Codex-Spark, keep their own pool and are never filed as account-level weekly quota, whichever account they belong to.

## Limitations and recovery

- Codex quota freshness is local rollout evidence, not a provider-live usage
  refresh. Run `cctally cache-sync --source codex` to reread local rollout data.
- Missing or malformed individual quota windows degrade that window only; they
  do not block Codex accounting or other valid windows.
- The commands do not add quota data to `report`, `$ / 1%`, dashboard panels,
  share formats, or the frozen TUI. Dashboard reconciliation is S4 work.
- A cache rebuild, root pruning, or truncation can orphan no-longer-observed
  interpreted rows. Current output excludes them; terminal alert evidence is
  retained so it cannot refire solely from recovery. Use
  `cctally cache-sync --source codex --rebuild`, then rerun the command.
- For hook lifecycle state and stale/future local captures, run
  `cctally doctor`; repair a malformed `hooks.json` before rerunning
  `cctally setup`.

## See also

- [`codex`](codex.md) — Codex accounting subgroup
- [`codex percent-breakdown`](codex-percent-breakdown.md) — current or retained
  native seven-day milestones in the canonical weekly table design
- [`setup`](setup.md) — Codex hook installation and trust review
- [`doctor`](doctor.md) — local quota freshness and lifecycle diagnostics
