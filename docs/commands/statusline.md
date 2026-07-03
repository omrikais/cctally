# `statusline`

One-line status string for Claude Code's `statusLine` hook.

> Canonical form: [`cctally claude statusline`](claude.md) (this flat form remains as an alias).

`cctally statusline` is a drop-in replacement for `ccusage statusline` —
the ccusage line shape is honored byte-for-byte for the segments cctally
emits, plus two intentional improvements over upstream:

1. **Context % is computed, not `N/A`.** ccusage defaults `🧠 N/A`;
   cctally tail-reads the most recent assistant `message.usage` block in
   the transcript JSONL and divides into the model's context window.
2. **`5h X% · 7d Y%` extension** (default-on). Appended after the
   ccusage-shape segments when stdin carries `rate_limits` or cctally's
   snapshot DB has a recent row. Opt out with `--no-cctally-extensions`.

A third divergence is the stdin contract: ccusage exits 1 on missing
`transcript_path` (and other fields); cctally only exits 1 on malformed
JSON or non-object root. Every other field absence degrades gracefully
so the hook line never breaks the user's status bar.

## Synopsis

```
cctally statusline [-h]
                   [-B {off,emoji,text,emoji-text}]
                   [--cost-source {auto,cctally,cc,both}]
                   [--cache | --no-cache]
                   [--refresh-interval N]
                   [--context-low-threshold N]
                   [--context-medium-threshold N]
                   [-z TZ]
                   [-O | --no-offline]
                   [--color | --no-color]
                   [--cctally-extensions | --no-cctally-extensions]
                   [--usage-only | --no-usage-only]
                   [--config PATH]
                   [--single-thread]
                   [-d]
```

Reads the Claude Code hook payload from stdin. Writes the rendered line
to stdout. Exits 0 on success.

## Output line shape

```
🤖 Sonnet 4.5 | 💰 $1.23 session / $46.36 today / $13.48 block (3h 22m left) | 🔥 $10.30/hr | 🧠 35% | 5h 34% (3h 22m) · 7d 42% (6d 14h)
```

Five `|`-delimited segments, left to right:

| # | Segment | Source |
|---|---|---|
| 1 | `🤖 <model>` | stdin `model.display_name`; falls back to `model.id`, then `Unknown model`. |
| 2 | `💰 $X.XX session / $Y.YY today / $Z.ZZ block (Hh Mm left)` | session per `--cost-source`; today bucketed in `display.tz`; block = active 5h block; `time-left = block_end − now`, clamped `≥ 0m`. |
| 3 | `🔥 $X.XX/hr` (+ optional visual indicator) | active 5h block cost ÷ elapsed hours. `-B` controls the visual. |
| 4 | `🧠 X%` (or `🧠 N/A`) | tail-read last assistant `message.usage`, divided by `CLAUDE_MODEL_CONTEXT_WINDOWS[<model_id>]`. |
| 5 | `5h X% (Hh Mm) · 7d Y% (Dd Hh)` — **cctally extension, default-on** | stdin `rate_limits` → DB HWM clamp → DB-latest-row fallback → suppress. |

When `--cost-source both` is in effect, segment 2's `session` slot
collapses to a side-by-side view:

```
💰 ($X.XX cc / $Y.YY cctally) session / $Z.ZZ today / $W.WW block (Hh Mm left)
```

`both` only affects the `session` slot; `today` and `block` always
render the cctally figure.

When `--no-cctally-extensions` is in effect, or when the chain produces
no data, segment 5 is omitted:

```
🤖 Sonnet 4.5 | 💰 ... | 🔥 ... | 🧠 35%
```

When `--usage-only` is in effect, cctally renders just the subscription
usage percentages and omits the ccusage-shaped telemetry segments plus
reset countdowns:

```
5h 34% · 7d 42%
```

If no 5h/7d usage data is available, `--usage-only` writes an empty line.

## Flag reference

