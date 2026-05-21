// Canonical panel id type + default ordering.
//
// This module deliberately has zero imports: it is consumed by both
// `panelRegistry.ts` (which pulls in all panel components and therefore
// also pulls in the store) AND by `store/store.ts` itself. Putting the
// constants here breaks the otherwise-circular `panelRegistry → panels →
// store → panelRegistry` import cycle that would leave
// `DEFAULT_PANEL_ORDER` in the temporal dead zone during store module
// init.

export type PanelId =
  | 'current-week'
  | 'forecast'
  | 'trend'
  | 'sessions'
  | 'projects'
  | 'weekly'
  | 'monthly'
  | 'blocks'
  | 'daily'
  | 'alerts'
  | 'cache-report';

export const DEFAULT_PANEL_ORDER: PanelId[] = [
  'current-week', 'forecast', 'trend', 'sessions',
  'projects',
  'weekly', 'monthly', 'blocks', 'daily', 'alerts',
  'cache-report',
];

// Panels for which a share affordance is rendered (spec §6.1, plan §M1.9).
//
// Mirrors `bin/_lib_share_templates.SHARE_CAPABLE_PANELS` — the Python
// kernel is the source of truth (9 entries, hyphenated). Alerts is
// excluded by design: it's a notification stream, not a snapshotted
// report. Keep this set in sync with the Python side; the TS-side
// `SharePanelId` literal union (share/types.ts) is the typed reflection
// of the same ids.
export const SHARE_CAPABLE_PANELS: ReadonlySet<PanelId> = new Set([
  'current-week', 'forecast', 'trend', 'sessions', 'projects',
  'weekly', 'monthly', 'blocks', 'daily',
]);  // intentionally excludes 'alerts'
