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

| Tile | Emphasis | Status (M1) |
|---|---|---|
| **Recap** | Balanced — KPI strip + chart + table. | Selectable. Default. |
| **Visual** | Chart-first; minimal table. | Greyed; ships in M2. |
| **Detail** | Table-first; expanded rows. | Greyed; ships in M2. |

Only **Recap** is functional in M1. Visual / Detail tiles render but cannot be selected — the gallery animates ready for M2 expansion.

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
| `md` | Markdown — paste-friendly for Slack, GitHub issues, code reviews. (MD frontmatter ships in M2.) |
| `html` | Self-contained themed HTML document — open in browser, screenshot, or print to PDF. |
| `svg` | Inline graphics rendering. Same data shape as HTML; vector for slide decks. |

The format radio resets the preview pane to match. Some action buttons gate on format (e.g. `Copy` is MD-only; see below).

## Actions

The action bar at the bottom of the modal. Buttons gate on format and on milestone:

| Button | Behavior | M1 status |
|---|---|---|
| `Copy` | (MD only) Render with `Anon on export` honored, write to clipboard via `navigator.clipboard.writeText`. Greyed for HTML/SVG. | Functional |
| `Download` | Render to a blob, anchor-click with filename `cctally-<panel>-<utcdate>.<ext>`. | Functional |
| `Open` | (HTML / SVG only) Open the blob in a new tab via `URL.createObjectURL`. Greyed for MD. | Functional |
| `PNG` | (SVG only) Rasterize via `<canvas>` and download. | Deferred — M4 |
| `Print → PDF` | (HTML only) Render in a hidden iframe with print stylesheet, call `print()`. | Deferred — M4 |
| `+ Basket` | Push the recipe to the composer basket. | Deferred — M3 |
| `Save preset…` | Inline popover prompting for a name; persists to `/api/share/presets`. | Deferred — M2 |

Buttons deferred to later milestones still appear in M1 but are greyed with an explanatory tooltip.

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
| `Esc` | Close share modal. If layered above a panel modal, closes only the share layer; the panel modal stays open. |
| `Tab` / `Shift+Tab` | Cycle focus within the modal (template tiles → knobs → format → actions → save preset → close). |
| `Enter` on focused button | Trigger that action. |

**Guards (when `S` does nothing):**

- A share, composer, panel, or update modal is already open.
- A filter / search input has focus.
- No panel is focused (a help toast surfaces instead: *"Click a panel first, then press S to share it."*).
- The focused panel is the Alerts panel (not share-capable).
- The viewport is at or below the mobile breakpoint (640 px) — hotkeys are mouse-only on mobile per spec §12.9.

The `S` binding fires the same dispatch as clicking the panel's share icon; focus restores to the icon when the modal closes.

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

## See also

- [`share.md`](share.md) — the CLI `--format` reference (same render kernel, terminal-driven).
- [`dashboard.md`](dashboard.md) — the dashboard subcommand (server, keybindings, threat model).
- Design spec: `docs/superpowers/specs/2026-05-11-shareable-reports-v2-design.md`.
- Implementation plan: `docs/superpowers/plans/2026-05-11-share-v2.md`.
