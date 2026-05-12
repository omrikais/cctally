# Share v2 — dashboard share GUI

`cctally`'s dashboard ships an in-browser share GUI that lets you customize and export any share-capable panel as Markdown, HTML, or SVG. This page documents the GUI surface (M1 release).

For the **command-line** share surface (`--format` on reporting subcommands), see [`share.md`](share.md). The two share paths use the same underlying render kernel (`bin/_lib_share.py`), so output is byte-stable across them.

## Overview

Each share-capable dashboard panel (and its detail modal) renders a header **share icon** (`↗`) next to the existing controls. Click it (or press `S` while the panel is focused) to open the **share modal**, which lets you choose a template, tune knobs, preview the result live, and export.

| Surface | Behavior |
|---|---|
| Panel header `↗` | Opens the share modal for that panel. |
| Modal header `↗` | Opens the share modal layered above the panel modal. Closing share returns to the underlying modal — it does not unmount. |
| Keyboard `S` | Opens the share modal for the currently-focused panel. (See [Keyboard](#keyboard).) |

**Not share-capable:** the Alerts panel has no share icon — it is a notification stream, not a snapshotted data view.

## Share modal anatomy

```
┌────────────────────────────────────────────────────────────┐
│ Share Weekly Report                                    ⤬   │
├────────────────────────────────────────────────────────────┤
│  [Recap]   [Visual]   [Detail]                             │
├────────────────────────────────────────────────────────────┤
│  ┌─────────────────────┬─────────────────────────────────┐ │
│  │ Period: This week ▾ │  ┌────────────────────────────┐ │ │
│  │ Theme:  ◉ light ○ dk│  │ sandboxed preview iframe   │ │ │
│  │ Top-N:  [10  ]      │  │ (live re-render on knob    │ │ │
│  │ Projects: 5 of 7 ▾  │  │  changes, 200 ms debounce) │ │ │
│  │ Show chart  ☑       │  └────────────────────────────┘ │ │
│  │ Show table  ☑       │                                  │ │
│  │ Anon on export ☑    │                                  │ │
│  └─────────────────────┴─────────────────────────────────┘ │
├────────────────────────────────────────────────────────────┤
│  Format: ◉ md ○ html ○ svg          [Save preset…]         │
│  [Copy] [Download] [Open] [PNG] [Print → PDF] [+ Basket]   │
└────────────────────────────────────────────────────────────┘
```

Top to bottom: panel title + close button, template gallery, knob column / preview iframe, format radio + actions.

## Template gallery

Three archetype tiles per panel (spec §9.4):

| Tile | Emphasis |
|---|---|
| **Recap** | Balanced — KPI strip + chart + table. Default. |
| **Visual** | Chart-first; minimal table. |
| **Detail** | Table-first; expanded rows. |

## Template inventory

24 templates total — 8 panels x 3 archetypes each.

| Panel | Recap | Visual | Detail |
|---|---|---|---|
| CurrentWeek | KPI strip + line + top-3 | Big % gauge + line | Per-day table + sidebar |
| Trend | $/% trend line over 8w | Trend with budget overlay | 8-week table + sparkline |
| Weekly | 8w rollup + sparkline | Bar chart of $/week | Per-week x per-model table |
| Daily | 7d bar + top-5 | Stacked bar by model | Per-day x per-project table |
| Monthly | Per-month bar + KPI | Month-over-month line | Per-month x per-model table |
| Blocks | Current block KPI + line | Burndown gauge | Per-block x model/project |
| Forecast | Projection + budget table | Projection w/ ceilings | Per-day forecast table |
| Sessions | Top-15 table + total | Top-N hbar | Top-50 + full columns |

## Knobs reference

Live in the left column of the modal. Knob changes debounce by 200 ms before re-rendering the preview.

| Knob | Effect |
|---|---|
| **Period** | `This week` (default for week-scoped panels), `Previous week`, or `Custom` (start/end pickers). For non-weekly panels the default tracks the panel's focus (Daily → today; Monthly → current month; etc.). |
| **Theme** | `light` (default) or `dark` palette. No-op for Markdown. |
| **Top-N** | Number of rows in the table. Default 5; 15 for Sessions. Validation: `N >= 1`. |
| **Projects allowlist** | Multi-select popover with real project names. Filters before anonymization, so anon labels stay dense (`project-1, project-2, project-3` — no gaps). |
| **Show chart** | Include / drop the chart fragment from the output. |
| **Show table** | Include / drop the table fragment from the output. |
| **Anon on export** | Anonymize project names in exports. **Preview always reveals**; only exports respect this checkbox. Default on. |

## Format selector

| Format | Output |
|---|---|
| `md` | Markdown — paste-friendly for Slack, GitHub issues, code reviews. Includes YAML frontmatter with panel, template id, period, anonymization, and version metadata. |
| `html` | Self-contained themed HTML document — open in browser, screenshot, or print to PDF. |
| `svg` | Inline graphics rendering. Same data shape as HTML; vector for slide decks. |

The format radio resets the preview pane to match. Some action buttons gate on format (e.g. `Copy` is MD-only; see below).

## Actions

The action bar at the bottom of the modal. Buttons gate on format:

| Button | Behavior | Format gate |
|---|---|---|
| `Copy` | Render with `Anon on export` honored, write to clipboard via `navigator.clipboard.writeText`. | MD only |
| `Download` | Render to a blob, anchor-click with filename `cctally-<panel>-<utcdate>.<ext>`. | All |
| `Open` | Open the blob in a new tab via `URL.createObjectURL`. | HTML / SVG |
| `PNG` | Rasterize the rendered SVG via a client-side `<canvas>` and anchor-click as PNG. Background fill is explicit so dark-theme exports stay legible. | SVG only |
| `Print -> PDF` | Render the HTML in a hidden iframe with an injected print stylesheet, call `iframe.print()`. Browser's native print dialog handles save-as-PDF. | HTML only |
| `+ Basket` | Add the current recipe (panel + template + options, **no rendered body**) to the basket. Header chip pulses and the count badge increments. | All |
| `Save preset...` | Inline name popover. Persists the template + knob recipe under `share.presets[<panel>][<name>]` via `POST /api/share/presets`. | All |

Buttons that don't apply to the selected format are disabled with an explanatory tooltip.

## Privacy & anonymization

- The render kernel anonymizes project paths by default (`project-1`, `project-2`, … cost-descending).
- The single chokepoint is `_scrub` in `bin/_lib_share.py`. The Layer-A "no original tokens" invariant test guards against leaks.
- The **preview pane always reveals** real names so you can verify what you're sharing. The `Anon on export` checkbox controls only the exported artifact.
- The mapping is point-in-time; re-rendering tomorrow may shuffle assignments. Uncheck `Anon on export` if you need stable names.
- See [Privacy](share.md#privacy) in the CLI reference for the full algorithm.

## Keyboard

| Key | Action |
|---|---|
| `S` | Open share modal for the currently-focused panel. (Click a panel first to focus it.) |
| `B` | Open the composer. Works whether the basket is empty or not — an empty composer shows a hint to add sections from any panel's share modal. |
| `Esc` | Close the topmost overlay (share modal -> composer modal -> preset popover). Underlying layers stay open. |
| `Tab` / `Shift+Tab` | Cycle focus within the active modal. |
| `Enter` on focused button | Trigger that action. |

**Guards (when `S` / `B` do nothing):**

- A share, composer, panel, or update modal is already open (mode-stack guard).
- A filter / search input has focus.
- No panel is focused — `S` surfaces a help toast: *"Click a panel first, then press S to share it."*
- The focused panel is the Alerts panel (not share-capable).
- The viewport is at or below the mobile breakpoint (640 px) — hotkeys are mouse-only on mobile per spec §12.9.

Both bindings fire the same dispatch as clicking the corresponding icon (panel `↗` for `S`, header chip for `B`); focus restores to the trigger when the modal closes.

## Presets

Save a template + knob recipe as a named preset for the panel:

- In the share modal, configure the template and knobs as you want them.
- Click **Save preset...** in the action bar.
- Type a name (1-64 chars, no `/`) and hit Enter.

Recall a preset from the gallery's **presets ▾** affordance — it replaces the modal's `template_id` + `options` with the saved recipe. Manage all presets across panels via **Manage presets...** at the bottom of the dropdown.

Presets persist in `~/.local/share/cctally/config.json` under `share.presets[<panel>][<name>]` — CLI-readable for future `--preset <name>` support (designed-for-but-not-shipped in v2).

## Basket + composer

Build multi-section reports by collecting templates from different panels into a "basket":

1. Open any panel's share modal.
2. Click **+ Basket** — the section's recipe (no rendered body — see [Privacy](#privacy--anonymization)) is added to the basket.
3. Repeat from other panels. The header chip 📋 shows the count.
4. Click the chip (or press `B`) to open the composer.

The composer modal:

- **Left pane:** reorderable section list (drag the `≡` handle). Per-section kebab `⋯` opens preview-only / refresh-from-current-data / remove.
- **Right pane:** live combined preview in a sandboxed iframe.
- **Top knobs:** title, theme, format, anon-on-export, no-branding. These are composite — they override every section's add-time values per spec §8.5.
- **Outdated badge:** appears when section data has shifted since add-time OR when the kernel version has changed. Click `Refresh from current data` to re-render.
- **Real-name banner:** appears at the top of the composed output when at least one section was added with `reveal_projects=true` AND the composite `anon-on-export` is unchecked. Anonymizing at compose time hides the banner.

The basket persists across page reloads in browser localStorage (`cctally:share:basket`). Hard-capped at 20 sections. Cleared via the composer's `Clear all` button (no Undo affordance — refresh is the recovery).

## Share history

Every successful export appends a recipe (no body) to a 20-deep ring buffer at `share.history` in `config.json`. Surfaced under the **Recent shares** segmented group inside the gallery's **presets ▾** dropdown — filtered to the panel you're sharing. Clicking a history entry overwrites the modal's options; it does NOT auto-export.

Clearing history has no GUI control in v2 yet — edit `~/.local/share/cctally/config.json` directly (drop the `share.history` key) or call `DELETE /api/share/history` (Origin/Host CSRF gate applies; pass `-H 'Origin: http://127.0.0.1:8789'`).

## Examples

**Share this week's cost recap to a teammate in Slack:**

1. Focus the Current Week panel (Tab to it, or just click anywhere in the panel body).
2. Press `S` (or click `↗`).
3. Leave defaults; format `md`; click `Copy`.
4. Paste into Slack.

**Generate a themed HTML brief for a weekly review:**

1. Click `↗` on the Weekly panel.
2. Set `Theme: dark`, `Show chart` on, `Top-N: 10`.
3. Format `html` → click `Download`.
4. The file lands in your default downloads folder as `cctally-weekly-<YYYY-MM-DD>.html`.

**Reveal real project names for a personal record:**

1. Open any panel's share modal.
2. Uncheck `Anon on export`.
3. Click `Download` — projects appear with their git-root basenames instead of `project-N`.

## Manual smoke checklist

A reusable end-to-end walk-through for verifying the share-v2 surface in a real browser. Plan to spend ~15 minutes. File any regression as a GitHub issue immediately when it surfaces (don't batch — context decays).

### Setup

```bash
# Terminal A — backend
cctally dashboard

# Terminal B — frontend dev server with hot-reload
cd dashboard/web && nvm use && npm run dev
# Visit http://127.0.0.1:5173/
```

### 1. Per-panel share + export

For each of the 8 share-capable panels (CurrentWeek, Trend, Weekly, Daily, Monthly, Blocks, Forecast, Sessions):

1. Click the `↗` icon (or focus the panel and press `S`).
2. In the modal, cycle Recap -> Visual -> Detail.
3. Toggle `Anon on export`.
4. Test each format: MD -> Copy; HTML -> Open + Print -> PDF; SVG -> Download + PNG.
5. Verify the preview iframe updates within 200 ms after each knob change.

**Pass:** every export produces a non-empty file / clipboard text; no console errors.

### 2. Basket + composer

1. Add 3 sections to the basket from different panels (Weekly Recap, Daily Visual, Forecast Detail).
2. Verify the header chip 📋 shows count `3` with the amber badge.
3. Click the chip; the composer opens.
4. Reorder via drag.
5. Toggle `Anon on export` on the composer. Verify the section list rows do NOT re-show the "Outdated" badge for sections that were added with `reveal_projects=true` — they should appear in the composite output as anonymized.
6. Test the per-section kebab -> `Remove` on one section.
7. Test `Refresh from current data` on the second section. The Outdated badge (if shown) should clear.
8. Export the composed report as HTML, then PDF via Print.
9. Hit `Clear all`.

**Pass:** section reorder persists across composer close-reopen (via the basket localStorage); recompose fires within 400 ms (200 ms debounce + network roundtrip); no console errors.

### 3. Preset + history

1. Configure a non-default knob set on the Weekly panel (e.g., theme dark, top-N 10, anon ON).
2. Click `Save preset...` -> name it `team-monday`.
3. Refresh the page.
4. Reopen the Weekly share modal; click `presets ▾`.
5. Verify `team-monday` appears AND the `Recent shares` group shows your earlier exports.
6. Click the preset; verify the form repopulates correctly.

**Pass:** preset round-trips across reload; history persists; nothing in `share.presets` / `share.history` looks malformed in `~/.local/share/cctally/config.json`.

### 4. Keyboard

1. Click a Weekly panel cell to focus the panel. Press `S`. Share modal opens.
2. Press `Esc`. Modal closes; focus returns to the share icon.
3. Press `B`. Composer opens.
4. Press `Esc`. Composer closes.
5. With a panel modal open (click on a Weekly KPI to deep-dive), press `S`. Spec says guards block this — `S` should be inert.
6. With nothing focused, press `S`. Toast: "Click a panel first, then press S to share it."

**Pass:** keyboard behavior matches spec §12.1 guards; no rogue keystrokes.

### 5. Accessibility

1. With the system "Reduce motion" setting enabled, add a section to the basket. Verify the chip pulse animation does NOT fire (or appears static).
2. Tab-through the share modal. Verify focus visits: template tiles -> knobs -> format -> actions -> save preset -> close (X).
3. Open the composer; tab-through. Verify focus visits: composite knobs -> section list (each row focusable) -> per-section kebab -> real-name banner button (if visible) -> composite actions -> Clear all.
4. With a screen reader: open the share modal — should announce "dialog, Share Weekly Report". Open the composer — should announce "dialog, Compose report".

**Pass:** full keyboard navigability; reduced-motion respected; no axe-core regressions if you run an automated audit.

### 6. File issues for any failures

For each smoke failure, file a GitHub issue with:

- Steps to reproduce
- Expected behavior (cite spec §X.Y)
- Actual behavior (with screenshot if visual)
- Browser + OS

If the smoke is fully clean, this run has no deliverable beyond the pass itself.

## See also

- [`share.md`](share.md) — the CLI `--format` reference (same render kernel, terminal-driven).
- [`dashboard.md`](dashboard.md) — the dashboard subcommand (server, keybindings, threat model).
- Design spec: `docs/superpowers/specs/2026-05-11-shareable-reports-v2-design.md`.
- Implementation plan: `docs/superpowers/plans/2026-05-11-share-v2.md`.
