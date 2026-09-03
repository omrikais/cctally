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

The 25pp threshold is deliberate: a slightly-behind API replica reports a marginally-lower number, and a 15pp drop is fundamentally ambiguous from percent-shape alone, so lowering the threshold would reintroduce exactly the false positives it guards against. `record-credit` is the deterministic, user-asserted escape hatch: you state the credit explicitly (zero false-positive risk), and the command repairs the local data so every current-week surface reads the credited value.

**Same week — no re-anchor, on EVERY path.** An Anthropic reset never changes the week's boundaries. Whatever Anthropic does to the counter — a partial goodwill credit, a full zeroing, an early reset — the week keeps its original boundaries (e.g. `2026-06-13 → 2026-06-20`) and only the running 7d percent steps down (46 → 31, then climbs again as usage resumes). `record-credit` records this by writing a row to `week_reset_events`, the one table an Anthropic credit lives in. That row defines an **accounting epoch** — a milestone ladder segment, a cost range, a high-water-mark floor — and never a display boundary, so the window-resolution code never re-anchors the week to the credit moment. The four MAX-clamp sites that derive the current 7d percent (the statusline 7d high-water mark, the `record-usage` write-site monotonic clamp, the `--from` default helper, and `project`'s per-week usage) read that epoch's instant and floor the displayed value to the post-credit reading while the window stays put.

Because the credit is a `week_reset_events` row, it also **restarts the milestone ladder** from the credited value. Before unification a manual credit restored the 7d number and could not restore the ladder, because it wrote no event and therefore opened no epoch.

The **≥25pp / reset-to-zero auto-detected** credit takes the same model. It used to re-anchor the week; it no longer does. If Anthropic ever genuinely moves a boundary, cctally renders the original cadence and disagrees with the API — a consequence accepted deliberately.

**Several credits in one week are representable.** A credit's identity is the journal record that caused it, not the week's boundaries, so two credits in one week — or two inside one hour — are two records. A repeat of the identical source record stays an idempotent retry.

This command is weekly/7d only. The 5-hour dimension has its own auto-detection and is out of scope.

## When to use it

- The 7d percent the statusline / `report` / dashboard shows is stuck at an old peak after Anthropic lowered your weekly usage mid-window by a sub-25pp, non-zero amount (e.g. 46% → 31%).
- Use `--dry-run` first to eyeball the plan; then re-run with `--yes` to apply.

## Options

| Flag | Required | Default | Description |
| --- | --- | --- | --- |
| `--to FLOAT` | yes | — | New post-credit weekly %. Must be in `[0, 100]` and strictly **less than** `--from` (a credit is a decrease). |
| `--from FLOAT` | no | current reset-aware HWM for the week | Pre-credit baseline. By default resolved to the same floored `MAX(weekly_percent)` the statusline and the write-site clamp use, so it matches what you currently see. When a credit floor already exists for the week (completion or `--force`), the default instead comes from that floor's recorded pre-credit value. |
| `--at ISO` | no | now | Effective credit moment, internally floored to the hour for the credit floor's timestamp. **Naive timestamps are treated as UTC** (not host-local). Must be within the resolved week window and not in the future. |
| `--week DATE` | no | the snapshot week whose window contains `--at`/now | `week_start_date` as `YYYY-MM-DD`. The default resolves the week window **containing `--at`** (correct at a reset edge, where the most-recent snapshot can belong to a just-ended week). If no snapshot week contains `--at`, the command refuses and points at `--week`. |
| `--dry-run` | no | off | Print the plan, write nothing, exit 0. Works with or without `--json`, on a TTY or not. |
| `--yes` | no | off | Apply without the confirm prompt. |
| `--force` | no | off | Replace the **one** credit occurrence `--at` names — its record, its own synthetic snapshot and its dependent milestones — then re-record. Every other credit in the week is untouched. No credit at that instant is a refusal (exit 2), and several credits at one instant is an ambiguity refusal. Never touches real status-line snapshots. |
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
  week:          2026-06-13 -> 2026-06-20 05:00 UTC
  from -> to:    46% -> 31%   (from: current HWM)
  effective:     2026-06-19 14:00 UTC  (floored from 2026-06-19 14:37)
  writes:
    + credit record         (effective=2026-06-19T14:00:00+00:00, pre_credit=46)
    ~ hwm-7d                46 -> 31
    - stale replays         0 rows
    + snapshot              captured=2026-06-19T14:37:00Z, weekly_percent=31
  note: same week — the window keeps its own boundaries
  (dry-run — nothing written)
