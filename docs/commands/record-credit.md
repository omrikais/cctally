# `record-credit`

Record an **in-place weekly (7d) credit** — a case where Anthropic lowered your 7-day usage counter mid-window *without* a clean reset to zero — and repair the local data so reports and the statusline read the credited value instead of the stale pre-credit high-water mark.

## Synopsis

```
cctally record-credit
    --to TO
    [--from FROM]
    [--at AT]
    [--week WEEK]
    [--dry-run] [--yes] [--force] [--json]
```

## Purpose

cctally already auto-detects two shapes of an in-place weekly credit during `record-usage`: a **big drop** (≥25 percentage points, fired immediately) and a **reset-to-zero** (post-value ≤1%, debounced). A real incident on 2026-06-19 fell between the cracks: the weekly counter changed **46% → 31%**, a 15pp drop whose post-value is nowhere near zero. That is below the 25pp threshold and is not a zero-collapse, so neither auto-detector fires — and the write-site monotonicity clamp then treats the lower 31% reading as a lagging-replica regression and refuses to store it, leaving every display pinned at the stale 46%.

The 25pp threshold is deliberate: a slightly-behind API replica reports a marginally-lower number, and a 15pp drop is fundamentally ambiguous from percent-shape alone, so lowering the threshold would reintroduce exactly the false positives it guards against. `record-credit` is the deterministic, user-asserted escape hatch: you state the credit explicitly (zero false-positive risk), and the command performs the same repair the auto-detector would, plus the one piece the live path normally gets for free — a post-credit snapshot at the new value.

This command is weekly/7d only. The 5-hour dimension has its own auto-detection and is out of scope.

## When to use it

- The 7d percent the statusline / `report` / dashboard shows is stuck at an old peak after Anthropic lowered your weekly usage mid-window by a sub-25pp, non-zero amount (e.g. 46% → 31%).
- Use `--dry-run` first to eyeball the plan; then re-run with `--yes` to apply.

## Options

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `--to FLOAT` | yes | — | New post-credit weekly %. Must be in `[0, 100]` and strictly **less than** `--from` (a credit is a decrease). |
| `--from FLOAT` | no | current reset-aware HWM for the week | Pre-credit baseline. By default resolved to the same floored `MAX(weekly_percent)` the statusline and the write-site clamp use, so it matches what you currently see. When a credit event already exists for the week (completion or `--force`), the default instead comes from that event's recorded pre-credit value. |
| `--at ISO` | no | now | Effective credit moment, internally floored to the hour for the reset-event timestamp. **Naive timestamps are treated as UTC** (not host-local). Must be within the resolved week window and not in the future. |
| `--week DATE` | no | the snapshot week whose window contains `--at`/now | `week_start_date` as `YYYY-MM-DD`. The default resolves the week window **containing `--at`** (correct at a reset edge, where the most-recent snapshot can belong to a just-ended week). If no snapshot week contains `--at`, the command refuses and points at `--week`. |
| `--dry-run` | no | off | Print the plan, write nothing, exit 0. Works with or without `--json`, on a TTY or not. |
| `--yes` | no | off | Apply without the confirm prompt. |
| `--force` | no | off | Re-record when a credit is already **fully** recorded for the week. Deletes only that week's command-owned (`source='record-credit'`) snapshots, the event row, and its dependent milestones, then re-records. Never touches real status-line snapshots. |
| `--json` | no | off | Machine output, `schemaVersion: 1`. Must be paired with `--yes` (apply) or `--dry-run` (preview); otherwise refused. |

## Confirm matrix

`record-credit` writes to the real database, so it never applies silently. `--dry-run` previews and writes nothing. `--yes` applies without prompting. With neither flag on a TTY it prints the preview and asks `Proceed? [y/N]` — anything other than `y`/`yes` (including EOF) aborts with exit 0 and nothing written. With neither flag on a non-TTY it refuses (exit 2) rather than hang. `--json` never prompts and must be paired with `--yes` or `--dry-run`.

