import type { TableColumn } from './tableSort';

// Decorated row type for the ProjectsModal table. Computed in-place from
// `trend.projects[]` per spec §3.4 — see ProjectsModal.tsx's `tableRows`
// reduction for derivation. Hoisted here so PROJECTS_COLUMNS can reference
// the same field shape.
export interface ProjectsTableRow {
  key: string;
  displayKey?: string;
  sessionsCount: number;
  firstSeenAt: string | null;
  lastSeenAt: string | null;
  windowCost: number;
  windowPct: number | null;
  // % of weekly cost — `windowCost / total_window_cost` (0–100, NOT 0–1
  // so `fmt.pct0` renders directly). Replaces the v1 `dollarsPerPct`
  // column, which collapsed to a constant across rows by construction
  // (issue #72): `attributed_pct` is proportional to `cost_share`, so
  // `cost / attributed_pct = total_cost / weekly_used_pct` for every
  // project. `% of week` is the only orthogonal axis the data carries.
  // `null` when the active window has zero total cost (all rows null).
  shareOfWindow: number | null;
}

// Null-last semantics live on `TableColumn.nullKey` (see lib/tableSort.ts).
// Columns whose value can be null/undefined declare a `nullKey` extractor
// and `applyTableSort` parks those rows at the END regardless of asc/desc.
// The column's `compare` only runs for two non-null rows, so we can safely
// `!`-assert the non-null fields inside `compare`. Doing null-last inside
// `compare` is broken: `applyTableSort` flips the comparator under desc,
// which flips the null parking too — that is the regression this layout
// is designed to prevent (Codex round 1).
const cmpStr = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

// Spec §3.4 default directions (per the table — "Sort" column):
//   Project=asc, Sessions=desc, First seen=asc, Last seen=desc,
//   Cost=desc (default), Used %=desc, % of week=desc.
export const PROJECTS_COLUMNS: TableColumn<ProjectsTableRow>[] = [
  {
    id: 'project',
    label: 'Project',
    defaultDirection: 'asc',
    compare: (a, b) => cmpStr(a.displayKey ?? a.key, b.displayKey ?? b.key),
    className: 'project',
  },
  {
    id: 'sessions',
    label: 'Sessions',
    defaultDirection: 'desc',
    numeric: true,
    compare: (a, b) => a.sessionsCount - b.sessionsCount,
  },
  {
    id: 'first_seen',
    label: 'First seen',
    defaultDirection: 'asc',
    nullKey: (r) => r.firstSeenAt,
    compare: (a, b) => cmpStr(a.firstSeenAt!, b.firstSeenAt!),
    className: 'started',
  },
  {
    id: 'last_seen',
    label: 'Last seen',
    defaultDirection: 'desc',
    nullKey: (r) => r.lastSeenAt,
    compare: (a, b) => cmpStr(a.lastSeenAt!, b.lastSeenAt!),
    className: 'started',
  },
  {
    id: 'cost',
    label: 'Cost',
    defaultDirection: 'desc',
    numeric: true,
    compare: (a, b) => a.windowCost - b.windowCost,
  },
  {
    // #620 S1 D2 (F7) — the header states what the figure IS. `Used pp` alone
    // reads as one week's usage, and the only thing that said otherwise was a
    // `title`, which never appears on touch. The id is the persisted sort key
    // and stays `used_pct`; only the rendered label changes.
    id: 'used_pct',
    label: 'Used pp (sum)',
    title: "Sum of each week's attributed usage-% over the selected window (percentage-points; can exceed 100 across multiple weeks).",
    defaultDirection: 'desc',
    numeric: true,
    nullKey: (r) => r.windowPct,
    compare: (a, b) => a.windowPct! - b.windowPct!,
  },
  {
    id: 'share_of_window',
    label: 'Cost share',
    title: "This project's share of total project spend in the selected window.",
    defaultDirection: 'desc',
    numeric: true,
    nullKey: (r) => r.shareOfWindow,
    compare: (a, b) => a.shareOfWindow! - b.shareOfWindow!,
  },
];

// #620 S1 D2 (F7) + D13 (F4) — the definitions, in rendered text.
//
// Both figures named here already carried a `title`, and a `title` never
// appears on touch: there is no hover, and a long-press opens the platform's
// own menu instead. The tooltips stay; this caption is what a touch user can
// actually read. It sits UNDER the window selector because both definitions
// are relative to the window that selector chooses.
export const PROJECTS_WINDOW_CAPTION =
  'Used pp (sum) adds each week\u2019s attributed usage percentage across the '
  + 'selected window \u2014 a sum of percentage points, not a share of any one '
  + 'week. Cost share is each project\u2019s share of total project spend in that '
  + 'same window.';

// #620 S1 D1 — the merged-fold clause, stated on the surface rather than only
// enforced in the server arithmetic. Rendered ONLY when the provider has more
// than one real account, because below that threshold nothing is merged and
// the sentence would describe a fold that is not happening.
export const PROJECTS_MERGED_ACCOUNTS_NOTE =
  'Claude accounts are folded into one ranking here. Each account has its own '
  + 'weekly quota, so those percentages are never added across accounts.';
