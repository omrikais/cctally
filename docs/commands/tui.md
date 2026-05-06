# `cctally tui`

Live refreshing dashboard for subscription usage, spend, and recent-session
activity. Renders with `rich`; background daemon thread keeps the data
fresh while the main thread handles keys and redraws.

Two layout variants:

- **Conventional** (default): a 2×2 grid — Current Week / Forecast on top,
  $/1% Trend / Recent Sessions on the bottom.
- **Expressive** (`--expressive` or `--variant expressive`): a hero
  Current-Week meter beside a big trend sparkline, then a forecast
  strip, then full-width Recent Sessions.

## Synopsis

    cctally tui [--variant {conventional,expressive}]
                                   [--expressive] [--refresh SECONDS]
                                   [--sync-interval SECONDS] [--no-sync]
                                   [--no-color] [--tz TZ]

## Quick examples

    # Default: conventional grid, 1 Hz UI tick, 10 s sync
    cctally tui

    # Hero / expressive layout
    cctally tui --expressive

    # Faster UI tick, slower sync
    cctally tui --refresh 0.5 --sync-interval 20

    # Frozen view: no background sync, render once with current data
    cctally tui --no-sync

    # Via the wrapper
    cctally-tui --expressive

## Flags

| Flag | Default | Description |
|---|---|---|
| `--variant {conventional,expressive}` | `conventional` | Choose layout. |
| `--expressive` | off | Alias for `--variant expressive`. |
| `--refresh SECONDS` | `1.0` | UI tick cadence. Redraws when a key is pressed or this interval elapses. |
| `--sync-interval SECONDS` | `10.0` | How often the background daemon thread rebuilds `DataSnapshot` from the JSONL cache + SQLite. |
| `--no-sync` | off | Skip the background sync thread. The initial snapshot is still built; the UI stays frozen on it. Useful for screenshots and flaky-filesystem debugging. |
| `--no-color` | off | Disable ANSI color. Also honored: `NO_COLOR` env var. |
| `--tz TZ` | config | Display timezone for this call (`local`, `utc`, or IANA, e.g. `America/New_York`). Overrides config `display.tz`. See [Display timezone](config.md#how-displaytz-interacts-with-subcommands) for the full contract (parsing scope, JSON UTC invariant). Reset boundaries are always rendered in UTC for consistency with other subcommands. |

## Keys

### Dashboard

| Key | Action |
|---|---|
| `Tab` | Cycle panel focus (Current Week → Forecast → Trend → Sessions). Works in both variants. |
| `↑` / `↓` / `j` / `k` | Scroll sessions (or scroll modal content). |
| `PgUp` / `PgDn` | Page scroll. |
| `r` | Force an immediate sync (non-blocking — schedules the daemon). |
| `v` | Toggle variant (conventional ↔ expressive). |
| `?` | Show/hide the help overlay. |
| `q` / `Ctrl-C` | Quit, restore the terminal, and return exit code 0. |

### Sessions panel

| Key | Action |
|---|---|
| `s` | Cycle sort key: `last-activity` → `cost` → `duration` → `model` → `project`. Direction is hard-coded (descending for numeric/recency, ascending for text). |
| `f` | Open filter input (substring match across project & model, OR semantics, case-insensitive). Live narrowing. `Enter` applies, `Esc` cancels, empty + `Enter` clears. |
| `/` | Open search input. Live highlight + jump to first match. `Enter` confirms, `Esc` cancels. |
| `n` / `N` | After confirming a search: next / previous match. Wraps. |

### Detail modals

| Key | Action |
|---|---|
| `Enter` | Open the detail modal of the focused panel. Works in both variants (the focused panel's card border highlights to show which). |
| `1` / `2` / `3` / `4` | Universal shortcuts: open Current Week / Forecast / Trend / Sessions detail directly (works in both variants regardless of focus). |
| `Esc` | Close the active modal (or cancel an input prompt, or close the help overlay). |

Inside a modal: `↑↓ / j k` scroll content, `PgUp / PgDn` page, `Esc` closes, `q / Ctrl-C` quits the TUI. Other dashboard keys (`Tab`, `s`, `f`, `/`, `v`, `r`, `?`, `Enter`, `1-4`) are silently swallowed.

## Output

- **Conventional**: two rows of two bordered boxes, each tagged with a
  focus hint (`[1]` / `[2]` / `[3]` / `focus`). Header strip up top
  summarizes Week, Used %, 5-hour %, $/1%, forecast verdict, and sync age.
  Footer strip lists keybinds.
- **Expressive**: a colored verdict ribbon, a subheader, then a hero
  Current-Week panel beside a promoted `$/1% Trend` sparkline, a
  full-width Forecast & Budget strip, and a full-width Recent Sessions
  panel.

Both variants size columns by terminal width (80-99 / 100-119 / ≥120).
At <80 cols the command refuses to start.

## Data sources per panel

- **Current Week** — latest row in `weekly_usage_snapshots` (usage %,
  5-hour %) joined with live `session_entries` cost priced by
  `CLAUDE_MODEL_PRICING`.
- **Forecast** — reuses the pure helpers `_load_forecast_inputs` and
  `_compute_forecast` from the `forecast` subcommand.
- **$/1% Trend** — last 8 subscription weeks from `weekly_usage_snapshots`
  + live recomputed cost per week (same formula as `weekly`).
- **Recent Sessions** — latest 100 sessionIds from the shared
  `session_entries` cache, grouped and ordered by last activity.

All heavy reads happen on the sync thread; the main thread only touches
an atomic `_SnapshotRef`.

## Exit codes

- `0` — normal quit (`q` or `Ctrl-C`).
- `1` — fatal: `rich` not installed, terminal narrower than 80 cols,
  unrecoverable renderer error.
- `2` — argument error (bad `--force-size`, malformed snapshot module).

## Gotchas

- **Requires `rich`.** The import is lazy: other subcommands still work
  even if `rich` isn't available. If missing, `tui` prints a one-line
  install hint and exits 1.
- **Minimum 80 columns.** Between 80 and 99 cols, the Sessions panel
  drops the Model and Project columns in favor of a Cache % column;
  a narrow-warning line appears. Under 80 the command refuses to start.
- **tmux alt-screen quirks.** If `$TERM` lies about supporting the
  alternate screen, the dashboard may leave artifacts on exit. Work
  around by running inside a fresh tmux pane or forcing
  `TERM=tmux-256color`.
- **LOW CONF short-circuit.** When the current week lacks usage data
  (fresh install, no `record-usage` yet), the forecast panel skips the
  projection and shows a `LOW CONF` fallback with an install hint.
- **`--no-sync` freezes the view.** Timestamps continue to tick visually
  (e.g. "last snapshot 3m ago"), but no new data flows in. Use this
  only for stable screenshots.

## See also

- `forecast` — one-shot projection (the TUI's Forecast panel wraps this).
- `weekly` / `report` — the $/1% trend history that feeds the Trend panel.
- `session` — the full Recent Sessions rollup with filters.
