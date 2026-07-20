# `cctally codex percent-breakdown`

Per-percent cumulative and marginal Codex cost milestones for one native
seven-day quota cycle. Its terminal output uses the same week header, section
label, boxed table, columns, alignment, and number formatting as
[`cctally percent-breakdown`](percent-breakdown.md).

## Synopsis

```text
cctally codex percent-breakdown
    [--reset-at ISO-8601]
    [--speed {auto,standard,fast}]
    [--root-key FULL_SOURCE_ROOT_KEY]
    [--limit-key FULL_LOGICAL_LIMIT_KEY]
    [--sync]
    [--config PATH]
    [--tz TZ]
    [--json]
```

## Cycle selection

Without `--reset-at`, the command selects the active native seven-day cycle at
the current time. `--reset-at` selects a retained cycle by its exact reset
timestamp; date-only input is rejected, a naive timestamp means UTC, and an
offset-aware timestamp is normalized to UTC.

The selected evidence must resolve to exactly one seven-day quota identity.
Use the exact, case-sensitive `--root-key` and `--limit-key` selectors when
multiple Codex roots or logical limits are retained. Independent percentages
are never summed, averaged, or merged. A zero or ambiguous match exits `2` and
prints the available root/limit candidates.

## Cost and five-hour correlation

The command reads the same durable `quota_percent_milestones` projection used
by the dashboard hero modal. Token and cost totals are correlated to the
selected source root and physical observation tuple, then repriced at query
time. `--speed` selects the standard or fast Codex pricing tier; `auto` uses
the configured service tier.

The `5h at crossing` column uses the latest matching native five-hour
observation for the same source root, observed slot, and provider limit at the
crossing time. If that evidence is genuinely absent, the cell is `n/a`.

Like the Claude equivalent, the command reads the already-materialized durable
projection by default. This keeps the normal report path fast and does not scan
rollout files. `--sync` explicitly refreshes retained Codex rollout data and
reconciles the projection before rendering.

## Options

| Flag | Description |
| --- | --- |
| `--reset-at ISO-8601` | Exact retained seven-day reset. Defaults to the active cycle. |
| `--speed {auto,standard,fast}` | Query-time Codex pricing tier. |
| `--root-key FULL_SOURCE_ROOT_KEY` | Exact source-root selector. |
| `--limit-key FULL_LOGICAL_LIMIT_KEY` | Exact logical-limit selector. |
| `--sync` | Refresh retained Codex evidence and its projection before rendering. |
| `--config PATH` | Read display settings from another config file for this invocation. |
| `--tz TZ` | Display timezone (`local`, `utc`, or an IANA name). |
| `--json` | Emit a stamped machine-readable envelope. |

## JSON contract

JSON is stamped first with `schemaVersion: 1` and includes `source: "codex"`,
the selected root-qualified `identity`, `weekStartDate`, `weekEndDate`,
`weekStartAt`, `weekEndAt`, `generatedAt`, and:

```text
milestones: [{
  percentThreshold,
  cumulativeCostUSD,
  marginalCostUSD,
  capturedAt,
  fiveHourPercentAtCrossing
}]
```

This is the existing `percent-breakdown` milestone vocabulary with an additive
provider marker. Consumers must tolerate additive fields. JSON timestamps are
UTC with a `Z` suffix.

## Examples

```bash
cctally codex percent-breakdown
cctally codex percent-breakdown --root-key <full-root-key> --limit-key <full-limit-key>
cctally codex percent-breakdown --reset-at 2026-07-15T15:00:00Z --speed standard
cctally codex percent-breakdown --json
```

## See also

- [`percent-breakdown`](percent-breakdown.md) — the Claude weekly equivalent
- [`codex quota breakdown`](codex-quota.md#breakdown) — the lower-level
  arbitrary-duration native block view
- [`codex`](codex.md) — all Codex reporting commands
