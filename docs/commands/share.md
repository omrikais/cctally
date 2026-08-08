# Shareable reports (`--format` on reporting commands)

> The dashboard ships a **GUI** for these flags — see [`share-v2.md`](share-v2.md). The two paths share the render kernel (`bin/_lib_share.py`), so formatting, escaping and the provenance line are identical across them. They do **not** share snapshot builders, so titles, subtitles, column sets and the row axis differ: `daily` is one row per day on the CLI and one row per project on the dashboard. Do not expect a CLI artifact and a dashboard artifact of the same panel to be byte-identical.

`cctally` has a cross-command shareable-output surface: the explicitly listed command families below accept a `--format {md,html,svg}` flag that produces a self-contained artifact suitable for chat paste, GitHub issue, or screenshot. This page is the single reference for the flag surface; per-command pages document the per-command chart and table layouts.

> **Claude cost coverage:** Shared Claude dollar and token totals inherit the
> [transcript-derived lower-bound contract](../claude-cost-coverage.md). Codex
> accounting is unaffected.

## Supported subcommands

`report`, `daily`, `monthly`, `weekly`, `forecast`, `budget`, `project`, `five-hour-blocks`, `session`, the four `codex-*` accounting reports, and the source-aware `project`, `diff`, `range-cost`, `cache-report`, and `report` routes — sixteen commands in total. The dashboard has its own share UI; its source selection and share history are documented under *Dashboard share source identity* below.

For each source-aware command, the flat command and both fixed subgroup forms
(`cctally claude <command>` and `cctally codex <command>`) expose the same
share flags, including `--reveal-projects`; normal share validation runs before
provider reads or output-destination I/O.

## Provider identity and privacy

Source-aware CLI artifacts always identify their source. A direct Codex
artifact shows `Codex`; an all-source artifact is composed as exactly two
sections, Claude then Codex, rather than a synthetic `all` source. Empty
sections render `No data`; unavailable sections render `Unavailable: <stable
reason>` and are not silently dropped.

Project anonymization remains fail-closed at the share kernel's single scrub
chokepoint. Source-aware project cells carry opaque qualified identities, so
two equal visible labels from different Codex roots receive different aliases
(`project-1`, `project-2`) while the same identity is consistently aliased in
table cells, charts, and columns. Artifacts must not expose a canonical root,
home directory, source path/fingerprint, conversation key, or logical-limit
key. `--reveal-projects` opts out only of the project-basename anonymization;
it does not authorize exposing those internal identities.

Review every rendered MD/HTML/SVG artifact before sharing.

### Dashboard share source identity (#294 S5)

The dashboard share GUI stamps and displays source identity end-to-end. Opening a share flow (a panel's Share affordance, or `S` on a focused panel) captures the active source at that moment; switching the global selector while the modal, preview, or export is open never restamps the in-flight flow. Every render and composer request the client issues carries an explicit `source` (including `claude`) — so newly produced artifacts and history entries visibly say "Claude", "Codex", or "All" (an intended, documented change; a never-switching Claude user's data and flows are otherwise unchanged). The modal's live preview surfaces the flow's source label chrome so the pre-copy preview matches what the artifact says.

The share picker and the `S` shortcut gate through the per-source panel matrix: Claude offers its full nine-panel set (including forecast and trend); Codex and `All` offer the same seven-panel intersection (`current-week`, `daily`, `monthly`, `weekly`, `blocks`, `sessions`, `projects`) — forecast and trend are absent, since the server unconditionally builds both provider snapshots for `all`.

Each basket item permanently carries the source it was captured under, shown as an always-visible chip; a mixed-source basket is allowed and the composer renders every section with its source label, composing as provider-labelled sections (no blended snapshot exists client-side). Legacy stored basket items without a `source` load as Claude on read, without any destructive rewrite of stored bytes. Preset rows and "Recent shares" history rows display their stored-source label; presets keep the server's `(panel, name)` identity, so saving a preset under another source overwrites that record and updates its stored source, while history rows are per-source distinct (source participates in server-side history/digest identity — "same panel, different source" entries never collapse).

## Flags

| Flag | Default | Effect |
|---|---|---|
| `--format {md,html,svg}` | (off) | Render to markdown, HTML, or SVG. Mutually exclusive with `--json` (and `--status-line` on `forecast`). |
| `--theme {light,dark}` | `light` | Color theme for HTML/SVG. No-op for markdown. |
| `--reveal-projects` | off | Show real project basenames; default is `project-1`, `project-2`, … cost-descending. |
| `--no-branding` | off | Strip the advertisement, keep the provenance. See *What `--no-branding` removes* below. |
| `--output PATH` | (auto) | Write to PATH; `-` for stdout. Default: md→stdout, html/svg→`~/Downloads/cctally-<cmd>-<utcdate>.<ext>`. |
| `--copy` | off | Pipe md output to clipboard via `pbcopy`/`xclip`/`clip`. Rejected for html/svg. |
| `--open` | off | Open html/svg file in default app after writing. Rejected for md. |
| `--top-n N` (`session` only) | `15` | Cap rows to top N by cost in `--format` output. Validation: `N >= 1`. |

## What every artifact states about itself

Every artifact, in every format, carries a one-line facts strip directly under its title, followed by the generation timestamp:

```
2026-05-04 → 2026-05-09 (America/New_York) · projects anonymized
```

The line has three states, and which one you get depends on the report's contents rather than on a flag:

| State | When | Example |
|---|---|---|
| Period and zone only | The report contains no project names at all — `forecast` and `report` are the usual cases | `2026-05-04 → 2026-05-09 (Etc/UTC)` |
| … `· projects anonymized` | The report contains project names and they were aliased (the default) | `2026-05-04 → 2026-05-09 (Etc/UTC) · projects anonymized` |
| … `· real project names` | The report contains project names and `--reveal-projects` was passed | `2026-05-04 → 2026-05-09 (Etc/UTC) · real project names` |

A report with no project names states neither privacy claim, because it has no basis for either. The claim is read from what the render pipeline actually did, not inferred from the labels in the output — a project genuinely named `project-1` is reported as a real name.

The timezone is always a concrete IANA zone. The `local` and `utc` configuration tokens are resolved before the artifact is built, so no artifact prints `(local)`. The dates are the ones the report's own rows are bucketed by: a boundary that began life as a calendar label — a week start, a `--since` date, a daily or monthly bucket — keeps that calendar day in every zone, and a boundary that is a real moment in time — a 5-hour block's start, a session's last activity — is converted into the labelled zone, so a block that began at 03:30 UTC is dated the previous day in an American zone.

**The stated period always covers everything that artifact displays, and it is computed per format.** Markdown draws no chart, so a chart-only Markdown export states the window the report is about — the focal week, the current 5-hour block — while the HTML and SVG exports of the same report state the wider span their chart actually draws. A weekly Markdown recap therefore reads `2026-05-04 → 2026-05-10` where the HTML form of the same report reads `2026-03-16 → 2026-05-10`. The period is never narrowed: a report that covered nine days and found rows on five still states the nine days it covered.

**The Markdown frontmatter's `anonymized:` key and the facts strip's privacy clause answer different questions, so they can legitimately disagree.** `anonymized:` reports the MODE the export ran in: `true` unless `--reveal-projects` was passed. The strip's clause reports what the document CONTAINS: it is omitted entirely when the report has no project names, whichever mode was used. So a `report` export shows `anonymized: true` in its frontmatter and no privacy clause in its strip, which is what `tests/fixtures/share/report-md/output.md.golden` records. Neither is wrong; do not "fix" one to match the other.

An artifact never carries a table header with no rows under it. An empty-result report states its title, its facts strip, its availability text and its totals, and draws no table frame.

## What `--no-branding` removes

`--no-branding` removes the advertisement and keeps the provenance, in all three formats:

| Format | Removed | Kept |
|---|---|---|
| Markdown | The `Generated by cctally` footer line and the `cctally_version` frontmatter key | The whole rest of the frontmatter — `title`, `generated_at`, `period`, `panel`, `template_id`, `anonymized` — plus the facts strip and the timestamp |
| HTML | The `<footer>` element | The header, the facts strip and the timestamp |
| SVG | The footer text band | The header, the facts strip and the timestamp |

A composed document follows the same rule: its composite footer goes, its composite title and frontmatter stay.

## Output destination resolution

For html/svg, the default file path is computed as:

1. `$XDG_DOWNLOAD_DIR` if set (Linux XDG-compliant).
2. Else `~/Downloads` if the directory exists.
3. Else `~` (home directory) with a one-shot stderr hint.

Filename: `cctally-<cmd>-<utcdate>.<ext>`. `<utcdate>` is the UTC date of `generated_at` — chosen for byte-stable test goldens and immunity to host-tz midnight boundaries. Existing files are never overwritten; collision counter goes up to `-99` then errors with `cctally: too many same-day collisions`.

## Privacy

Project paths are anonymized by default. The mapping algorithm:

- Sort projects by descending $ cost within the rendered set.
- Assign `project-1`, `project-2`, … in that order.
- `(unknown)` (null `project_path`) keeps its literal label.

Mapping is point-in-time (not persisted). Re-running tomorrow may shuffle assignments. Use `--reveal-projects` to opt out.

## Examples

```bash
# Markdown to clipboard for Slack paste:
cctally daily --format md --copy

# HTML report, custom path:
cctally weekly --format html --output ~/Desktop/weekly.html

# SVG with dark theme:
cctally project --format svg --theme dark

# Reveal real project names:
cctally project --format md --reveal-projects

# Top 5 sessions:
cctally session --format md --top-n 5
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `2` | Invalid flag combination: `--format` mutex (with `--json` or `--status-line`); `--copy` with `--format html`/`--format svg`; `--copy` with `--output`; `--open` with `--format md`; `--top-n < 1`; missing clipboard tool with `--copy`. |
| `3` | Either of two staged failures. **Output filename collision exhaustion** — `~/Downloads/cctally-<cmd>-<utcdate>.<ext>` cannot be unique-named after 99 attempts; pass `--output <path>` to override. **Privacy refusal** — the artifact would have disclosed an absolute path, a UUID, an email address or another forbidden identifier class, so it was not produced. The refusal names the class and the offending value on stderr, and precedes destination resolution, so nothing is written. Both the single-source and the `--source all` paths exit 3. |