## Worked example (46% → 31%)

```bash
# 1. Preview the plan (nothing written).
cctally record-credit --to 31 --dry-run

# 2. Apply.
cctally record-credit --to 31 --yes
```

The dry-run prints a preview like:

```
record-credit — weekly in-place credit
  week:        2026-06-13 -> 2026-06-20 05:00 UTC
  from -> to:  46% -> 31%   (from: current HWM)
  effective:   2026-06-19 14:00 UTC  (floored from 2026-06-19 14:37)
  writes:
    + week_reset_events  (effective=2026-06-19T14:00:00+00:00, pre_credit=46)
    ~ hwm-7d             46 -> 31
    - stale replays      0 rows
    + snapshot           captured=2026-06-19T14:37:00Z, weekly_percent=31
  (dry-run — nothing written)
```

After applying, `cctally report` (and the statusline / dashboard) read **31%** for the current week, the pre-credit 46% rows are preserved as history, and a `week_reset_events` row records the credit.

## `--json` envelope

`--json --yes` emits the applied envelope (and `--json --dry-run` emits the equivalent plan with `applied: false`, `dryRun: true`). All datetimes are UTC (`…Z`):

```json
{
  "schemaVersion": 1,
  "applied": true,
  "dryRun": false,
  "forced": false,
  "week": {
    "weekStartDate": "2026-06-13",
    "weekStartAt": "2026-06-13T05:00:00Z",
    "weekEndAt": "2026-06-20T05:00:00Z"
  },
  "credit": {
    "fromPct": 46.0,
    "toPct": 31.0,
    "fromSource": "hwm",
    "effectiveAtUtc": "2026-06-19T14:00:00Z"
  },
  "actions": {
    "resetEventInserted": true,
    "hwm7dBefore": 46.0,
    "hwm7dAfter": 31.0,
    "staleReplaysDeleted": 0,
    "postCreditSnapshotInserted": true
  }
}
```

`fromSource` is `hwm` (defaulted) or `explicit` (user-supplied `--from`). Validation and refusal errors stay plain-text on stderr even under `--json`, so consumers should check the exit code, not stdout.

## Re-running: completion, refuse, and `--force`

Because the underlying repair commits the reset event and cleanup *before* the post-credit snapshot, a crash in between can leave a **half-applied** credit (event present, no snapshot — every post-credit read then sees an empty segment). A plain rerun (no `--force`) detects this and **finishes it** — no special flag needed.

A **fully-applied** credit (event plus a command-owned snapshot) is **refused by default** (exit 2), naming the recorded pre-credit value and effective time. Pass `--force` for a clean re-do: it deletes only that week's `source='record-credit'` synthetic rows, the event row, and its dependent milestones — never your real status-line snapshots — and then re-records.

## Exit codes

- `0` — success, including `--dry-run` and an interactive decline.
- `2` — validation error (out-of-range, `--to >= --from`, future/out-of-window `--at`, no snapshot week, `--json` without `--yes`/`--dry-run`, non-TTY without `--yes`/`--dry-run`) or a refused already-fully-applied credit.
- `3` — a database error.

## Notes

- "Now" routes through the same `CCTALLY_AS_OF` testing hook the other reporting commands use.
- The effective reset moment is floored to the hour; the synthetic snapshot is captured at the un-floored `--at`, so the post-credit segment's `MAX` always includes it.
- Per-percent milestones from 1 up to `--to` are **not** backfilled (there is no authentic per-percent crossing cost to record); milestones above `--to` re-fire normally as you re-climb.
- No schema migration is involved — the command only writes rows to tables that already exist.

## See also

- [`record-usage`](record-usage.md) — the ingestion path whose auto-detector this command complements
- [`report`](report.md) — joins usage snapshots with cost; the surface that reflects the repair
- [`percent-breakdown`](percent-breakdown.md) — per-percent milestones for a week
