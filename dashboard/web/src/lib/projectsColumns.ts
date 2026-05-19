import type { TableColumn } from './tableSort';

// Decorated row type for the ProjectsModal table. Computed in-place from
// `trend.projects[]` per spec §3.4 — see ProjectsModal.tsx's `tableRows`
// reduction for derivation. Hoisted here so PROJECTS_COLUMNS can reference
// the same field shape.
export interface ProjectsTableRow {
  key: string;
  sessionsCount: number;
  firstSeenAt: string | null;
  lastSeenAt: string | null;
  windowCost: number;
  windowPct: number | null;
  dollarsPerPct: number | null;
}

// Null-safe comparator helpers. nullsLast puts unknown values at the END
// regardless of sort direction (matches the rendered "—" affordance — a
// missing first_seen / last_seen / windowPct shouldn't jump to the top of
// either direction).
const nullsLast = (a: number | string | null, b: number | string | null): number | null => {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return null;
};
const cmpStr = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

// Spec §3.4 default directions (per the table — "Sort" column):
//   Project=asc, Sessions=desc, First seen=asc, Last seen=desc,
//   Cost=desc (default), Used %=desc, $/1%=desc.
export const PROJECTS_COLUMNS: TableColumn<ProjectsTableRow>[] = [
  {
    id: 'project',
    label: 'Project',
    defaultDirection: 'asc',
    compare: (a, b) => cmpStr(a.key, b.key),
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
    compare: (a, b) => {
      const n = nullsLast(a.firstSeenAt, b.firstSeenAt);
      if (n != null) return n;
      return cmpStr(a.firstSeenAt!, b.firstSeenAt!);
    },
    className: 'started',
  },
  {
    id: 'last_seen',
    label: 'Last seen',
    defaultDirection: 'desc',
    compare: (a, b) => {
      const n = nullsLast(a.lastSeenAt, b.lastSeenAt);
      if (n != null) return n;
      return cmpStr(a.lastSeenAt!, b.lastSeenAt!);
    },
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
    id: 'used_pct',
    label: 'Used %',
    defaultDirection: 'desc',
    numeric: true,
    compare: (a, b) => {
      const n = nullsLast(a.windowPct, b.windowPct);
      if (n != null) return n;
      return a.windowPct! - b.windowPct!;
    },
  },
  {
    id: 'dollar_per_pct',
    label: '$/1%',
    defaultDirection: 'desc',
    numeric: true,
    compare: (a, b) => {
      const n = nullsLast(a.dollarsPerPct, b.dollarsPerPct);
      if (n != null) return n;
      return a.dollarsPerPct! - b.dollarsPerPct!;
    },
  },
];
