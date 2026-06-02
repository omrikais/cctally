# `cctally alerts` — threshold notifications

Opt-in OS notifications when usage crosses a percent threshold on the
weekly subscription axis or the 5h-block axis. Off by default; once
enabled, alerts fire automatically from `record-usage` on any new
percent crossing — no extra wiring beyond enabling the feature. The OS
popup is dispatched cross-platform — `osascript` on macOS, `notify-send`
on Linux, or a custom command (see [Dispatch backends](#dispatch-backends)).

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

Sends a synthetic alert through the same dispatch pipeline and
`alerts.log` writer as a real crossing, but with `mode=test` in the log
line so it's distinguishable. No DB writes, no envelope mutation. Use it
to verify your notifier is wired up before relying on real crossings.

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

Or click **Send test alert** in the dashboard Settings overlay; the
backend's `POST /api/alerts/test` echoes the synthetic payload back so
the dashboard can render a toast even if no native notifier is available.

Exit codes for the CLI form:

- `0` — alert queued (notifier spawned successfully).
- `1` — the native notifier binary is missing on this host (e.g. not
  macOS, or `osascript` unavailable).
- `2` — `--threshold` out of `[1, 100]`.
- `3` — other spawn error (PermissionError, OSError, etc.).

## Surfaces

- **Native OS popup.** Spawned non-blocking via the resolved notifier —
  `osascript display notification` on macOS, `notify-send` on Linux, or a
  custom `alerts.command_template`. If the OS notification surface is off
  or in Do Not Disturb, the popup is silently dropped (we cannot detect
  this).
- **Dashboard "Recent alerts" panel.** Press `9` (or click the panel) to
  open the modal with the full alert history for the current envelope.
  Collapsible from the panel header chevron.
- **Dashboard toast.** Transient pill near the top of the page when a
  new alert lands; click to dismiss. Distinct visual variant from status
  toasts; colored by the 3-tier severity (see [Severity](#severity)).
- **`alerts.log` audit line.** One tab-delimited line per dispatch
  attempt at `~/.local/share/cctally/logs/alerts.log`. The seven columns
  are: `timestamp`, `axis`, `threshold`, `window_key`, `mode`
  (`real`/`test`), `status` (`queued` / `no_notifier:<reason>` /
  `spawn_error:…`), and `severity` (`info`/`warn`/`critical`). The
  severity column is the 7th field.

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
cctally config get alerts.enabled
```