```

After applying, `cctally report` (and the statusline / dashboard) read **31%** for the current week, the pre-credit 46% rows are preserved as history, a `week_reset_events` row records the credit and opens its milestone epoch, and the week keeps its original `2026-06-13 → 2026-06-20` boundaries (no re-anchor). As real usage resumes, the next `record-usage` tick at a value below the pre-credit peak (e.g. 37%) is stored normally rather than suppressed by the monotonic clamp.

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
    "creditFloorInserted": true,
    "hwm7dBefore": 46.0,
    "hwm7dAfter": 31.0,
    "staleReplaysDeleted": 0,
    "postCreditSnapshotInserted": true
  }
}
```

`fromSource` is `hwm` (defaulted, first credit), `explicit` (user-supplied `--from`), or `prior_credit` (defaulted on a completion / `--force` re-apply, read from the existing credit record). Validation and refusal errors stay plain-text on stderr even under `--json`, so consumers should check the exit code, not stdout.

## Re-running: completion, refuse, and `--force`

Because the underlying repair commits the credit floor and cleanup *before* the post-credit snapshot, a crash in between can leave a **half-applied** credit (floor row present, no snapshot — every post-credit read then sees an empty segment). A plain rerun (no `--force`) detects this and **finishes it** — no special flag needed, and it **reuses the existing floor's effective moment** rather than re-flooring to the rerun's "now", so a rerun at a later wall-clock can't strand stale pre-credit readings inside the floored window.

A **fully-applied** credit at the instant `--at` names (record plus a command-owned snapshot) is **refused by default** (exit 2). A plain run at a DIFFERENT instant adds another occurrence rather than refusing. Pass `--force` for a clean re-do of one occurrence: it removes that occurrence's record, the synthetic snapshot keyed on it and the milestones that depend on it — never your real status-line snapshots, and never another credit in the same week — and then re-records.

## Exit codes

- `0` — success, including `--dry-run` and an interactive decline.
- `2` — validation error (out-of-range, `--to >= --from`, future/out-of-window `--at`, no snapshot week, `--json` without `--yes`/`--dry-run`, non-TTY without `--yes`/`--dry-run`) or a refused already-fully-applied credit.
- `3` — a database error.

## Notes

- "Now" routes through the same `CCTALLY_AS_OF` testing hook the other reporting commands use.
- The effective credit moment is floored to the hour; the synthetic snapshot is captured at the un-floored `--at`, so the floored `MAX` always includes it.
- Milestones **segment**: the credit writes a `week_reset_events` row, and the milestone ladder (keyed on `reset_event_id`) starts a new segment from the credited value. The percent milestones you already crossed stay recorded on the segment they were crossed in, so they neither re-fire nor disappear, and the credit does not undo the cost already spent. Inside the new segment the ladder starts at the credited level and rises with the climb. This is the change unification makes: before it, a manual credit restored the 7d number and left the ladder where the pre-credit peak had put it.
- The credit opens an accounting epoch; it does **not** re-anchor the week. A credited week renders as ONE row on its original boundaries, marked `+`, and its `$/1%` is measured from the credit forward over the climb since it. When the counter has not yet climbed past the level it was credited to, that ratio is withheld with a typed cause rather than rendered.
- No schema migration is involved. `week_reset_events` gains its columns through the normal schema init and the epoch bump, so a dev/worktree binary can still open and repair the production DB.

## See also

- [`record-usage`](record-usage.md) — the ingestion path whose auto-detector this command complements
- [`report`](report.md) — joins usage snapshots with cost; the surface that reflects the repair
- [`percent-breakdown`](percent-breakdown.md) — per-percent milestones for a week