| Flag | Values | Default | Behavior |
|---|---|---|---|
| `-B`, `--visual-burn-rate` | `off`, `emoji`, `text`, `emoji-text` | `off` | Segment 3 visual indicator (see below). Config key `statusline.visual_burn_rate`. |
| `--cost-source` | `auto`, `cctally`, `cc`, `both` | `auto` | Session cost source (see below). The legacy `ccusage` value is rejected with a rename hint. Config key `statusline.cost_source`. |
| `--cache`, `--no-cache` | bool | on | **No-op alias.** cctally renders from cache.db directly; no extra output cache. |
| `--refresh-interval N` | int seconds | `1` | **No-op alias.** Accepted for ccusage drop-in compat. |
| `--context-low-threshold N` | int 0-100 | `50` | Segment 4 `🧠 X%` green band: `pct < N`. |
| `--context-medium-threshold N` | int 0-100 | `80` | Segment 4 yellow band: `pct < N`; else red. Must be `> --context-low-threshold`. |
| `-z`, `--timezone TZ` | IANA name | `display.tz` config or `UTC` | Display tz for the `today` calendar-day bucket. |
| `-O`, `--offline`, `--no-offline` | bool | offline | **No-op alias.** cctally is always offline. |
| `--color`, `--no-color` | bool | auto | ANSI on/off. Auto = TTY-attached stdout AND `NO_COLOR` env unset. |
| `--cctally-extensions`, `--no-cctally-extensions` | bool | on | Append (or suppress) segment 5. Config key `statusline.cctally_extensions`. |
| `--usage-only`, `--no-usage-only` | bool | off | Render only `5h X% · 7d Y%` subscription usage percentages. Config key `statusline.usage_only`. |
| `--config PATH` | path | unset | Read config from PATH for this invocation only (no mutation of the persisted default). Missing/unreadable/non-object-JSON PATH exits 2. Parity with the 10 sibling Claude reporting commands. |
| `--single-thread` | flag | off | **No-op alias.** |
| `-d`, `--debug` | flag | off | Print pricing-mismatch / config diagnostics on stderr. |

## `--cost-source`

Controls where segment 2's `session` slot reads from:

| Value | Behavior |
|---|---|
| `auto` (default) | cctally cache.db when transcript readable + session_id present + cache hit; falls through to `cc` otherwise. |
| `cctally` | cache.db only. Returns `$0.00` when the cache has no rows for the session id. |
| `cc` | stdin `cost.total_cost_usd`. Returns `$0.00` if the field is absent. |
| `both` | Side-by-side `($X.XX cc / $Y.YY cctally) session`. Useful when comparing pricing tables. |

The legacy `ccusage` value name was renamed to `cctally`. Passing
`--cost-source ccusage` exits 2 with:

```
cctally statusline: error: argument --cost-source: invalid choice: 'ccusage' — cctally renamed it; try --cost-source cctally
```

