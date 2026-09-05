# `cctally alerts` — threshold notifications

Opt-in OS notifications when usage crosses a percent threshold on the
weekly subscription axis or the 5h-block axis. Off by default; once
enabled, alerts fire automatically from `record-usage` on any new
percent crossing — no extra wiring beyond enabling the feature. The OS
popup is dispatched cross-platform — `osascript` on macOS, `notify-send`
on Linux, or a custom command (see [Dispatch backends](#dispatch-backends)).

> **Cost coverage:** Claude costs shown in weekly, five-hour, or budget alert
> payloads are [transcript-derived lower
> bounds](../claude-cost-coverage.md), not exact `/usage` billing totals.
> Codex budget alerts use the separate Codex accounting source.

## When it helps

You're pacing fine right now, but a long agent run mid-week could push
you past 90% before you notice. Threshold alerts surface the crossing
the moment it lands in the next snapshot, with a Notification Center
popup, a dashboard toast (if open), and a persistent line in the
"Recent alerts" panel.

## Enable

CLI:
```
cctally config set alerts.enabled true
```

Or in the dashboard: open Settings (`s`) → **Alerts** → **Claude alerts**
→ check **Enable threshold alerts**. The overlay has a filter, so typing
`alerts.enabled` finds the row directly. The dashboard mirrors via `POST
/api/settings`; both paths share the same `config.json` writer lock.

## Defaults

```
alerts:
  enabled: false
  projected_enabled: false
  weekly_thresholds: [90, 95]
  five_hour_thresholds: [90, 95]
```

When you enable alerts and start a fresh week, the first crossing of 90%
fires once; the next crossing of 95% fires once. Re-crossings within the
same window are deduped — `alerted_at IS NOT NULL` on the milestone row
gates re-fire.

## Projected weekly pace

Set `alerts.projected_enabled` to `true` to receive a separate alert when the
current subscription week's projected end value reaches 90% or 100%. The
alert uses the same projection `cctally forecast` publishes: a supported
`cctally quota` calibration supplies the model-backed value, while a week the
calibration cannot support uses the corrected meter pace. A low-confidence
forecast does not fire a projected alert.

The calibration is read and the current week's retained entries are scanned
on the recording path only when a fitted regime validates. The computed value
is reused for the alert; it is not replaced by the meter and is not scanned a
second time.

## Test the pipeline

```
cctally alerts test [--axis AXIS] [--threshold N] [--metric METRIC]
```

`--axis` takes `weekly`, `five-hour`, `budget`, `project-budget`,
`codex-budget`, `projected`, `quota` or `meter-rate-change`.

Sends a synthetic alert through the same dispatch pipeline and
`alerts.log` writer as a real crossing, but with `mode=test` in the log
line so it's distinguishable. No DB writes, no envelope mutation. Use it
to verify your notifier is wired up before relying on real crossings.

`quota` and `meter-rate-change` are the two families that are **not**
percentage-threshold axes, and until #699 neither could be rehearsed at all:
the only way to see either notification was to wait for a real Codex quota
crossing or a real metering-rate transition. Both now build a synthetic that
goes through the same payload builder production uses.

`--threshold` does not apply to `--axis meter-rate-change` and supplying it
exits 2, because a metering-rate change has no percentage threshold and
carries an explicit severity instead. `--axis quota` takes `--threshold`
normally.

Both synthetics resolve a **real account key** from the registry when the
vendor has one, and fall back to the vendor-wide sentinel when it does not.
That is what makes the R8 `[label]` title prefix observable from this command
on a decorated install, while an install with one account or none sees output
that is byte-identical to what it saw before.

The first stdout line reports the **resolved notifier** for this host +
config, e.g.:

```
notifier: osascript
Test alert dispatched (mode=test). Check Notification Center.
```

The notifier is resolved the same way a real crossing resolves it (see
[Dispatch backends](#dispatch-backends) below): `osascript` on macOS,
`notify-send` on Linux, `command` when you've set a custom
`alerts.command_template`, or `none` when no backend is available on this
host. This line is informational — it prints even when the dispatch
itself produces no OS popup (`notifier: none`).

Or click **Send test alert** in the dashboard Settings overlay, under
**Alerts**; the
backend's `POST /api/alerts/test` echoes the synthetic payload back so
the dashboard can render a toast even if no native notifier is available.

Exit codes for the CLI form:

- `0` — alert queued (notifier spawned successfully).
- `1` — the spawned native notifier raised `FileNotFoundError` (e.g.
  `osascript` is selected but missing from `PATH`).
- `2` — `--threshold` out of `[1, 100]`.
- `3` — any other non-queued outcome: a spawn error (`PermissionError`,
  `OSError`), or no popup fired — `none` selected, or an explicitly chosen
  native notifier unavailable on this host (status `no_notifier:*`).

## Surfaces

- **Native OS popup.** Spawned non-blocking via the resolved notifier —
  `osascript display notification` on macOS, `notify-send` on Linux, or a
  custom `alerts.command_template`. If the OS notification surface is off
  or in Do Not Disturb, the popup is silently dropped (we cannot detect
  this).
- **Dashboard "Recent alerts" panel.** Press `9` (or click the panel) to
  open the modal with the full alert history for the current envelope.
  Collapsible from the panel header chevron.
- **Dashboard toast.** Transient pill near the top of the page when a new alert lands. Distinct visual variant from status toasts; colored by the 3-tier severity (see [Severity](#severity)). Click anywhere on it to dismiss it, or Tab to it and press Enter or Space — the toast is focusable and carries the alert's own sentence as its accessible name, and a keyboard dismissal returns focus to wherever it came from. An arriving toast never takes focus by itself. On an install with more than one real account per provider the head also names the account, and the dismiss instruction then reads `tap or click to dismiss` on its own line below the alert's body rather than `click to dismiss` inside the head; a single-account install renders the in-head form unchanged.
- **`alerts.log` audit line.** One tab-delimited line per dispatch
  attempt at `~/.local/share/cctally/logs/alerts.log`. The eight columns
  are: `timestamp`, `axis`, `threshold`, `window_key`, `mode`
  (`real`/`test`), `status` (`queued` / `no_notifier:<reason>` /
  `spawn_error:…`), `severity` (`info`/`warn`/`critical`), and
  `account_key` (the crossing's account; `*` for vendor-wide rows — #341).
  The severity column is the 7th field; the account_key column is the 8th.
  The 8th field is unconditional (the log is runtime state, exempt from the
  R8 byte-stability contract).

## Configuring threshold lists

Threshold lists are read-only in the dashboard for v1 — edit
`~/.local/share/cctally/config.json` directly:

```json
{
  "alerts": {
    "enabled": true,
    "weekly_thresholds": [80, 90, 95],
    "five_hour_thresholds": [90, 95]
  }
}
```

The dashboard re-reads on the next SSE tick after the file changes
(visible in the Settings overlay's read-only list).

### Validation rules

- Each item is an integer in `[1, 100]`.
- The list is **strictly increasing** (`[90, 95]` ok; `[95, 90]` rejected).
- No duplicates.
- Non-empty.

A malformed `alerts` block fails closed: alerts are disabled until the
config is fixed (the validator emits a one-shot stderr warning on the
next read).

## Severity

Severity is a 3-tier mapping from the crossed threshold (axis-uniform):

| Tier | Threshold | Dashboard color | `notify-send` urgency |
|------|-----------|-----------------|-----------------------|
| `info` | `< 90` | indigo | `low` |
| `warn` | `90`–`99` | amber | `normal` |
| `critical` | `>= 100` | red | `critical` |

This drives the dashboard toast color, the panel row color chip, and the
Linux `notify-send -u` urgency token. The mapping is a single authority
(`bin/_lib_alert_axes.py::severity_for`, kept byte-identical with
`dashboard/web/src/lib/alertAxis.ts::alertSeverity`); a legacy `amber`
token from a stale backend normalizes to `warn`, `red` to `critical`.

## Dispatch backends

The notifier that fires the OS popup is resolved per host + config by
`alerts.notifier` (and, for the `command` backend, `alerts.command_template`).
See [`config.md`](config.md#alerts-dispatch-keys) for the full key
reference, validation rules, and the trusted-execution / `shell=False`
safety model. Summary:

| `alerts.notifier` | Effect |
|-------------------|--------|
| `auto` (default) | `command_template` (if set, on any OS) → `osascript` on macOS → `notify-send` on Linux → `none`. |
| `osascript` | macOS `display notification`; downgrades to `none` off macOS. |
| `notify-send` | Linux `notify-send -u <urgency> -- <title> <body>`; downgrades to `none` if Linux/binary unavailable. |
| `command` | Spawn `alerts.command_template` (requires it to be set). |
| `none` | No OS popup; log + dashboard surfaces only. |

`auto` + a `command_template` overrides the native backend on **every**
platform — set the template to take over dispatch regardless of OS. An
explicitly-selected native notifier that is unavailable on this host
downgrades to `none` (it is never spawned-and-failed).

## Next step on every alert

Every alert body ends with one line naming the command that explains that
alert, scoped to its own provider and window:

```
→ Run `cctally percent-breakdown --week-start 2026-04-27` — claude · 2026-04-27 00:00 → 2026-05-04 00:00 UTC
```

The command's arguments are UTC, because that is what the selectors they feed
accept; the scope statement after the em dash renders in your `display.tz`.
The line is part of the message body, so `alerts.log` is unchanged.

| Axis | Command it offers |
|---|---|
| `weekly` | `cctally percent-breakdown --week-start <week>` |
| `five_hour` | `cctally five-hour-breakdown --block-start <block>` |
| `budget` | `cctally budget` |
| `codex_budget` | `cctally budget` |
| `project_budget` | `cctally project --since <start> --until <last day> --project <name>` |
| `projected` | `cctally forecast --explain`, or `cctally budget` for a budget metric |
| `quota` | `cctally codex quota breakdown --reset-at <reset>` |

`cctally budget` and `cctally forecast` take no window selector: each reports
whichever window is live when you run it. An alert whose window has already
closed therefore offers nothing and says why, rather than sending you to a
different window than the one the line names. That covers the budget family,
and the `projected` axis on both its budget metrics and its weekly-percent
metric.

The window an alert names is the window it fired against, re-measured with
current data and current pricing. It is not a reconstruction of what the
numbers looked like at firing time, and the current window is never
substituted for it.

A weekly window states the reset instant your subscription week actually runs
from, which is normally not midnight. Where the crossing predates the column
that records it, the window is stated as two calendar dates with no clock
reading and no zone, because the recorded row does not carry the reset hour:

```
→ Run `cctally percent-breakdown --week-start 2026-03-30` — claude · 2026-03-30 → 2026-04-06
```

Where the alert did not retain enough to derive a window, the line says so and
offers nothing:

```
→ No scoped explanation: the projected alert retains no period for metric 'budget_usd', so its window length is unknown
```

The same line appears on the CLI warning states of `forecast`, `budget`,
`cache-report`, `project` and `diff`, so the form is learned once. Note that a
`percent-breakdown` offered by a weekly alert inherits the issue #213 gap
described in [`percent-breakdown`](percent-breakdown.md): after an in-place
weekly credit, no milestone is recorded until usage passes the pre-credit
peak.

## Limitations

- **No popup exit-code detection.** `Popen` is fire-and-forget;
  `alerted_at` records "we queued the OS popup," not "the user saw the
  popup."
- **OS notification surface off / Do Not Disturb silently drops the
  popup.** The dashboard panel and toast still surface the alert; only
  the OS popup is affected.
- **Windows native popup deferred.** No built-in `BurntToast` backend;
  use `alerts.notifier=command` with a `command_template` to wire one up.
- **v2 features deferred** (per-project budgets, threshold list editing
  in the dashboard).

## Examples

```bash
cctally config set alerts.enabled true
cctally alerts test
cctally alerts test --axis five-hour --threshold 95
cctally alerts test --axis quota --threshold 90
cctally alerts test --axis meter-rate-change
cctally config get alerts.enabled
```
