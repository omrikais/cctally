import type { SessionRow } from '../types/envelope';
import type { TableColumn } from './tableSort';

// Canonical superset — one entry per possible column. The store sorts
// through this (id -> compare lookup) so a persisted sessionsSortOverride
// keeps working even when the panel currently hides the sorted column
// (e.g. a `model` override while the single-model view drops the Model
// column). See Codex pre-plan finding 1: the builder can't live only in
// the panel, or sorting by the new Session/Cache columns — and any
// persisted override — breaks in the store.
export const ALL_SESSIONS_COLUMNS: TableColumn<SessionRow>[] = [
  { id: 'started',  label: 'Started', defaultDirection: 'desc',
    compare: (a, b) => {
      const ta = Date.parse(a.started_utc ?? '') || 0;
      const tb = Date.parse(b.started_utc ?? '') || 0;
      return ta - tb;
    },
  },
  { id: 'duration', label: 'Dur',     defaultDirection: 'desc',
    compare: (a, b) => (a.duration_min ?? 0) - (b.duration_min ?? 0),
  },
  { id: 'model',    label: 'Model',   defaultDirection: 'asc',
    compare: (a, b) => (a.model || '').localeCompare(b.model || ''),
  },
  { id: 'session',  label: 'Session', defaultDirection: 'asc',
    compare: (a, b) => (a.title || '').localeCompare(b.title || ''),
  },
  { id: 'project',  label: 'Project', defaultDirection: 'asc',
    compare: (a, b) => (a.project || '').localeCompare(b.project || ''),
  },
  { id: 'cache',    label: 'Cache',   defaultDirection: 'desc', numeric: true,
    // Sessions with no cache_hit_pct (null) park at the END regardless of
    // asc/desc — nullKey is direction-invariant in applyTableSort — so the
    // comparator only ever sees two non-null values.
    nullKey: (r) => r.cache_hit_pct ?? null,
    compare: (a, b) => (a.cache_hit_pct ?? 0) - (b.cache_hit_pct ?? 0),
  },
  { id: 'cost',     label: 'Cost',    defaultDirection: 'desc', numeric: true,
    compare: (a, b) => (a.cost_usd ?? 0) - (b.cost_usd ?? 0),
  },
];

// Render-subset for the panel. Model is dropped entirely when the whole
// session set is one model (SESS-1 — the "all · <model>" caption is the
// signpost instead of a column of ditto middots). `transcriptsOn` is
// accepted for symmetry/future use: the Session column is ALWAYS present
// regardless of the transcript gate — a gated-off / missing title just
// renders as a muted em-dash, never a disappearing column.
export function sessionsColumns(
  { oneModel }: { oneModel: boolean; transcriptsOn: boolean },
): TableColumn<SessionRow>[] {
  return ALL_SESSIONS_COLUMNS.filter((c) => (oneModel ? c.id !== 'model' : true));
}
