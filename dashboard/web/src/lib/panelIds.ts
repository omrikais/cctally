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

// Grid-only ids (#248): `current-week` left the grid — it is now the hero
// strip (HeroStrip), opened via click/Enter, not a digit. It STAYS a valid
// `PanelId` so `ModalKind` / `SharePanelId` / the Current Week modal + share
// path continue to compile. Everything that owns a GRID card — the default
// order, the registry, the tier map, `openPanelByPosition`, `prefs.panelOrder`
// — is typed to `GridPanelId` so the grid can never address `current-week`.
export type GridPanelId = Exclude<PanelId, 'current-week'>;

export const DEFAULT_PANEL_ORDER: GridPanelId[] = [
  'forecast', 'trend', 'sessions',
  'projects',
  'weekly', 'monthly', 'blocks', 'daily', 'alerts',
  'cache-report',
];

// Two-tier grid (#248, spec §2). Compact uniform summary TILES (auto-fit
// packed) vs full-width content-height WIDE data cards. App partitions
// `prefs.panelOrder` by this static map into two independent dnd-kit strips.
export const CARD_TIER: Record<GridPanelId, 'tile' | 'wide'> = {
  forecast: 'tile', weekly: 'tile', monthly: 'tile', blocks: 'tile', alerts: 'tile',
  sessions: 'wide', trend: 'wide', projects: 'wide', daily: 'wide', 'cache-report': 'wide',
};

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
