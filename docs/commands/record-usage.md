## Optional: status-line integration (no OAuth API calls)

`record-usage` is now an **opt-in alternative** to the default hook-based
integration installed by `cctally setup`. Use it if you specifically want to
avoid OAuth API roundtrips and prefer your usage data to come from the
status-line stdin Claude Code already provides.

To wire it up, add this snippet to your `~/.claude/statusline-command.sh`
(after parsing `week_pct`/`week_resets`/`five_pct`/`five_resets` from the
status-line JSON):

```bash
if [ -n "$week_pct" ] && [ -n "$week_resets" ]; then
    record_args="--percent $week_pct --resets-at ${week_resets%.*}"
    if [ -n "$five_pct" ] && [ -n "$five_resets" ]; then
        record_args="$record_args --five-hour-percent $five_pct --five-hour-resets-at ${five_resets%.*}"
    fi
    cctally record-usage $record_args &
fi
```

Trailing `&` matters — `record-usage` runs in the background so it never blocks
the status-line render.

Both paths (status-line snippet and the default hook integration) can run
simultaneously; the `record-usage` funnel dedupes correctly.

---

# `record-usage`

Record a usage-percent snapshot from the Claude Code status-line `rate_limits`
data. Writes to `weekly_usage_snapshots` and, if the snapshot crosses a new
integer percent, also to `percent_milestones`.

## Synopsis

```
cctally record-usage
    --percent PERCENT
    --resets-at RESETS_AT
    [--five-hour-percent FIVE_HOUR_PERCENT]
    [--five-hour-resets-at FIVE_HOUR_RESETS_AT]
```

## Purpose

This is the **ingestion** end of the data pipeline. It is not meant to be
called interactively — your status-line script calls it after every status
render so the SQLite snapshot history stays fresh.

## When to use it

- Wired into `~/.claude/statusline-command.sh`. See
  [installation.md](../installation.md#optional-opt-in-status-line-integration-no-oauth-api-calls).
- Manually replaying a missed snapshot if your status-line script crashed
  (rare; the cost is one missing data point).

## Options

| Flag | Required | Description |
| --- | --- | --- |
| `--percent` | yes | 7-day utilization percentage (0–100). |
| `--resets-at` | yes | 7-day window reset timestamp (Unix epoch seconds). |
| `--five-hour-percent` | no | 5-hour utilization percentage (0–100). |
| `--five-hour-resets-at` | no | 5-hour window reset timestamp (Unix epoch seconds). |

## Examples

```bash
cctally record-usage \
    --percent 14.2 \
    --resets-at 1744531200

cctally record-usage \
    --percent 14.2 --resets-at 1744531200 \
    --five-hour-percent 38.5 --five-hour-resets-at 1744502400
```

## Notes

- **Run in the background** from your status line (trailing `&`). It must
  not block status-line render.
- `--resets-at` is normalized to the nearest hour to absorb Anthropic's
  jitter — so a single subscription week always resolves to one
  `week_start_at` value.
- Crossing a new integer percent fires a `percent_milestones` write
  exactly once. Re-recording a snapshot at the same or lower percent is a
  no-op for milestones.
- Milestones store cumulative cost **at the moment of crossing** — they
  are deliberately never backfilled.

## See also

- [`percent-breakdown`](percent-breakdown.md) — render the milestones recorded here
- [`report`](report.md) — joins these snapshots with cost
- [Architecture · week boundaries](../architecture.md#week-boundaries)
