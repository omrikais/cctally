// Canonical panel id type + default ordering.
//
// This module deliberately has zero imports: it is consumed by both
// `panelRegistry.ts` (which pulls in all panel components and therefore
// also pulls in the store) AND by `store/store.ts` itself. Putting the
// constants here breaks the otherwise-circular `panelRegistry → panels →
// store → panelRegistry` import cycle that would leave
// `DEFAULT_PANEL_ORDER` in the temporal dead zone during store module
// init.

// S8 (#254): the weekly/monthly/daily grid tiles collapsed into one
// `history` card (the relabeled heatmap). `history` is a grid PanelId; the
// share backend still knows daily/weekly/monthly as SharePanelId (see
// share/types.ts), reached from the History modal's period toggle.
export type PanelId =
  | 'current-week'
  | 'forecast'
  | 'trend'
  | 'sessions'
  | 'projects'
  | 'history'
  | 'blocks'
  | 'alerts'
  | 'cache-report';

// Grid-only ids (#248): `current-week` left the grid — it is now the hero
// strip (HeroStrip), opened via click/Enter, not a digit. It STAYS a valid
// `PanelId` so `ModalKind` / `SharePanelId` / the Current Week modal + share
// path continue to compile. Everything that owns a GRID card — the default
// order, the registry, the tier map, `openPanelByPosition`, `prefs.panelOrder`
// — is typed to `GridPanelId` so the grid can never address `current-week`.
export type GridPanelId = Exclude<PanelId, 'current-week'>;

// S8 (#254): `history` (the relabeled heatmap card) takes the slot the
// former `daily` card held among the wide cards; weekly/monthly left the
// grid. Canonical index 5 — the reconcile v3→v4 migration collapses a
// saved weekly/monthly/daily set into `history` at this position.
export const DEFAULT_PANEL_ORDER: GridPanelId[] = [
  'forecast', 'trend', 'sessions',
  'projects',
  'blocks', 'history', 'alerts',
  'cache-report',
];

// Two-tier grid (#248, spec §2). Compact uniform summary TILES (auto-fit
// packed) vs full-width content-height WIDE data cards. App partitions
// `prefs.panelOrder` by this static map into two independent dnd-kit strips.
export const CARD_TIER: Record<GridPanelId, 'tile' | 'wide'> = {
  forecast: 'tile', blocks: 'tile', alerts: 'tile',
  sessions: 'wide', trend: 'wide', projects: 'wide', history: 'wide', 'cache-report': 'wide',
};

// Panels for which a share affordance is rendered (spec §6.1, plan §M1.9).
//
// Two id families live here, so it is keyed by `string` (a DOM-derived
// `data-panel-kind` membership gate), NOT `PanelId`:
//   • the Python share ids (`bin/_lib_share_templates.SHARE_CAPABLE_PANELS`,
//     the source of truth — 9 hyphenated entries) which the modal ShareIcons
//     and the composer still key off (SharePanelId, share/types.ts). This
//     keeps daily/weekly/monthly here even though they left the grid.
//   • S8 (#254) `history` — the grid card whose id is a PanelId but NOT a
//     SharePanelId; global `S` on it routes through `gridPanelToSharePanel`
//     (history → daily) in share/keyboardShare.ts before the cast.
// Alerts is excluded by design: it's a notification stream, not a report.
export const SHARE_CAPABLE_PANELS: ReadonlySet<string> = new Set<string>([
  'current-week', 'forecast', 'trend', 'sessions', 'projects',
  'weekly', 'monthly', 'blocks', 'daily', 'history',
]);  // intentionally excludes 'alerts'
