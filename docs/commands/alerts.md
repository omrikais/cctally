# `cctally alerts` — threshold notifications

Opt-in macOS notifications when usage crosses a percent threshold on the
weekly subscription axis or the 5h-block axis. Off by default; once
enabled, alerts fire automatically from `record-usage` on any new
percent crossing — no extra wiring beyond enabling the feature.

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

Or in the dashboard: open Settings (`s`) → **Threshold alerts** → check
**Enable threshold alerts**. The dashboard mirrors via `POST
/api/settings`; both paths share the same `config.json` writer lock.

## Defaults

```
alerts:
  enabled: false
  weekly_thresholds: [90, 95]
  five_hour_thresholds: [90, 95]
```

When you enable alerts and start a fresh week, the first crossing of 90%
fires once; the next crossing of 95% fires once. Re-crossings within the
same window are deduped — `alerted_at IS NOT NULL` on the milestone row
gates re-fire.

## Test the pipeline

```
cctally alerts test [--axis weekly|five-hour] [--threshold N]
```

Sends a synthetic alert through the same osascript spawn and
`alerts.log` writer as a real crossing, but with `mode=test` in the log
line so it's distinguishable. No DB writes, no envelope mutation. Use it
to verify Notification Center is wired up before relying on real
crossings.

Or click **Send test alert** in the dashboard Settings overlay; the
backend's `POST /api/alerts/test` echoes the synthetic payload back so
the dashboard can render a toast even if osascript isn't available.

Exit codes for the CLI form:

- `0` — alert queued (osascript spawned successfully).
- `1` — osascript missing on this host (not macOS, or binary unavailable).
- `2` — `--threshold` out of `[1, 100]`.
- `3` — other spawn error (PermissionError, OSError, etc.).

## Surfaces

- **macOS Notification Center popup.** Spawned via `osascript display
  notification`; non-blocking. If Notification Center is off or in Do Not
  Disturb, the popup is silently dropped (we cannot detect this).
- **Dashboard "Recent alerts" panel.** Press `9` (or click the panel) to
  open the modal with the full alert history for the current envelope.
  Collapsible from the panel header chevron.
- **Dashboard toast.** Transient pill near the top of the page when a
  new alert lands; click to dismiss. Distinct visual variant from status
  toasts (amber for `<95%`, red for `>=95%`).

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

For v1, severity is hardcoded:

- `< 95%` → amber (caution).
- `>= 95%` → red (warning).

This applies to dashboard toast color and the panel row color chip.
Multi-tier severity (per-threshold severity overrides) is deferred to
v2.

## Limitations (v1)

- **macOS only.** Linux (`notify-send`) and Windows (`BurntToast` /
  `New-BurntToastNotification`) backends are deferred to v2.
- **No `osascript` exit-code detection.** `Popen` is fire-and-forget;
  `alerted_at` records "we queued the OS popup," not "the user saw the
  popup."
- **Notification Center off / Do Not Disturb silently drops the popup.**
  The dashboard panel and toast still surface the alert; only the OS
  popup is affected.
- **v2 features deferred** (custom command exec on alert, per-project
  budgets, severity overrides, non-macOS backends, threshold list
  editing in dashboard).

## Examples

```bash
cctally config set alerts.enabled true
cctally alerts test
cctally alerts test --axis five-hour --threshold 95
cctally config get alerts.enabled
```