The `today` and `block` slots always read from cctally (matching ccusage's
own contract — `today` and `block` don't exist as ccusage-side concepts).

## `-B` / `--visual-burn-rate`

Controls the burn-rate indicator appended to segment 3:

| `-B` value | Output |
|---|---|
| `off` (default) | `🔥 $X.XX/hr` |
| `emoji` | `🔥 $X.XX/hr 🟢` |
| `text` | `🔥 $X.XX/hr (Normal)` |
| `emoji-text` | `🔥 $X.XX/hr 🟢 (Normal)` |

Burn-rate bands (mirror ccusage at the time of writing):

| Band | Condition | Emoji | Text |
|---|---|---|---|
| Normal | `< $15.00/hr` | `🟢` | `Normal` |
| Moderate | `< $30.00/hr` | `🟡` | `Moderate` |
| High | `≥ $30.00/hr` | `🔴` | `High` |

## Context %

Segment 4 reads the last assistant turn's `message.usage` from the
transcript JSONL pointed to by stdin `transcript_path`, then divides:

```
context_tokens = usage.input_tokens
                + usage.cache_read_input_tokens
                + usage.cache_creation_input_tokens
context_pct = context_tokens / CLAUDE_MODEL_CONTEXT_WINDOWS[model_id] * 100
```

The table is keyed by `model_id` first; falls back to a family-substring
match (`sonnet`/`opus`/`haiku` → 200_000) if the exact id is unknown.
The `[1m]` variants (Sonnet 4.5 / Opus 4.7) carry an explicit 1_000_000
entry.

Unknown model id → `🧠 N/A` plus a one-shot stderr warning per process.
Missing `transcript_path` (or unreadable file) → `🧠 N/A` silently.

Color bands (defaults — overridable via `--context-low-threshold` /
`--context-medium-threshold`):

- `pct < 50` → green
- `pct < 80` → yellow
- `pct ≥ 80` → red

## cctally extension segment

Segment 5 is cctally-specific and on by default. It renders the user's
weekly subscription consumption inline so the bash status-bar wrapper
doesn't need to maintain its own polling. The source-priority chain:

1. **Stdin `rate_limits`** (freshest). Both `five_hour` and `seven_day`
   sub-blocks are independently optional.
2. **DB-latest fallback** when stdin lacks `rate_limits` entirely:
   `SELECT five_hour_percent, weekly_percent, five_hour_window_key,
   week_end_at FROM weekly_usage_snapshots ORDER BY captured_at_utc
   DESC LIMIT 1`.
3. **HWM monotonic clamp** within window: percentages monotonic-up only.
   Reads the same SQL chokepoint `record-usage` writes to.
4. **Suppress segment 5** when the chain produces nothing.

Color bands (fixed for v1):

- `< 60%` → green
- `< 85%` → yellow
- `≥ 85%` → red

Opt out with `--no-cctally-extensions` for strict ccusage-shape output.

## `--config PATH`

Same surface as the 10 sibling Claude reporting commands (`daily`,
`monthly`, `weekly`, `session`, `blocks`, `forecast`, `range-cost`,
`cache-report`, `diff`, `project`): reads config from PATH for this
invocation only — no mutation of the persisted default at
`~/.local/share/cctally/config.json`. Missing / unreadable / non-object
JSON exits 2 with a clear stderr message.

## Configuration persistence

Four keys join `display.tz` in the cctally config:

| Key | Values | Default |
|---|---|---|
| `statusline.visual_burn_rate` | `off`, `emoji`, `text`, `emoji-text` | `off` |
| `statusline.cost_source` | `auto`, `cctally`, `cc`, `both` | `auto` |
| `statusline.cctally_extensions` | `true`, `false` | `true` |
| `statusline.usage_only` | `true`, `false` | `false` |

Precedence (high → low): CLI flag > config key > built-in default.
Invalid config values emit a one-shot stderr warning per process and
fall back to the built-in default — the hot statusline path never exits
nonzero on a config typo.

Set via `cctally config set <key> <value>`. Unset (revert to default)
via `cctally config unset <key>`.

```bash
cctally config set statusline.visual_burn_rate emoji-text
cctally config set statusline.cctally_extensions false
cctally config set statusline.usage_only true
cctally config get statusline.cost_source
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. The rendered line is on stdout. Every absent stdin field degrades gracefully (e.g. `model` absent → `🤖 Unknown model`; `transcript_path` absent → `🧠 N/A`). |
| 1 | Stdin is not parseable JSON, OR the root is not a JSON object (e.g. `[]`, `42`, `"x"`). The error message is on stderr; stdout is empty. |
| 2 | argparse rejected a flag (e.g. `--cost-source ccusage`), OR `--config PATH` is missing / unreadable / non-object JSON. |

## Examples

```bash
# Default invocation from the CC hook
cctally statusline < /tmp/cc-hook-payload.json

# Strict ccusage shape (drop the cctally extensions)
cctally statusline --no-cctally-extensions < /tmp/cc-hook-payload.json

# Subscription usage chip only
cctally statusline --usage-only < /tmp/cc-hook-payload.json

# Side-by-side cc vs. cctally cost
cctally statusline --cost-source both -B emoji-text < /tmp/cc-hook-payload.json

# Pin context % thresholds for a custom workflow
cctally statusline --context-low-threshold 40 --context-medium-threshold 70 < /tmp/cc-hook-payload.json

# Per-invocation config override (test a settings change without saving)
cctally statusline --config /tmp/custom-cctally.json < /tmp/cc-hook-payload.json
```

## See also

- `cctally record-usage` — writes the `rate_limits` snapshots that segment 5 reads from when stdin lacks them.
- `cctally hook-tick` — internal CC hook that keeps the session-entry cache warm.
- `cctally blocks --active` — the same active-5h-block kernel statusline reuses for segments 2's `block` slot + segment 3's burn rate.
- `cctally claude statusline` — canonical hierarchical form.
