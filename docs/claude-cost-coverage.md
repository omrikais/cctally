# Claude cost coverage

cctally's Claude dollar and token totals are a **transcript-derived lower
bound**, not an exact reconstruction of Claude Code's `/usage` billing totals.
This limit applies even when the local transcript cache is complete and model
pricing is current.

## What cctally includes

cctally recursively reads the normal Claude Code JSONL history under
`~/.claude/projects/`, including main, resumed, and subagent transcripts. It
prices assistant records that retain a timestamp, model, and `message.usage`.
When streamed and finalized records share `(message.id, requestId)`, cctally
keeps the fuller record.

`message.usage.iterations[]` is a breakdown of the same top-level usage fields
in the observed records, not additional usage. Adding it to the top-level
fields would double-count billed tokens.

## What Claude Code does not retain

Controlled, quiescent comparisons against Claude Code `/usage` identified at
least two billed request classes whose usable accounting fields are absent from
normal transcripts:

- **Title generation.** The transcript may retain an `ai-title` marker, but the
  marker contains no model, request identity, or token usage.
- **Prompt suggestions / side queries.** Claude Code can make a separate
  billed request after the main turn without retaining any corresponding
  normal transcript record.

Because these records do not carry model or token counts, cctally cannot
reconstruct their cost, synthesize a safe model row, or historically backfill
them. The size of the gap varies with behavior and version. A measurement from
one session is not a universal correction factor, so cctally does not apply a
percentage uplift or estimate the missing amount.

## Affected surfaces

The lower-bound contract applies to every Claude cost surface derived from
local session history:

- live rollups such as `daily`, `monthly`, `weekly`, `session`, `blocks`,
  `project`, `diff`, `range-cost`, and `cache-report`;
- forecasts, budgets, status-line/TUI/dashboard cost figures, and shareable
  artifacts built from those rollups;
- `sync-week` and the `report` snapshots it produces; and
- five-hour blocks and percent milestones whose stored cost was derived from
  transcript history at record time.

Changing embedded pricing can correct the rate for usage that is retained; it
cannot recover a billed call that has no usable transcript usage fields.
Historical stored snapshots and milestones are not rewritten to guess at the
missing amount.

Codex accounting uses a different retained source and is not covered by this
Claude-specific limitation.

## Comparing with `/usage`

Use Claude Code `/usage` when you need its billed Session total. For a
reproducible comparison, stop all work in the session, wait for main and
subagent transcript files to stop growing, freeze their exact byte prefixes,
then compare by model and token field. `/usage` is rounded and is not a durable
per-request source, so it cannot serve as a historical backfill for invisible
calls.
